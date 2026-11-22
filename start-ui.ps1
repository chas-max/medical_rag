<#
.SYNOPSIS
    启动/停止 medical-rag-resume 的 Streamlit 问答与指标控制台（本机 8501）。

.DESCRIPTION
    单一入口：启动租户登录的 Streamlit 控制台，包含 RAG 问答页（连接本机
    RAG 后端 http://127.0.0.1:8001）和数据统计页（多版本检索指标、延迟、
    吞吐、任务进程）。问答需要后端与检索依赖已就绪：请先运行 .\start.ps1；
    后端未启动时界面仍可登录并查看本地已持久化的指标与评测报告。

    租户账号、会话映射、问答指标与界面视图状态持久化到
    data\streamlit\ui-state.sqlite3（可通过环境变量 UI_STATE_FILE 覆盖）。
    该登录只隔离界面层数据，不是 RAG 后端的身份认证。

    安全边界：只绑定 127.0.0.1，不得转发、不得接入反向代理、不得处理真实
    患者数据；RAG 后端必须是本机 unsafe-local-demo，界面不写问答库。

.PARAMETER Stop
    停止本脚本启动的 Streamlit 进程（校验命令行匹配 medical_rag|streamlit）。

.PARAMETER Status
    只打印运行状态、PID、URL 与界面状态文件路径，不启动、不停止任何进程。

.PARAMETER Port
    监听端口，默认 8501（只绑定 127.0.0.1）。

.PARAMETER NoBrowser
    启动成功后不自动打开浏览器。

.PARAMETER Install
    缺少 streamlit 时允许用 uv 自动安装可选依赖 [ui]。

.PARAMETER Foreground
    前台运行（Ctrl+C 停止），用于排查启动问题；此时不写 PID 文件、不自动
    打开浏览器。

.PARAMETER TimeoutSeconds
    后台启动后等待 /_stcore/health 通过的最长秒数，默认 90。

.EXAMPLE
    .\start-ui.ps1

.EXAMPLE
    .\start-ui.ps1 -NoBrowser

.EXAMPLE
    .\start-ui.ps1 -Status

.EXAMPLE
    .\start-ui.ps1 -Stop

.EXAMPLE
    .\start-ui.ps1 -Foreground

.NOTES
    Windows 的 .ps1 没有默认文件关联：在 cmd 里输入 .\start-ui.ps1 可能用记事本
    打开文件而不执行。请改用 .\start-ui.cmd，或在 PowerShell 中显式运行：
      pwsh -NoProfile -ExecutionPolicy Bypass -File .\start-ui.ps1
    cmd 入口 start-ui.cmd 会把参数（如 -Status、-Stop、-NoBrowser）原样传给本脚本。
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Status,
    [int]$Port = 8501,
    [switch]$NoBrowser,
    [switch]$Install,
    [switch]$Foreground,
    [int]$TimeoutSeconds = 90
)

# Windows PowerShell 5.1 turns a native tool's stderr into a terminating
# NativeCommandError when this is 'Stop'. Keep 'Continue' and check $LASTEXITCODE
# explicitly so uv/streamlit progress text cannot abort startup.
$ErrorActionPreference = 'Continue'

$Root       = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunDir     = Join-Path $Root 'outputs\run'
$LogDir     = Join-Path $Root 'outputs\logs'
$Python     = Join-Path $Root '.venv\Scripts\python.exe'
$PidFile    = Join-Path $RunDir 'streamlit-ui.pid'
$StdOutLog  = Join-Path $LogDir 'streamlit-ui.log'
$StdErrLog  = Join-Path $LogDir 'streamlit-ui.err.log'
$UiUrl      = "http://127.0.0.1:$Port"
$HealthUrl  = "$UiUrl/_stcore/health"
$BackendHealthUrl = 'http://127.0.0.1:8001/health'
$DefaultStateFile = Join-Path $Root 'data\streamlit\ui-state.sqlite3'
$InstallCommand = 'uv pip install --python .venv\Scripts\python.exe -e ".[ui]"'

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "  [OK]   $Message" -ForegroundColor Green }
function Write-Info([string]$Message) { Write-Host "  [..]   $Message" -ForegroundColor Gray }
function Write-Warn([string]$Message) { Write-Host "  [WARN] $Message" -ForegroundColor Yellow }
function Write-Err([string]$Message)  { Write-Host "  [FAIL] $Message" -ForegroundColor Red }

# ---------- 通用工具 ----------
function Get-ListeningProcessId {
    param([int]$Port)
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -First 1
        if ($conn) { return [int]$conn.OwningProcess }
        return $null
    }
    $line = netstat -ano | Select-String -Pattern "[:.]$Port\s+.*LISTENING" | Select-Object -First 1
    if ($line) {
        $parts = ($line.Line -split '\s+') | Where-Object { $_ }
        return [int]$parts[-1]
    }
    return $null
}

function Get-CommandLine {
    param([int]$ProcessId)
    return (Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue).CommandLine
}

# 只认可本项目启动的界面进程，避免按 PID/端口误杀其他程序。
function Test-StreamlitProcess {
    param([string]$CommandLine)
    if (-not $CommandLine) { return $false }
    return ($CommandLine -match 'medical_rag|streamlit')
}

function Get-UvPath {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    if ($env:USERPROFILE) {
        foreach ($relative in @('.local\bin\uv.exe', '.cargo\bin\uv.exe')) {
            $candidate = Join-Path $env:USERPROFILE $relative
            if (Test-Path $candidate) { return $candidate }
        }
    }
    return $null
}

function Save-PidFile {
    param([int]$ProcessId)
    if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Path $RunDir -Force | Out-Null }
    Set-Content -Path $PidFile -Value $ProcessId -Encoding ascii
}

function Get-SavedProcessId {
    if (-not (Test-Path $PidFile)) { return $null }
    $text = (Get-Content -Path $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    $parsed = 0
    if ([int]::TryParse([string]$text, [ref]$parsed)) { return $parsed }
    return $null
}

function Remove-PidFile {
    if (Test-Path $PidFile) { Remove-Item $PidFile -Force -ErrorAction SilentlyContinue }
}

function Stop-ProcessTree {
    param([int]$ProcessId, [string]$Label)
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $proc) {
        Write-Info "$Label 已不在运行 (PID $ProcessId)"
        return
    }
    if (Get-Command taskkill -ErrorAction SilentlyContinue) {
        $null = & taskkill /PID $ProcessId /T /F 2>$null
    } else {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    try { Wait-Process -Id $ProcessId -Timeout 5 -ErrorAction SilentlyContinue } catch { }
    Write-Ok "已停止 $Label (PID $ProcessId)"
}

# 界面健康检查：GET http://127.0.0.1:<port>/_stcore/health 返回 ok。
# 用 HttpWebRequest 并显式禁用代理，避免 HTTP_PROXY/系统代理把回环请求带走。
function Test-StreamlitHealth {
    param([string]$Url)
    try {
        $request = [System.Net.HttpWebRequest]::Create($Url)
        $request.Proxy = $null
        $request.Timeout = 3000
        $request.ReadWriteTimeout = 3000
        $request.Method = 'GET'
        $response = $request.GetResponse()
        try {
            $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
            try { $body = $reader.ReadToEnd().Trim() } finally { $reader.Dispose() }
            return (([int]$response.StatusCode -eq 200) -and ($body -eq 'ok'))
        }
        finally { $response.Close() }
    }
    catch { return $false }
}

function Wait-StreamlitHealth {
    param([int]$Timeout)
    $deadline = (Get-Date).AddSeconds($Timeout)
    while ((Get-Date) -lt $deadline) {
        if (Test-StreamlitHealth -Url $HealthUrl) { return $true }
        Start-Sleep -Seconds 1
    }
    return (Test-StreamlitHealth -Url $HealthUrl)
}

# RAG 后端（默认本机 8001）是问答页的数据源；没有它界面仍可登录并查看历史指标，
# 但无法完成问答。这里只做只读探测，不影响启动结果。
function Test-RagBackend {
    try {
        $request = [System.Net.HttpWebRequest]::Create($BackendHealthUrl)
        $request.Proxy = $null
        $request.Timeout = 2000
        $request.ReadWriteTimeout = 2000
        $request.Method = 'GET'
        $response = $request.GetResponse()
        try { return ([int]$response.StatusCode -eq 200) } finally { $response.Close() }
    }
    catch { return $false }
}

function Resolve-StateFile {
    $fromEnv = [string]$env:UI_STATE_FILE
    if ($fromEnv -and $fromEnv.Trim()) {
        $value = $fromEnv.Trim()
        if ([System.IO.Path]::IsPathRooted($value)) { return $value }
        return (Join-Path $Root $value)
    }
    return $DefaultStateFile
}

function Assert-Python {
    if (-not (Test-Path $Python)) {
        Write-Err "未找到虚拟环境解释器: $Python"
        Write-Host '  请先创建虚拟环境并安装界面依赖：' -ForegroundColor Yellow
        Write-Host '    uv venv .venv' -ForegroundColor Yellow
        Write-Host "    $InstallCommand" -ForegroundColor Yellow
        exit 1
    }
    Write-Ok "Python: $(& $Python --version 2>&1)"
}

function Assert-Streamlit {
    & $Python -c 'import streamlit, httpx, pandas' 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok 'Streamlit 依赖可导入'
        return
    }

    Write-Warn '缺少 Streamlit（可选依赖 [ui]）'
    $uv = Get-UvPath
    if ($Install -and -not $uv) {
        Write-Err '指定了 -Install，但未找到 uv.exe，无法自动安装'
        Write-Host '  请先安装 uv，再手动执行：' -ForegroundColor Yellow
        Write-Host "    $InstallCommand" -ForegroundColor Yellow
        exit 1
    }
    if ($uv) {
        # uv 创建的 .venv 没有 pip，必须用 uv pip install。
        Write-Info "安装界面依赖（$uv pip install，网络可用时约 1 分钟）"
        Push-Location $Root
        try {
            & $uv pip install --python $Python -e '.[ui]'
            if ($LASTEXITCODE -ne 0) {
                Write-Err "界面依赖安装失败（退出码 $LASTEXITCODE）"
                Write-Host '  请手动执行：' -ForegroundColor Yellow
                Write-Host "    $InstallCommand" -ForegroundColor Yellow
                exit 1
            }
        }
        finally { Pop-Location }

        & $Python -c 'import streamlit' 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Err '安装后仍无法 import streamlit'
            Write-Host "  请手动执行：$InstallCommand" -ForegroundColor Yellow
            exit 1
        }
        Write-Ok 'Streamlit 安装完成'
        return
    }

    Write-Err '缺少 Streamlit，且未找到 uv.exe（未自动安装）'
    Write-Host '  请先安装可选依赖 [ui]（在仓库根执行）：' -ForegroundColor Yellow
    Write-Host "    $InstallCommand" -ForegroundColor Yellow
    exit 1
}

# 若端口/PID 文件已被本项目的界面进程占用则返回其 PID；被其他程序占用则失败。
function Get-RunningStreamlitProcessId {
    $saved = Get-SavedProcessId
    if ($saved) {
        $proc = Get-Process -Id $saved -ErrorAction SilentlyContinue
        if ($proc -and (Test-StreamlitProcess -CommandLine (Get-CommandLine -ProcessId $saved))) {
            return $saved
        }
        Remove-PidFile
    }

    $listener = Get-ListeningProcessId -Port $Port
    if ($listener) {
        $cmd = Get-CommandLine -ProcessId $listener
        if (Test-StreamlitProcess -CommandLine $cmd) { return $listener }
        Write-Err "端口 $Port 已被其他进程占用 (PID $listener)"
        Write-Host '  请先释放端口，或使用 -Port 指定其他端口。' -ForegroundColor Yellow
        exit 1
    }
    return $null
}

function Show-StreamlitInfo {
    param([int]$ProcessId)
    $stateFile = Resolve-StateFile
    Write-Host ''
    Write-Ok "Streamlit 问答/指标控制台已就绪 (PID $ProcessId)"
    Write-Host '  地址: ' -NoNewline
    Write-Host $UiUrl -ForegroundColor White
    Write-Host '  界面状态（持久化）: ' -NoNewline
    Write-Host $stateFile -ForegroundColor Gray
    Write-Host '  登录页已自动填入本机演示账号密码，直接点击「登录」即可。' -ForegroundColor Cyan
    Write-Host '  日志: outputs\logs\streamlit-ui.log (错误: streamlit-ui.err.log)' -ForegroundColor Gray
    if (Test-RagBackend) {
        Write-Ok 'RAG 后端 (127.0.0.1:8001) 可访问'
    } else {
        Write-Warn '未检测到 RAG 后端 (127.0.0.1:8001)：问答页会提示后端不可用；请先运行 .\start.ps1'
    }
    Write-Host '  停止: .\start-ui.ps1 -Stop' -ForegroundColor Gray
    Write-Host ''
    Write-Host '  仅限本机演示，禁止转发或处理真实患者数据。' -ForegroundColor Yellow
}

function Show-StreamlitStatus {
    Write-Step 'Streamlit 控制台状态'
    $stateFile = Resolve-StateFile
    $running = $null

    $saved = Get-SavedProcessId
    if ($saved) {
        $proc = Get-Process -Id $saved -ErrorAction SilentlyContinue
        if ($proc -and (Test-StreamlitProcess -CommandLine (Get-CommandLine -ProcessId $saved))) {
            $running = $saved
        }
    }
    if (-not $running) {
        $listener = Get-ListeningProcessId -Port $Port
        if ($listener) {
            $cmd = Get-CommandLine -ProcessId $listener
            if (Test-StreamlitProcess -CommandLine $cmd) {
                $running = $listener
            } else {
                Write-Warn "端口 $Port 被其他进程占用 (PID $listener)"
            }
        }
    }

    if (-not $running) {
        Write-Info "运行状态: 未运行"
        Write-Host "  地址: $UiUrl" -ForegroundColor Gray
        Write-Host "  界面状态: $stateFile" -ForegroundColor Gray
        return
    }

    Write-Ok "运行状态: 运行中 (PID $running)"
    Write-Host "  地址: $UiUrl" -ForegroundColor White
    if (Test-StreamlitHealth -Url $HealthUrl) {
        Write-Host '  健康检查: 通过 (/_stcore/health)' -ForegroundColor Green
    } else {
        Write-Host '  健康检查: 未通过（进程可能仍在启动）' -ForegroundColor Yellow
    }
    Write-Host "  界面状态: $stateFile" -ForegroundColor Gray
    if (Test-RagBackend) {
        Write-Host '  RAG 后端 (127.0.0.1:8001): 可访问' -ForegroundColor Green
    } else {
        Write-Host '  RAG 后端 (127.0.0.1:8001): 不可访问（问答不可用，先运行 .\start.ps1）' -ForegroundColor Yellow
    }
    Write-Host '  停止命令: .\start-ui.ps1 -Stop' -ForegroundColor Gray
}

function Stop-StreamlitUi {
    Write-Step '停止 Streamlit 控制台'
    $stopped = 0

    $saved = Get-SavedProcessId
    if ($saved) {
        $proc = Get-Process -Id $saved -ErrorAction SilentlyContinue
        if (-not $proc) {
            Write-Info "PID 文件指向的进程已不在运行 (PID $saved)"
        }
        elseif (Test-StreamlitProcess -CommandLine (Get-CommandLine -ProcessId $saved)) {
            Stop-ProcessTree -ProcessId $saved -Label 'Streamlit 控制台'
            $stopped++
        }
        else {
            Write-Warn "PID 文件指向的进程不是 Streamlit 控制台，忽略 (PID $saved)"
        }
        Remove-PidFile
    }

    $listener = Get-ListeningProcessId -Port $Port
    if ($listener) {
        $cmd = Get-CommandLine -ProcessId $listener
        if (Test-StreamlitProcess -CommandLine $cmd) {
            Stop-ProcessTree -ProcessId $listener -Label 'Streamlit 控制台 (端口残留)'
            $stopped++
        } else {
            Write-Warn "端口 $Port 被其他进程占用 (PID $listener)，未自动结束"
        }
    }

    if ($stopped -eq 0) { Write-Info '没有发现正在运行的 Streamlit 控制台' }
    else { Write-Ok 'Streamlit 控制台已停止' }
}

# ---------- 主流程 ----------
if ($Stop) {
    Stop-StreamlitUi
    exit 0
}
if ($Status) {
    Show-StreamlitStatus
    exit 0
}

try {
    Write-Host '医疗资料 RAG 问答 — Streamlit 问答/指标控制台' -ForegroundColor White
    Write-Host "项目目录: $Root" -ForegroundColor DarkGray
    Write-Host "监听地址: $UiUrl （仅本机）" -ForegroundColor DarkGray

    # 幂等：端口/PID 文件已是本项目界面进程时直接复用，不再检查依赖。
    $running = Get-RunningStreamlitProcessId
    if ($running) {
        Save-PidFile -ProcessId $running
        Write-Info "端口 $Port 已被本项目的 Streamlit 进程占用 (PID $running)，复用"
        if (Wait-StreamlitHealth -Timeout $TimeoutSeconds) {
            Show-StreamlitInfo -ProcessId $running
            if (-not $NoBrowser) {
                try { Start-Process $UiUrl | Out-Null }
                catch { Write-Warn "无法自动打开浏览器，请手动访问 $UiUrl" }
            }
            exit 0
        }
        Write-Warn '现有进程未通过健康检查，清理后重新启动'
        Stop-ProcessTree -ProcessId $running -Label 'Streamlit 控制台 (无响应)'
        Remove-PidFile
    }

    Assert-Python
    Assert-Streamlit

    if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Path $RunDir -Force | Out-Null }
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    $stateFile = Resolve-StateFile
    $stateDir = Split-Path -Parent $stateFile
    if ($stateDir -and -not (Test-Path $stateDir)) {
        New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    }
    # 子进程继承环境变量；界面据此把状态写到 data\streamlit\ui-state.sqlite3。
    $env:UI_STATE_FILE = $stateFile

    $arguments = @('-m', 'medical_rag.ui',
                   '--host', '127.0.0.1', '--port', "$Port", '--no-browser')

    if ($Foreground) {
        Write-Step '前台运行 Streamlit 控制台'
        Write-Info 'Ctrl+C 停止；前台模式不会自动打开浏览器'
        Write-Host "  地址: $UiUrl" -ForegroundColor White
        Write-Host "  界面状态: $stateFile" -ForegroundColor Gray
        & $Python @arguments
        exit $LASTEXITCODE
    }

    Write-Step '启动 Streamlit 控制台'
    Write-Info '后台运行，日志: outputs\logs\streamlit-ui.log'
    $proc = Start-Process -FilePath $Python `
                          -ArgumentList $arguments `
                          -WorkingDirectory $Root `
                          -RedirectStandardOutput $StdOutLog `
                          -RedirectStandardError $StdErrLog `
                          -WindowStyle Hidden `
                          -PassThru
    if (-not $proc) { throw '无法启动 Streamlit 控制台进程' }
    Save-PidFile -ProcessId $proc.Id
    Write-Info "PID $($proc.Id)，等待健康检查 /_stcore/health（最多 $TimeoutSeconds 秒）"

    if (-not (Wait-StreamlitHealth -Timeout $TimeoutSeconds)) {
        Write-Err "Streamlit 未在 $TimeoutSeconds 秒内通过健康检查"
        Write-Host '  --- outputs\logs\streamlit-ui.err.log (最后 25 行) ---' -ForegroundColor DarkGray
        $tail = @()
        $tail += Get-Content -Path $StdErrLog -Tail 25 -ErrorAction SilentlyContinue
        $tail += Get-Content -Path $StdOutLog -Tail 25 -ErrorAction SilentlyContinue
        if ($tail) { $tail | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray } }
        Write-Info '完整日志: outputs\logs\streamlit-ui.log / streamlit-ui.err.log'
        Stop-ProcessTree -ProcessId $proc.Id -Label 'Streamlit 控制台 (启动失败)'
        Remove-PidFile
        exit 1
    }

    Show-StreamlitInfo -ProcessId $proc.Id
    if (-not $NoBrowser) {
        try { Start-Process $UiUrl | Out-Null }
        catch { Write-Warn "无法自动打开浏览器，请手动访问 $UiUrl" }
    }
    exit 0
}
catch {
    Write-Host ''
    Write-Err $_.Exception.Message
    Write-Host '  可用 .\start-ui.ps1 -Stop 清理进程后重试。' -ForegroundColor Yellow
    exit 1
}
