<#
.SYNOPSIS
    一键启动 medical-rag-resume 的本机演示：RAG 后端 + 患者问答门户。

.DESCRIPTION
    单一入口：启动真正的 RAG 答案服务（本机 8001），再启动需要机构登录的
    ChatGPT 式患者门户（https://localhost:8443），最后打开浏览器。

    患者门户使用虚构演示环境：Keycloak、一次性 PostgreSQL、自签 TLS 证书。
    首次启动或容器未运行时，脚本会调用 outputs/patient-demo/setup.py 重建
    演示环境（约 1-2 分钟），并写入演示账号到 credentials.txt。

    安全边界：8001 的 unsafe-local-demo 没有任何登录或患者隔离，只允许本机；
    患者门户的登录、授权、问答落库才走 PostgreSQL 与 OIDC。两者都不得转发、
    不得接入反向代理、不得处理真实患者数据。

.PARAMETER Stop
    停止本脚本启动的患者门户与 RAG API（保留 Docker 容器与数据）。

.PARAMETER StopAll
    停止进程，并删除患者演示容器/网络、关闭 docker compose 服务。

.PARAMETER ResetPatient
    强制重建患者演示环境（PostgreSQL/Keycloak/账号/配置）。

.PARAMETER SkipFrontend
    跳过患者前端构建检查，直接使用现有 frontend/dist-patient。

.PARAMETER WithWorkers
    同时启动 relay / ingest / memory 后台工作进程（文档入库与记忆需要）。

.PARAMETER NoBrowser
    启动完成后不自动打开浏览器。

.EXAMPLE
    .\start.ps1

.EXAMPLE
    .\start.ps1 -ResetPatient -WithWorkers

.EXAMPLE
    .\start.ps1 -Stop

.EXAMPLE
    .\start.ps1 -StopAll
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$StopAll,
    [switch]$ResetPatient,
    [switch]$SkipFrontend,
    [switch]$WithWorkers,
    [switch]$NoBrowser,
    [int]$DockerTimeoutSeconds = 240,
    [int]$PatientTimeoutSeconds = 240
)

# Windows PowerShell 5.1 turns a native tool's stderr into a terminating
# NativeCommandError when this is 'Stop'. Keep 'Continue' and throw explicitly
# after checking exit codes, so docker/npm progress text cannot abort startup.
$ErrorActionPreference = 'Continue'

$Root      = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunDir    = Join-Path $Root 'outputs\run'
$LogDir    = Join-Path $Root 'outputs\logs'
$Python    = Join-Path $Root '.venv\Scripts\python.exe'
$Frontend  = Join-Path $Root 'frontend'
$DemoDir   = Join-Path $Root 'outputs\patient-demo'
$PatientCa = Join-Path $DemoDir 'certs\ca.pem'
$PatientUrl = 'https://localhost:8443/'
$ApiPort   = 8001
$ApiUrl    = "http://127.0.0.1:$ApiPort"
$PatientPort = 8443
$AccessMode = 'unsafe-local-demo'

$script:Warnings = 0
function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "  [OK]   $Message" -ForegroundColor Green }
function Write-Info([string]$Message) { Write-Host "  [..]   $Message" -ForegroundColor Gray }
function Write-Warn([string]$Message) { $script:Warnings++; Write-Host "  [WARN] $Message" -ForegroundColor Yellow }
function Write-Err([string]$Message)  { Write-Host "  [FAIL] $Message" -ForegroundColor Red }

# ---------- 通用工具 ----------
function Test-TcpPort {
    param([int]$Port, [string]$Target = '127.0.0.1')
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($Target, $Port, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne(700) -and $client.Connected) { return $true }
        return $false
    } catch { return $false } finally { $client.Close() }
}

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

function Get-DockerPath {
    $cmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Test-DockerDaemon {
    param([string]$Docker)
    if (-not $Docker) { return $false }
    $null = & $Docker info --format '{{.ServerVersion}}' 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Start-DockerDesktop {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'),
        (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe')
    )
    foreach ($exe in $candidates) {
        if (Test-Path $exe) {
            Write-Info "启动 Docker Desktop: $exe"
            Start-Process -FilePath $exe | Out-Null
            return $true
        }
    }
    return $false
}

function Save-PidFile {
    param([string]$Name, [int]$ProcessId)
    if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Path $RunDir -Force | Out-Null }
    Set-Content -Path (Join-Path $RunDir "$Name.pid") -Value $ProcessId -Encoding ascii
}

function Get-SavedProcessId {
    param([string]$Name)
    $file = Join-Path $RunDir "$Name.pid"
    if (-not (Test-Path $file)) { return $null }
    $text = (Get-Content -Path $file -ErrorAction SilentlyContinue | Select-Object -First 1)
    $parsed = 0
    if ([int]::TryParse([string]$text, [ref]$parsed)) { return $parsed }
    return $null
}

function Remove-PidFile {
    param([string]$Name)
    $file = Join-Path $RunDir "$Name.pid"
    if (Test-Path $file) { Remove-Item $file -Force -ErrorAction SilentlyContinue }
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

function Start-BackgroundProcess {
    param(
        [string]$Name,
        [string[]]$Arguments,
        [string]$Description
    )
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    $stdout = Join-Path $LogDir "$Name.log"
    $stderr = Join-Path $LogDir "$Name.err.log"
    $proc = Start-Process -FilePath $Python `
                          -ArgumentList $Arguments `
                          -WorkingDirectory $Root `
                          -RedirectStandardOutput $stdout `
                          -RedirectStandardError $stderr `
                          -WindowStyle Hidden `
                          -PassThru
    if (-not $proc) { throw "无法启动 $Description" }
    Save-PidFile -Name $Name -ProcessId $proc.Id
    Write-Ok "$Description 已启动 (PID $($proc.Id))"
    Write-Info "日志: outputs\logs\$Name.log"
    return $proc
}

# ---------- 停止 ----------
function Stop-PatientPortal {
    $procId = Get-SavedProcessId -Name 'patient-portal'
    if ($procId) {
        $cmd = Get-CommandLine -ProcessId $procId
        if ($cmd -and $cmd -match 'demo_launcher\.py') {
            Stop-ProcessTree -ProcessId $procId -Label '患者门户'
        } else {
            Write-Warn "PID 文件指向的进程不是患者门户，忽略 (PID $procId)"
        }
        Remove-PidFile -Name 'patient-portal'
    }
    $listener = Get-ListeningProcessId -Port $PatientPort
    if ($listener) {
        $cmd = Get-CommandLine -ProcessId $listener
        if ($cmd -and $cmd -match 'demo_launcher\.py') {
            Stop-ProcessTree -ProcessId $listener -Label '患者门户 (残留)'
        } else {
            Write-Warn "端口 $PatientPort 被其他进程占用 (PID $listener)，未自动结束"
        }
    }
}

function Stop-LegacyApi {
    foreach ($name in @('api', 'relay', 'ingest', 'memory')) {
        $procId = Get-SavedProcessId -Name $name
        if ($procId) {
            $cmd = Get-CommandLine -ProcessId $procId
            if ($cmd -and $cmd -match 'medical_rag') {
                Stop-ProcessTree -ProcessId $procId -Label $name
            } else {
                Write-Warn "PID 文件指向的进程不是 $name，忽略 (PID $procId)"
            }
            Remove-PidFile -Name $name
        }
    }
    $listener = Get-ListeningProcessId -Port $ApiPort
    if ($listener) {
        $cmd = Get-CommandLine -ProcessId $listener
        if ($cmd -and $cmd -match 'medical_rag|uvicorn') {
            Stop-ProcessTree -ProcessId $listener -Label 'api (残留)'
        } else {
            Write-Warn "端口 $ApiPort 被其他进程占用 (PID $listener)，未自动结束"
        }
    }
}

function Invoke-Stop {
    Write-Step '停止本机演示进程'
    Stop-PatientPortal
    Stop-LegacyApi

    if ($StopAll) {
        Write-Step '清理患者演示容器与 Compose 服务'
        $docker = Get-DockerPath
        if ($docker -and (Test-DockerDaemon -Docker $docker)) {
            & $docker rm -f patient-demo-kc patient-demo-pg 2>$null | Out-Null
            & $docker network rm patient-demo-net 2>$null | Out-Null
            Push-Location $Root
            try {
                $downOut = & $docker compose down 2>&1
                $downOut | ForEach-Object { Write-Info $_ }
            }
            finally { Pop-Location }
            Write-Ok '患者容器已删除，Compose 服务已关闭（数据卷保留）'
        } else {
            Write-Warn 'Docker 未运行，跳过容器清理'
        }
    } else {
        Write-Info 'Docker 容器与数据保留；下次启动会复用'
    }

    Write-Host ''
    if ($script:Warnings -gt 0) { Write-Host "停止完成，$($script:Warnings) 条警告。" -ForegroundColor Yellow }
    else { Write-Host '停止完成。' -ForegroundColor Green }
}

# ---------- 前置检查 ----------
function Assert-Prerequisites {
    Write-Step '检查运行环境'

    if (-not (Test-Path $Python)) {
        Write-Err "未找到虚拟环境解释器: $Python"
        Write-Host '  请先执行：' -ForegroundColor Yellow
        Write-Host '    python -m venv .venv' -ForegroundColor Yellow
        Write-Host '    .\.venv\Scripts\python.exe -m pip install -e ".[dev]"' -ForegroundColor Yellow
        throw '缺少 .venv'
    }
    Write-Ok "Python: $(& $Python --version 2>&1)"

    & $Python -c 'import medical_rag, httpx, httpx2' 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Info '安装 Python 依赖（首次或依赖变更时较慢）'
        Push-Location $Root
        try {
            & $Python -m pip install --no-user -e '.[dev]'
            if ($LASTEXITCODE -ne 0) { throw 'pip install 失败' }
        }
        finally { Pop-Location }
    }
    Write-Ok 'Python 依赖可导入'

    foreach ($file in @('outputs\patient-demo\setup.py', 'outputs\patient-demo\demo_launcher.py')) {
        if (-not (Test-Path (Join-Path $Root $file))) { throw "缺少患者演示脚本: $file" }
    }
    Write-Ok '患者演示脚本存在'

    if (-not (Test-Path (Join-Path $Root '.env'))) {
        Write-Warn '.env 不存在，RAG 模型通常无法调用'
    }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        Write-Warn '未找到 Node.js，患者前端必须已构建'
    }
}

# ---------- Docker ----------
function Ensure-Docker {
    Write-Step '检查 Docker'
    $docker = Get-DockerPath
    if (-not $docker) { throw '未找到 docker 命令，请安装 Docker Desktop' }

    if (-not (Test-DockerDaemon -Docker $docker)) {
        Write-Info 'Docker 守护进程未运行，尝试启动 Docker Desktop'
        if (-not (Start-DockerDesktop)) {
            throw '无法自动启动 Docker Desktop，请手动启动后重试'
        }
        $deadline = (Get-Date).AddSeconds($DockerTimeoutSeconds)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 3
            if (Test-DockerDaemon -Docker $docker) { Write-Ok 'Docker 守护进程已就绪'; return }
            Write-Host '.' -NoNewline
        }
        Write-Host ''
        throw "Docker 守护进程在 $DockerTimeoutSeconds 秒内未就绪"
    }
    Write-Ok 'Docker 守护进程运行中'
}

function Ensure-ComposeStack {
    Write-Step '启动检索依赖（Milvus / RabbitMQ）'
    $docker = Get-DockerPath
    $services = @()
    Push-Location $Root
    try {
        $services = @(& $docker compose config --services 2>$null | Where-Object { $_ })
        Write-Info "服务: $($services -join ', ')"
        # docker writes progress to stderr; with Continue this is display-only.
        $composeOut = & $docker compose up -d 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'docker compose up 失败' }
        $composeOut | ForEach-Object { Write-Info $_ }

        $deadline = (Get-Date).AddSeconds($DockerTimeoutSeconds)
        $pending = $services
        while ((Get-Date) -lt $deadline) {
            $ids = @(& $docker compose ps -q 2>$null | Where-Object { $_ })
            $notReady = @()
            foreach ($id in $ids) {
                $state = (& $docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $id 2>$null)
                $state = ([string]$state).Trim()
                if ($state -notin @('healthy', 'running')) { $notReady += "$id=$state" }
            }
            if ($ids.Count -ge $services.Count -and $notReady.Count -eq 0) {
                Write-Ok "全部容器就绪 ($($services.Count) 个)"
                $pending = @()
                break
            }
            $pending = $notReady
            Start-Sleep -Seconds 5
        }
        if ($pending.Count -gt 0) { Write-Warn "仍有容器未就绪: $($pending -join ', ')" }
    }
    finally { Pop-Location }
}

# ---------- 患者前端 ----------
function Test-PatientBuildNeeded {
    $html = Join-Path $Frontend 'dist-patient\patient.html'
    if (-not (Test-Path $html)) { return $true }
    $builtAt = (Get-Item $html).LastWriteTimeUtc
    $inputs = @()
    foreach ($path in @('src\patient', 'patient.html', 'vite.patient.config.ts', 'package.json', 'tsconfig.json')) {
        $full = Join-Path $Frontend $path
        if (Test-Path $full) {
            if ((Get-Item $full).PSIsContainer) {
                $inputs += Get-ChildItem -Path $full -Recurse -File -ErrorAction SilentlyContinue
            } else {
                $inputs += Get-Item $full
            }
        }
    }
    foreach ($item in $inputs) {
        if ($item.LastWriteTimeUtc -gt $builtAt) { return $true }
    }
    return $false
}

function Ensure-PatientFrontend {
    if ($SkipFrontend) {
        Write-Step '患者前端'
        Write-Info '已指定 -SkipFrontend，跳过构建检查'
        if (-not (Test-Path (Join-Path $Frontend 'dist-patient\patient.html'))) {
            throw 'frontend/dist-patient 不存在，请先运行 npm --prefix frontend run build:patient'
        }
        return
    }

    Write-Step '构建患者前端'
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        if (Test-Path (Join-Path $Frontend 'dist-patient\patient.html')) {
            Write-Warn '未找到 npm，继续使用现有 frontend/dist-patient'
            return
        }
        throw '未找到 npm，且 frontend/dist-patient 不存在'
    }
    if (-not (Test-Path (Join-Path $Frontend 'node_modules'))) {
        Write-Info '安装前端依赖'
        Push-Location $Root
        try {
            & npm --prefix frontend install
            if ($LASTEXITCODE -ne 0) { throw 'npm install 失败' }
        }
        finally { Pop-Location }
    }
    if (-not (Test-PatientBuildNeeded)) {
        Write-Ok 'frontend/dist-patient 已是最新'
        return
    }
    Push-Location $Root
    try {
        & npm --prefix frontend run build:patient
        if ($LASTEXITCODE -ne 0) { throw '患者前端构建失败' }
    }
    finally { Pop-Location }
    Write-Ok '患者前端构建完成'
}

# ---------- RAG 后端 ----------
function Start-LegacyApi {
    Write-Step '启动 RAG 答案服务（本机 8001）'
    $listener = Get-ListeningProcessId -Port $ApiPort
    if ($listener) {
        $cmd = Get-CommandLine -ProcessId $listener
        if ($cmd -match 'medical_rag|uvicorn') {
            Save-PidFile -Name 'api' -ProcessId $listener
            Write-Ok "RAG API 已在运行 (PID $listener)，复用"
            return
        }
        throw "端口 $ApiPort 被其他进程占用 (PID $listener)；请先运行 .\start.ps1 -Stop"
    }

    $env:MEDICAL_RAG_ACCESS_MODE = $AccessMode
    $null = Start-BackgroundProcess -Name 'api' `
        -Description 'RAG API (uvicorn)' `
        -Arguments @('-m', 'uvicorn', 'medical_rag.api:app', '--app-dir', 'src',
                     '--host', '127.0.0.1', '--port', "$ApiPort")

    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        try {
            $health = Invoke-RestMethod -Uri "$ApiUrl/health" -TimeoutSec 5
            if ($health.success) {
                Write-Ok 'RAG API 健康检查通过'
                return
            }
        } catch { }
    }
    $tail = Get-Content -Path (Join-Path $LogDir 'api.err.log') -Tail 20 -ErrorAction SilentlyContinue
    if ($tail) { $tail | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray } }
    throw 'RAG API 未在 120 秒内通过健康检查'
}

function Start-Workers {
    if (-not $WithWorkers) {
        Write-Step '后台工作进程'
        Write-Info '默认跳过（问答不需要）；需要文档入库/记忆时加 -WithWorkers'
        return
    }
    Write-Step '启动后台工作进程'
    $env:MEDICAL_RAG_ACCESS_MODE = $AccessMode
    foreach ($role in @('relay', 'ingest', 'memory')) {
        $saved = Get-SavedProcessId -Name $role
        if ($saved -and (Get-Process -Id $saved -ErrorAction SilentlyContinue)) {
            Write-Ok "$role 已在运行 (PID $saved)，复用"
            continue
        }
        $null = Start-BackgroundProcess -Name $role `
            -Description "worker $role" `
            -Arguments @('-m', 'medical_rag.worker', $role)
    }
}

# ---------- 患者演示环境 ----------
function Test-ContainerRunning {
    param([string]$Name)
    $docker = Get-DockerPath
    $state = & $docker inspect --format '{{.State.Running}}' $Name 2>$null
    return (([string]$state).Trim() -eq 'true')
}

function Test-PatientDatabase {
    $config = Join-Path $DemoDir 'config.toml'
    if (-not (Test-Path $config)) { return $false }
    $code = @'
import pathlib, sys, tomllib, psycopg
base = pathlib.Path('outputs/patient-demo')
cfg = tomllib.loads((base / 'config.toml').read_text(encoding='utf-8'))
dsn = (base / cfg['database_dsn_file']).read_text(encoding='utf-8').strip()
try:
    with psycopg.connect(dsn, connect_timeout=3) as db:
        db.execute('select 1').fetchone()
except Exception:
    sys.exit(1)
'@
    & $Python -c $code 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Invoke-PatientSetup {
    Write-Step '初始化患者演示环境'
    Write-Info '重建一次性 PostgreSQL、Keycloak、演示账号与配置（约 1-2 分钟）'
    Stop-PatientPortal
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    $log = Join-Path $LogDir 'patient-setup.log'
    $err = Join-Path $LogDir 'patient-setup.err.log'
    $proc = Start-Process -FilePath $Python `
                          -ArgumentList (Join-Path $DemoDir 'setup.py') `
                          -WorkingDirectory $Root `
                          -RedirectStandardOutput $log `
                          -RedirectStandardError $err `
                          -WindowStyle Hidden `
                          -Wait `
                          -PassThru
    if (-not $proc) { throw '无法启动患者演示初始化进程' }
    if ($proc.ExitCode -ne 0) {
        $tail = @()
        $tail += Get-Content -Path $log -Tail 15 -ErrorAction SilentlyContinue
        $tail += Get-Content -Path $err -Tail 15 -ErrorAction SilentlyContinue
        if ($tail) { $tail | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray } }
        throw '患者演示环境初始化失败（详见 outputs\logs\patient-setup.log）'
    }
    Write-Ok '患者演示环境已就绪'
}

function Ensure-PatientEnvironment {
    Write-Step '检查患者演示环境'
    $running = (Test-ContainerRunning -Name 'patient-demo-pg') -and (Test-ContainerRunning -Name 'patient-demo-kc')
    if (-not $ResetPatient -and $running -and (Test-PatientDatabase)) {
        Write-Ok '患者 PostgreSQL / Keycloak 容器已在运行，数据库凭据有效'
        return
    }
    if ($running -and -not $ResetPatient) {
        Write-Warn '患者容器在运行，但数据库凭据不匹配（可能容器被重建过）；自动重建演示环境'
    } elseif (-not $ResetPatient) {
        Write-Info '患者容器未运行，重建演示环境'
    }
    Invoke-PatientSetup
    if (-not (Test-PatientDatabase)) {
        throw '患者数据库初始化后仍无法连接；请运行 .\start.ps1 -ResetPatient 后重试'
    }
    Write-Ok '患者数据库凭据校验通过'
}

function Wait-PatientHttps {
    $ca = $PatientCa.Replace('\', '/')
    $code = "import sys, httpx; r = httpx.get('$PatientUrl', verify=r'$ca', trust_env=False, timeout=3); sys.exit(0 if r.status_code == 200 else 1)"
    $deadline = (Get-Date).AddSeconds($PatientTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        & $Python -c $code 2>$null
        if ($LASTEXITCODE -eq 0) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Start-PatientPortal {
    Write-Step '启动患者问答门户（8443）'
    $listener = Get-ListeningProcessId -Port $PatientPort
    if ($listener) {
        $cmd = Get-CommandLine -ProcessId $listener
        if ($cmd -match 'demo_launcher\.py') {
            Save-PidFile -Name 'patient-portal' -ProcessId $listener
            Write-Ok "患者门户已在运行 (PID $listener)，复用"
            return
        }
        throw "端口 $PatientPort 被其他进程占用 (PID $listener)；请先运行 .\start.ps1 -Stop"
    }

    $null = Start-BackgroundProcess -Name 'patient-portal' `
        -Description '患者门户 (HTTPS)' `
        -Arguments @('outputs\patient-demo\demo_launcher.py')

    if (-not (Wait-PatientHttps)) {
        $tail = Get-Content -Path (Join-Path $LogDir 'patient-portal.err.log') -Tail 30 -ErrorAction SilentlyContinue
        if ($tail) { $tail | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray } }
        throw '患者门户未在限定时间内通过 HTTPS 健康检查'
    }
    Write-Ok "患者门户可用: $PatientUrl"
}

# ---------- 汇总 ----------
function Show-Summary {
    Write-Step '启动完成'
    Write-Host ''
    Write-Host '  问答入口（需机构登录）: ' -NoNewline
    Write-Host $PatientUrl -ForegroundColor White
    Write-Host '  RAG 答案服务（本机，不直接打开）: ' -NoNewline
    Write-Host $ApiUrl -ForegroundColor Gray
    Write-Host '  演示账号: outputs\patient-demo\credentials.txt' -ForegroundColor Gray
    Write-Host '  停止进程: .\start.ps1 -Stop' -ForegroundColor Gray
    Write-Host '  完全清理: .\start.ps1 -StopAll' -ForegroundColor Gray
    Write-Host ''
    Write-Host '  登录后直接进入问答页面；个人信息在左下角头像菜单。' -ForegroundColor Yellow
    Write-Host '  首次访问需在浏览器接受演示自签证书（仅限本机）。' -ForegroundColor Yellow
    Write-Host '  仅限本机虚构数据演示，禁止转发或处理真实患者数据。' -ForegroundColor Yellow
    if ($script:Warnings -gt 0) {
        Write-Host "`n  启动完成，共 $($script:Warnings) 条警告，请检查上方 [WARN] 行。" -ForegroundColor Yellow
    }
}

# ---------- 主流程 ----------
try {
    if ($Stop -or $StopAll) {
        Invoke-Stop
        return
    }

    Write-Host '医疗资料 RAG 问答 — 本机患者演示' -ForegroundColor White
    Write-Host "项目目录: $Root" -ForegroundColor DarkGray

    Assert-Prerequisites
    Ensure-Docker
    Ensure-ComposeStack
    Ensure-PatientFrontend
    Start-LegacyApi
    Ensure-PatientEnvironment
    Start-PatientPortal
    Start-Workers
    Show-Summary

    if (-not $NoBrowser) {
        try { Start-Process $PatientUrl | Out-Null }
        catch { Write-Warn "无法自动打开浏览器，请手动访问 $PatientUrl" }
    }
}
catch {
    Write-Host ''
    Write-Err $_.Exception.Message
    Write-Host '  启动中止。可用 .\start.ps1 -Stop 清理已启动的进程后重试。' -ForegroundColor Yellow
    exit 1
}
