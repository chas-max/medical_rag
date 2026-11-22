# Streamlit 持久化展示界面

`src/medical_rag/ui/` 是一个**可选、只读**的本机观察界面：它展示本项目已经写入本机的
持久化数据（会话历史、文档入库任务、评测报告），并把界面自身的视图状态（当前页面、
上次选中的会话/报告、操作员笔记）保存到独立的 SQLite 文件，重启后恢复上次视图。

它**不是患者门户，也不替代 React 前端**：不提供登录、授权、会话、上传或问答，不读取
患者 PostgreSQL，也不展示 `.env` 中的模型密钥。默认只监听 `127.0.0.1:8501`，属于开发
与运维观察用途。

> 验证状态：该界面是开发增量，**尚未通过生产准入**，只能用于本机虚构数据。
> 本文描述当前分支的接口；实际行为以 `src/medical_rag/ui/`、`start-ui.ps1` 和
> `docker-compose.streamlit.yml` 为准。具体版本号、测试通过数量不写入本文，按第 8 节
> 的命令自行核对。

## 1. 目标与边界

- **只读展示已持久化数据**：界面读取的是 RAG 服务与 worker 已经写入磁盘的内容，不在
  界面里发起问答、入库或评测。
- **不是患者门户**：患者入口（`https://localhost:8443/`）的账号、身份、健康信息授权、
  PostgreSQL 会话与文字记录都不在本界面出现；本界面连患者数据库都不连接。
- **不替代 React 前端**：`http://127.0.0.1:8001/ui/chat` 等旧界面仍由 React 提供；
  Streamlit 只做本机观察，不托管旧聊天页，也不改变任何现有路由。
- **无鉴权**：没有登录、角色或患者隔离。因此只能监听本机回环地址，不得转发、反向代理、
  内网穿透或映射端口到非回环地址。
- **写边界**：对项目数据的访问一律只读；界面唯一写入项目的文件是自己的状态库
  `data/streamlit/ui-state.sqlite3`（页面、选择、笔记）。
- **数据边界**：仅用于本机虚构数据与运维观察，不得用于真实患者数据；不显示 API Key、
  连接串或密钥内容。

## 2. 展示内容

侧栏提供五个页面：**概览 / 会话历史 / 文档任务 / 评测报告 / 界面笔记**，另有
「刷新数据」按钮（清除 15 秒读缓存，立即重读磁盘）和「恢复默认视图」按钮（清除页面
与选择，保留笔记）。

| 页面/区块 | 展示内容 | 数据来源（只读） |
|---|---|---|
| 概览 | 会话数、轮次数、已回答数、文档任务数与待处理数、历史库文件大小与更新时间、本机 RAG `/health` 探测（状态、Milvus、消息队列）、最近会话、最近评测报告、worker 心跳 | `data/chat-history.sqlite3`、`UI_API_BASE_URL` |
| 会话历史 | 会话筛选与列表；单会话的消息、引用、拒答/澄清标记、失败运行；会话笔记 | `conversations`、`turns` 表 |
| 文档任务 | 任务 ID、文件名、来源、状态、阶段、尝试次数、错误、创建/更新时间，可按状态筛选并展开失败详情 | `document_jobs` 表 |
| 评测报告 | 报告列表与详情：`dataset_version`、题目数、答案/裁判模型、`summary` 指标（Precision、Recall、NDCG、MRR、Hit Rate、忠实度、答案相关性、上下文相关性、拒答率、正确拒答率、裁判失败数）、可筛选的逐题结果、下载报告 JSON；报告笔记 | `UI_REPORT_DIRS` 下的报告 JSON |
| 界面笔记 | 所有笔记的列表、按会话/报告新增与删除 | `data/streamlit/ui-state.sqlite3` 的 `ui_notes` 表 |

空数据是正常状态，不报错：

- `chat-history.sqlite3` 不存在、为空或缺少相应表时，相应区块显示空态（会话、任务、
  心跳均为 0/空），并提示启动本机演示产生数据；
- `UI_REPORT_DIRS` 下没有可识别报告时显示空态，并给出生成报告的命令示例；
- 后端 `/health` 探测不可达时只把该指标标为不可达，不影响其它只读数据展示。

## 3. 数据来源与只读保证

- **会话历史与文档任务**：`data.py` 用 SQLite URI 的 `mode=ro` 打开
  `data/chat-history.sqlite3`，并在读取前检查表是否存在。界面不会建表、迁移或写入任何
  一行；数据库文件不存在或不可读时直接返回空结果，而不是抛异常页面。
- **评测报告**：只读取普通 JSON 文件。扫描按 `UI_REPORT_DIRS` 配置的目录递归查找
  `*.json`，只收录**顶层带 `summary` 对象**、看起来像评测报告的文件；无法解析或结构
  不符的文件被跳过。单个报告文件上限 200 MiB，超过时拒绝在界面中打开。报告由
  `python -m medical_rag.evaluate --output <报告.json>` 生成，界面不运行评测。
- **后端健康探测**：通过 `UI_API_BASE_URL` 请求 `/health`，只接受 `http(s)` 且主机为
  `127.0.0.1` / `localhost` / `::1` 的回环地址，短超时且不读取代理环境变量。探测失败
  只影响该指标，不阻塞页面。
- **不读取的内容**：患者 PostgreSQL 与患者入口配置不在读取范围。配置沿用项目
  `Settings`，但界面只使用 `CHAT_HISTORY_FILE`、`UI_STATE_FILE`、`UI_REPORT_DIRS`、
  `UI_API_BASE_URL` 这几个路径/地址项，不展示、不输出 API Key 或数据库连接串等密钥
  内容。健康探测只解析 `/health` 的响应体。

## 4. 持久化机制

Streamlit 的 `session_state` 在进程重启后会丢失，因此界面把需要跨重启保留的状态写入
独立的状态库：

- 默认路径：`data/streamlit/ui-state.sqlite3`（可用 `UI_STATE_FILE` 覆盖；相对路径按
  项目根目录解析）；
- `ui_state` 表：键值对，值为 JSON 文本，当前使用 `ui.page`（页面）、
  `ui.conversation`（上次选中会话）、`ui.report`（上次选中报告）；
- `ui_notes` 表：运维笔记，按对象类型与 ID 关联并记录创建/更新时间。状态层支持
  `conversation` / `report` / `document` 三种类型，当前界面入口提供会话与报告笔记；
  可新增、删除单条笔记；
- 上限：状态键 ≤200 字符、单项状态 ≤64 KiB、笔记正文 ≤4000 字符、关联对象 ID ≤512
  字符；数据库 `busy_timeout` 为 5 秒，多标签页共享同一个状态库。

行为约定：

- 重启界面进程后，按上次保存的页面与选中项恢复视图；笔记持久保留；
- 状态库不可读写（路径错误、目录权限不足、磁盘满）时，侧栏显示一次明确错误，提示检查
  `UI_STATE_FILE` 的路径、权限和磁盘空间；页面退回默认视图，只读数据仍可正常查看；
- Docker 方式：`docker-compose.streamlit.yml` 使用 `restart: unless-stopped`，只绑定
  `127.0.0.1:8501`，数据目录只读挂载，界面状态目录使用命名卷，因此 `up -d` / `down`
  循环不会丢失状态库；
- 日志：`start-ui.ps1` 后台启动时把输出写到 `outputs/logs/streamlit-ui.log` 与
  `outputs/logs/streamlit-ui.err.log`，PID 文件为 `outputs/run/streamlit-ui.pid`；
  `-Foreground` 直接输出到当前终端；界面容器的日志用
  `docker compose -f docker-compose.streamlit.yml logs -f` 查看。

## 5. 启动方式

先确认已安装项目依赖与虚拟环境。`.[ui]` 是可选依赖（`streamlit` 版本范围见
`pyproject.toml`），不安装时其余功能不受影响。

### 5.1 安装可选依赖（首次）

本仓库的 `.venv` 由 uv 创建（通常没有 `pip`），因此首选：

```powershell
uv pip install --python .venv\Scripts\python.exe -e ".[ui]"
```

若虚拟环境自带 `pip`，也可以用：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[ui]"
```

确认 Streamlit 可用（不要以本文档中的版本号为准）：

```powershell
.\.venv\Scripts\python.exe -c "import streamlit; print(streamlit.__version__)"
```

### 5.2 直接运行 Python 入口

```powershell
.\.venv\Scripts\python.exe -m medical_rag.ui
```

默认监听 `127.0.0.1:8501`，启动后约 2.5 秒自动打开浏览器，也可直接访问
`http://127.0.0.1:8501/`。参数：

| 参数 | 说明 |
|---|---|
| `--host` | 监听地址，只允许回环地址（`127.0.0.1` / `localhost` / `::1`），默认 `127.0.0.1` |
| `--port` | 监听端口，默认 `8501` |
| `--no-browser` | 启动后不自动打开浏览器 |
| `--state-file` | 覆盖界面状态库路径（等价 `UI_STATE_FILE`） |
| `--history-file` | 覆盖只读会话/任务库路径（等价 `CHAT_HISTORY_FILE`） |
| `--reports-dir` | 覆盖评测报告目录（等价 `UI_REPORT_DIRS`），可重复传入多个目录 |

### 5.3 使用启动器 `start-ui.ps1`

```powershell
.\start-ui.ps1                # 后台启动（默认 127.0.0.1:8501），就绪后打开浏览器
.\start-ui.ps1 -Install       # 缺少 Streamlit 时允许用 uv 自动安装 .[ui] 依赖
.\start-ui.ps1 -Port 8502     # 更换端口
.\start-ui.ps1 -NoBrowser     # 不自动打开浏览器
.\start-ui.ps1 -Foreground    # 前台运行，Ctrl+C 停止；不写 PID、不打开浏览器
.\start-ui.ps1 -Status        # 查看运行状态、PID、URL、状态文件与健康检查
.\start-ui.ps1 -Stop          # 停止启动器拉起的进程，保留状态库
.\start-ui.ps1 -TimeoutSeconds 120   # 后台启动后等待健康检查的秒数（默认 90）
```

说明：

- 启动器只接受本机回环绑定；端口已被**其他程序**占用时会直接报错并建议用 `-Port`
  换端口，不会结束来路不明的进程；
- 后台启动后轮询 `/_stcore/health`，通过后打印地址、状态文件与日志路径；失败时输出
  最后若干行日志并结束本次启动；
- 检测到缺少 Streamlit 且本机有 `uv` 时，启动器会尝试自动安装 `.[ui]` 依赖；显式加
  `-Install` 表示允许这一行为，没有 uv 时会给出需要手动执行的安装命令；
- 与 `start.ps1` 一样，`-Stop` 只结束进程，不删除数据；需要删除界面状态见第 7 节。

### 5.4 随一键启动一起启动

```powershell
.\start.ps1 -WithStreamlit
```

在启动 RAG 答案服务之后，额外启动 `127.0.0.1:8501` 的展示界面（不自动打开该页面；
启动完成的汇总里若健康检查通过会列出地址）。注意：

- 一键启动**不会自动安装** Streamlit：缺少 `.[ui]` 依赖时会报错并中止本次启动，其他
  已启动的进程需要 `.\start.ps1 -Stop` 清理。请先按 5.1 节安装；
- `.\start.ps1 -Stop` / `-StopAll` 会一并停止由它启动的 Streamlit 进程；
- 它**不会**替代 `https://localhost:8443/` 的患者门户，两者用途不同。

### 5.5 Docker Compose

```powershell
docker compose -f docker-compose.streamlit.yml up -d    # 启动
docker compose -f docker-compose.streamlit.yml logs -f  # 查看日志
docker compose -f docker-compose.streamlit.yml down     # 停止并删除容器
```

- 首次 `up -d` 会按 `deploy/streamlit/Dockerfile` 构建 `medical-rag-ui:dev` 镜像
  （`python:3.11-slim` + 最小运行依赖），需要 Docker 能访问 Python 包源；构建失败时先
  检查网络/镜像源；
- 容器配置为 `restart: unless-stopped` 并带 `/_stcore/health` 健康检查，宿主机端口只
  绑定 `127.0.0.1:8501`；容器以只读根文件系统、非 root 用户运行（仅 `/tmp` 可写）。
  容器内进程监听 `0.0.0.0:8501` 以配合 Docker 端口映射（映射转发到容器网卡地址而非
  容器回环），但宿主机发布端口限制在 `127.0.0.1`，不会暴露到局域网；
- `./data` 与 `./outputs` 以 `:ro` 只读挂载，界面不能写问答库与报告；`/app/data/streamlit`
  单独用命名卷 `medical_rag_ui_state` 覆盖为可写，界面状态（页面、选择、笔记）因此能跨
  容器重建保留。`down` 保留命名卷，`down -v` 才会删除状态（含笔记）；
- 容器内通过环境变量使用 `UI_STATE_FILE=/app/data/streamlit/ui-state.sqlite3`、
  `CHAT_HISTORY_FILE=/app/data/chat-history.sqlite3`、`UI_REPORT_DIRS=/app/outputs/reports`；
- 容器只包含界面本身，不包含 RAG 服务。健康探测只接受回环地址，而容器内的
  `127.0.0.1:8001` 指向容器自身、且 compose 未注入 `UI_API_BASE_URL`，因此容器里该指标
  通常显示不可达，这不影响只读数据展示；需要探测宿主机后端时，直接在宿主机运行
  `python -m medical_rag.ui` 或 `start-ui.ps1`。

## 6. 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `UI_STATE_FILE` | `data/streamlit/ui-state.sqlite3` | 界面状态库；相对路径按项目根目录解析 |
| `UI_REPORT_DIRS` | `outputs/reports` | 评测报告目录，逗号/分号/路径分隔符分隔；相对路径按项目根目录解析；可配置多个 |
| `UI_API_BASE_URL` | `http://127.0.0.1:8001` | RAG 后端 `/health` 探测地址；只接受回环 `http(s)` 地址 |
| `CHAT_HISTORY_FILE` | `data/chat-history.sqlite3` | 会话历史库路径，与 RAG 服务、worker 共用；界面只读打开 |
| `STREAMLIT_SERVER_*` | 由 Compose 固定 | `STREAMLIT_SERVER_ADDRESS` / `PORT` / `HEADLESS` / `FILE_WATCHER_TYPE`（及 `STREAMLIT_BROWSER_GATHER_USAGE_STATS`）只在容器内生效，取值见 `docker-compose.streamlit.yml` 与 `deploy/streamlit/Dockerfile`；宿主机 `.env` 中的同名变量不会自动传入容器 |

`.env` 的取值与注释以项目根目录 `.env.example` 为准；不要在 `.env`、仓库或截图中写入
真实密钥。改完环境变量后需重启界面进程；Docker 方式需重新
`docker compose -f docker-compose.streamlit.yml up -d`。

## 7. 状态管理：备份、重置与清空

所有操作前先停止界面（`.\start-ui.ps1 -Stop` 或
`docker compose -f docker-compose.streamlit.yml down`），避免边写边复制。

```powershell
# 备份状态库（含页面、选择与笔记）
Copy-Item data\streamlit\ui-state.sqlite3 outputs\ui-state-backup.sqlite3

# 重置全部界面状态：删除后下次启动自动重建空库
Remove-Item data\streamlit\ui-state.sqlite3

# 只清空笔记（保留页面与选择）
.\.venv\Scripts\python.exe -c "import sqlite3; c = sqlite3.connect(r'data\streamlit\ui-state.sqlite3'); c.execute('DELETE FROM ui_notes'); c.commit(); c.close()"

# 只重置导航（页面/上次选中项），保留笔记
.\.venv\Scripts\python.exe -c "import sqlite3; c = sqlite3.connect(r'data\streamlit\ui-state.sqlite3'); c.execute('DELETE FROM ui_state WHERE key IN (?,?,?)', ('ui.page','ui.conversation','ui.report')); c.commit(); c.close()"
```

界面也提供重置视图入口（侧栏「恢复默认视图」，等价于只重置导航）与单条笔记删除。

删除状态文件只影响界面自身的页面与笔记，不会删除会话历史、文档任务或评测报告；这些
数据由 RAG 服务、worker 与评测命令管理。

## 8. 验证命令

```powershell
# 界面状态与只读数据访问的测试（不产生模型费用）
# 未激活虚拟环境时把 python 换成 .\.venv\Scripts\python.exe
python -m pytest tests/test_ui_state.py tests/test_ui_data.py tests/test_ui_launcher.py -q

# 若已安装 .[ui]，可另跑页面装配冒烟测试；未安装 streamlit 时该文件自动跳过
python -m pytest tests/test_ui_app.py -q

# 启动界面后检查 Streamlit 自身健康路由，返回 ok 即可用
Invoke-RestMethod http://127.0.0.1:8501/_stcore/health
curl.exe -fsS http://127.0.0.1:8501/_stcore/health

# 启动器状态（是否运行、PID、URL、状态文件与健康检查）
.\start-ui.ps1 -Status

# 静态检查
python -m ruff check src/medical_rag/ui tests/test_ui_state.py tests/test_ui_data.py tests/test_ui_launcher.py
```

说明：

- `/_stcore/health` 是 Streamlit 自带的健康路由，只说明进程在监听，**不是**项目 API，
  也不代表身份认证或数据安全；
- 若只在当前分支运行新增测试，按上面的文件列表执行即可；全量测试与覆盖率口径以
  README 的“测试与评测”一节为准；
- 测试通过数量与版本不在本文档固化，运行后以实际输出为准。

## 9. 故障排查

| 现象 | 排查 |
|---|---|
| 启动时提示缺少 Streamlit（或 `ModuleNotFoundError: No module named 'streamlit'`） | 未安装可选依赖。按第 5.1 节手动安装，或用 `.\start-ui.ps1 -Install`（需要有 uv）；启动器会打印同样的安装提示 |
| 端口 8501 被占用 | `Get-NetTCPConnection -LocalPort 8501 -State Listen` 或 `netstat -ano \| findstr :8501` 找到占用进程；换端口用 `--port` / `-Port`，不要结束来路不明的进程；`start-ui.ps1` 只会在确认是本项目界面进程时停止它 |
| 页面能打开但会话/任务为空 | 确认 `data/chat-history.sqlite3` 存在且由 RAG 服务或 worker 产生过数据；确认 `CHAT_HISTORY_FILE` 指向正确文件；空库会显示空态而不是报错 |
| 评测报告列表为空 | 确认 `UI_REPORT_DIRS` 指向的目录存在；只识别顶层含 `summary` 对象的 JSON；确认报告由 `python -m medical_rag.evaluate --output ...` 生成 |
| 报告详情打不开 | 单报告上限 200 MiB；文件不是合法 JSON 或顶层结构不是对象时会报“报告无法解析”，换用完整生成的报告 |
| 后端指标显示不可达 | RAG 服务未启动时属正常；确认 `UI_API_BASE_URL` 是回环地址；容器内探测容器自身，见 5.5 节说明 |
| 状态写失败 / 每次重启都回到默认页 | 检查 `UI_STATE_FILE` 路径、目录权限与磁盘空间；容器方式确认命名卷可写，只读挂载的目录不能作为状态目录 |
| 页面提示“读取配置失败” | `.env` 或环境变量里有非法取值导致 `Settings()` 失败；按提示检查 `.env` 后重启界面 |
| 容器启动失败 | `docker compose -f docker-compose.streamlit.yml logs` 查看原因；确认 8501 未被占用、镜像已构建；不要改绑非回环地址 |
| 浏览器打不开 | 只监听回环，需在本机访问 `http://127.0.0.1:8501/`；不要通过反向代理或转发访问；自动打开失败时手动访问即可 |

## 10. 安全声明

**本界面未通过生产医疗数据准入，不能处理真实患者数据，也不是生产界面。**

- 无登录、无鉴权、无 TLS，仅限本机回环访问；不得通过反向代理、端口映射、隧道或任何
  转发服务暴露，也不得更改监听地址为非回环；
- 只用于查看本机虚构数据的持久化数据与运维观察，不构成患者门户、医疗建议或生产监控；
- “只读”指界面不修改项目数据，不能替代数据库权限、审计、备份、加密与访问控制评估；
- 会话历史、评测报告等文件仍可能包含本机演示内容，截图、备份与共享前应先确认不含
  敏感信息；不要在日志、截图或文档中暴露 API Key；
- 生产环境应使用机构批准的部署方式与独立安全评审；本界面不是其组成部分。
