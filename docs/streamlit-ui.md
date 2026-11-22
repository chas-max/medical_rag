# Streamlit 问答与指标控制台

`src/medical_rag/ui/` 是本项目的**本机 Streamlit 控制台**（可选依赖 `.[ui]`）：操作员用本机
租户账号登录后，可直接发起基于公开医疗资料的多轮 RAG 问答（流式、引用、运行轨迹），在
「数据统计」页查看多版本检索指标、本界面问答延迟/吞吐与文档入库任务，并通过侧边栏
「任务进程」观察后端 `/document-jobs`。

它**不是患者门户，也不替代 React 前端**，更不是生产控制台：登录只是界面层的应用功能，
RAG 后端本身没有认证；默认只监听 `127.0.0.1:8501`，只能用于本机虚构数据与演示。

> 文档口径：本文以当前分支实现为准，事实来源为 `src/medical_rag/ui/`、`start-ui.ps1`、
> `start.ps1`、`docker-compose.streamlit.yml`、`deploy/streamlit/Dockerfile` 与
> `.env.example`。测试数量与版本号不写入本文，按第 10 节命令自行核对。

## 1. 概述与定位
能力范围：

- **租户登录**：账号存在本机 SQLite（`UI_STATE_FILE`），scrypt 口令哈希、连续失败锁定、
  滑动会话超时；默认开启**演示账号密码自动填入**（首次访问自动创建并填入本机演示租户，点一次
  「登录」即可进入），账号也可用 CLI 维护。
- **RAG 问答**：调用本机 RAG 后端 `POST /chat/runs/stream`（SSE），可回退
  `POST /chat/runs`；支持多轮追问、流式阶段提示、已核验结论逐条出现、最终答案、引用资料
  表与运行轨迹。
- **数据统计**：多版本检索指标（扫描评测报告 JSON）、问答延迟与吞吐（按租户的真实请求
  遥测）、任务与入库（后端文档任务）。
- **会话与任务**：侧边栏「＋ 新对话」「最近会话」「任务进程」，问答页可确认删除会话。

明确不做的事：不提供后端身份认证或授权（后端仍以 `unsafe-local-demo` 运行、无登录与患者隔离）；不连接患者
PostgreSQL、不处理真实患者数据；不替代 `http://127.0.0.1:8001/ui/chat` 的 React 前端；不是生产监控系统。

## 2. 系统架构
```text
浏览器 ──► Streamlit 控制台 (127.0.0.1:8501，src/medical_rag/ui/)
              │  租户层（仅在界面进程内）
              │    ├─ ui_tenants / ui_auth_attempts   账号与登录审计
              │    ├─ ui_chat_sessions / ui_state     租户会话映射与视图状态
              │    └─ ui_query_metrics                每次问答的耗时/状态/trace
              ├──HTTP/SSE──► RAG 后端 (127.0.0.1:8001，unsafe-local-demo)
              │                ├─ Milvus + BGE 嵌入/重排
              │                ├─ 模型 Provider（QWEN_* / DEEPSEEK_*）
              │                └─ CHAT_HISTORY_FILE（后端会话历史，多轮上下文来源）
              └──只读扫描──► UI_REPORT_DIRS（评测报告 JSON，项目级共享）
```

- 控制台只通过 HTTP/SSE 调用后端，不 import 检索/生成代码，也不直接打开后端会话库；问答正文保存在后端，
  界面只保存租户映射与遥测。
- 租户会话 ID 由 `tenant_id` 前 12 位 + `uuid4` 构成命名空间，后端把会话 ID 当作不透明字符串。
- 后端的 `/conversations`、`/document-jobs` 是全局接口，没有租户概念；租户过滤只在界面层（侧边栏只展示
  `ui_chat_sessions` 中属于当前租户的会话）。

## 3. 前置条件与最短启动
问答必须依赖本机 RAG 后端：先运行 `start.ps1`（拉起 Milvus/RabbitMQ、本机 RAG 服务
`127.0.0.1:8001` 与患者演示环境，以 `unsafe-local-demo` 模式运行）。没有后端时控制台仍能
登录、查看已持久化指标与报告，但问答页显示「后端不可用」，任务相关区块提示失败原因。真实
生成回答还需要 Milvus 可用、模型 Key 已配置。

```powershell
# 终端 1：启动本机 RAG 后端（默认 http://127.0.0.1:8001，unsafe-local-demo）
.\start.ps1

# 首次安装 Streamlit 可选依赖（uv 管理的 .venv 通常没有 pip）
uv pip install --python .venv\Scripts\python.exe -e ".[ui]"

# 终端 2：启动控制台并打开 http://127.0.0.1:8501/
.\start-ui.cmd
```

控制台默认开启**演示账号自动登录**（`UI_DEMO_LOGIN_ENABLED=true`）：第一次打开登录页时会自动
创建本机演示租户，并把**可用的用户名与随机生成、保存在本机状态库中的密码**填入表单，直接点击
「登录」即可进入，无需手工建号。该功能仅用于本机演示；共享/非演示部署请设置
`UI_DEMO_LOGIN_ENABLED=false`（此时登录页回到 CLI 建号流程，未配置租户时显示「创建演示租户」
按钮）。固定演示密码可用 `UI_DEMO_PASSWORD` 指定（8-128 字符）。

也可以预先用 CLI 建号（演示自动登录关闭时更常见）：

```powershell
# 用户名 3-32 位（小写字母/数字/下划线/点/连字符），密码 8-128 位
.\.venv\Scripts\python.exe -m medical_rag.ui.tenants add alice --display-name "Alice" --generate
```

## 4. 界面功能
### 4.1 登录页
- **默认演示自动登录**（`UI_DEMO_LOGIN_ENABLED=true`）：首次打开登录页会自动创建用户名
  `demo`、显示名「演示租户」的本机账号，并把用户名和随机密码直接填入表单，操作员只需点击
  「登录」。密码在首次创建时随机生成并保存在本机状态库（`ui.demo.credentials`），不写入仓库；
  之后每次进入登录页都复用该凭据。
- **固定演示密码（可选）**：设置 `UI_DEMO_PASSWORD`（8-128 字符）后，自动填入使用该固定密码；
  若演示账号已存在但密码不匹配，**不会覆盖**，而是提示后不再自动填入。
- **降级保护**：演示账号被 CLI 改过密码、被停用、或同名租户不是由自动登录创建时，登录页只提示
  原因、不自动填入，也不覆盖现有密码；可用 `reset-password` 重置或手输密码。
- **关闭自动登录**（`UI_DEMO_LOGIN_ENABLED=false`）：回到手工流程。未配置租户时显示
  「创建演示租户（随机密码）」按钮与 CLI 示例，创建成功后一次性展示初始密码；已有租户时显示
  用户名/密码表单。
- 登录错误：用户名不存在、密码错误、账号被禁用统一提示「用户名或密码错误」；连续失败达到上限时
  提示「尝试次数过多，请在约 N 秒后重试」。
- 底部固定安全说明：仅限本机演示，界面登录不能替代后端认证。状态库不可用时提示检查
  `UI_STATE_FILE` 路径与权限；配置非法时提示检查 `.env`。

### 4.2 侧边栏
- 顶部显示当前租户（显示名 · @用户名）与后端健康状态（`GET /health`，缓存 10 秒）：在线时进一步
  显示 Milvus 是否可用，离线时提示「问答需先启动 start.ps1」。
- **「＋ 新对话」**：创建会话 ID、写入 `ui_chat_sessions`、记录上次会话，并切到智能问答。
- **「💬 智能问答 / 📊 数据统计」**：按钮切换页面、当前页高亮，选择写入 `ui_state`（`ui.page`），
  进程重启后恢复。
- **「任务进程」**：读取后端 `GET /document-jobs?limit=8`，显示运行中/最近任务数以及最多 6 条任务
  的图标与阶段（`queued` / `running` / `retrying` / `succeeded` / `failed`）；后端不可用时显示
  「后端不可用，无法获取任务进程」。该面板在每次界面重跑（切换页面、点击按钮、发送问题等）时
  刷新，当前实现没有定时自动刷新。
- **「最近会话」**：只列当前租户在 `ui_chat_sessions` 中的会话（最多 30 条，按最近使用排序）；
  标题优先取后端会话标题，取不到时用本地标题；点击切换。
- **「退出登录」**：清除当前浏览器会话的登录态并回到登录页，不删除账号、会话或指标。

### 4.3 问答页
参考 DeepSeek 网页的居中窄栏布局：

- 标题区显示会话标题（后端标题优先），副标题为「基于公开医疗资料 · 回答不构成医疗建议 · 引用可
  展开核对」；右侧「会话管理」需勾选确认后才允许删除当前会话（删除后端会话并移除本地租户映射）。
- 历史消息按 `user` / `assistant` 气泡渲染；「引用资料」展开表格含资料名、页码、相关度与摘录以及
  资料块 ID；「运行轨迹 / 延迟明细」展示各节点耗时与客户端总耗时；拒答显示证据不足警告，澄清类
  回答显示提示；失败运行折叠在「失败运行」中。
- 底部 `chat_input` 提问，单条问题上限 20 000 字符。提交后流式渲染阶段状态（理解问题 → 检查画像
  与记忆 → 检索资料并生成回答 / 评估问题复杂度 → 规划检索路径 → 召回资料中，附已召回条数与已用
  秒数），已核验结论逐条出现；完成后显示最终答案、引用资料、可选「解读与延伸」、运行轨迹与
  `run_id`。后端错误会显示错误信息与针对性排障提示（见第 11 节）。
- 每次请求结束（成功或失败）都会向 `ui_query_metrics` 写入一行遥测，问答页本身不展示图表。

### 4.4 数据统计页
页首说明数据来源：**检索质量来自评测报告（项目级共享）；延迟与吞吐来自本界面最近的真实问答请求
（按当前租户）**。三个标签：

1. **多版本检索指标**：递归扫描 `UI_REPORT_DIRS` 下最多 100 份顶层含 `summary` 对象的 JSON 报告
   （按修改时间倒序），可选最多 11 项指标（Precision / Recall / NDCG / MRR / Hit Rate / 忠实度 /
   答案相关性 / 上下文相关性 / 拒答率 / 正确拒答率 / 裁判失败数）与报告版本；柱状图对比、最新
   报告指标卡片（相对第二新报告的 delta）、明细表与报告 `summary` 原文；目录为空时给出
   `python -m medical_rag.evaluate` 示例命令。
2. **问答延迟与吞吐**：时间范围「最近 1 小时 / 24 小时 / 7 天 / 全部」（默认 24 小时）；卡片为请求
   数、成功率、P50/P95 延迟、平均首事件、每分钟吞吐；折线图显示延迟分位趋势，柱状图显示每桶请求
   数（1h→5 分钟桶、24h→15 分钟桶、7d/全部→小时桶），另有各节点 P95 耗时、失败错误码分布与最近
   100 条请求明细。数据按当前租户过滤；吞吐覆盖跨度最少按 1 分钟计算。
3. **任务与入库**：读取后端 `GET /document-jobs?limit=100`，展示任务总数、运行中、成功、失败卡片，
   任务状态分布、每日任务量与任务明细；没有任务时提示 `.\start.ps1 -WithWorkers`。该标签是后端
   全局数据，不按租户过滤。

## 5. 租户与登录管理
### 5.1 命令行（CLI）
账号由本机管理员用 CLI 维护，界面没有自助注册（默认只在登录页自动创建/填入演示账号；关闭
`UI_DEMO_LOGIN_ENABLED` 后，空库时显示一次性「创建演示租户」按钮）。命令在**仓库根
目录**执行，读写 `Settings().ui_state_file` 指向的同一个 SQLite：

```powershell
.\.venv\Scripts\python.exe -m medical_rag.ui.tenants add alice --display-name "Alice" --generate  # 创建并生成随机密码
.\.venv\Scripts\python.exe -m medical_rag.ui.tenants list                                         # 列出全部租户
.\.venv\Scripts\python.exe -m medical_rag.ui.tenants reset-password alice --generate              # 重置密码
.\.venv\Scripts\python.exe -m medical_rag.ui.tenants disable alice                                # 禁用（保留数据）
.\.venv\Scripts\python.exe -m medical_rag.ui.tenants enable alice                                 # 重新启用
.\.venv\Scripts\python.exe -m medical_rag.ui.tenants delete alice                                 # 删除账号及登录记录
```

- 用户名入库前转小写，重复用户名报错；密码 8-128 字符。`--generate` 生成含大小写字母、数字、
  符号的随机密码并只打印一次；`-p/--password` 直接传明文（留在 shell 历史/进程列表，仅限
  脚本，不推荐）；两者都不给时用 `getpass` 交互输入两次确认。
- `reset-password` / `disable` / `enable` / `delete` 的目标参数接受用户名或 `tenant_id`；
  `delete` 同时清掉该用户名的登录尝试记录。
- 成功返回 0、业务失败返回 1（参数错误由 argparse 返回 2）；任何模式都不把明文密码写入日志。

### 5.2 演示自动登录、锁定与超时
- **演示自动登录**：`UI_DEMO_LOGIN_ENABLED=true`（默认）时，登录页每次渲染都会校验演示账号：
  不存在则以 `UI_DEMO_USERNAME`（默认 `demo`）创建并随机生成密码，密码保存在本机状态库的
  `ui.demo.credentials`；存在且密码仍匹配时复用并填入。删除状态库会连同演示账号一起重建新的
  随机密码。`UI_DEMO_PASSWORD` 可指定固定演示密码（此时不写状态库）。演示账号被改密/停用/
  凭据缺失时只提示、不覆盖、不填入。
- **口令存储**：stdlib `hashlib.scrypt`（n=16384、r=8、p=1、16 字节随机盐），格式
  `scrypt$16384$8$1$<salt_b64>$<hash_b64>`，用 `hmac.compare_digest` 常量时间校验；账号不
  存在时也做一次真实 scrypt，弱化时间侧信道。演示自动填入的明文密码只存在于本机
  `UI_STATE_FILE`（权限由本机文件系统控制，不加密、不写入仓库）。
- **锁定**：同一用户名在 `UI_LOGIN_LOCKOUT_SECONDS`（默认 300 秒）窗口内失败达到
  `UI_LOGIN_MAX_ATTEMPTS`（默认 5 次）即临时锁定并显示剩余秒数；锁定期间不再累加失败记录；
  成功登录清除该用户名的失败记录（成功审计记录保留）。
- **会话超时**：登录态在 Streamlit `session_state`，有效期 `UI_SESSION_TIMEOUT_MINUTES`
  （默认 480 分钟），每次页面重跑校验并**滑动续期**。超时、进程重启或换浏览器会话都需重新
  登录；账号被禁用/删除后下一次操作即清除登录态并提示重新登录。

### 5.3 安全边界（重要）
**界面登录是应用层功能，不等于后端鉴权。** 租户隔离只覆盖侧边栏会话映射
（`ui_chat_sessions`）、问答指标（`ui_query_metrics`）与视图状态（`ui_state`）。RAG 后端
没有认证，任何能访问本机 `127.0.0.1:8001` 的程序都可直接调用全部接口、列举或删除全部会话；
界面租户不能当作权限、审计或隔离边界。不得把控制台或后端暴露到非回环地址，也不得处理真实
患者数据。

## 6. 持久化状态库
默认文件 `data/streamlit/ui-state.sqlite3`（`UI_STATE_FILE` 可覆盖；相对路径按仓库根目录
解析，不是当前工作目录）。所有表在同一个 SQLite 中，由 `storage.UiSqliteStore` 懒建表、短
连接、`busy_timeout=5000ms`，多标签页可共享：

| 表 | 字段（摘要） | 用途 |
|---|---|---|
| `ui_tenants` | `tenant_id` PK、`username` UNIQUE、`display_name`、`password_hash`、`disabled`、`created_at`、`updated_at` | 租户账号与 scrypt 口令哈希 |
| `ui_auth_attempts` | `id` PK、`username`、`attempted_at`、`success`；索引 `(username, attempted_at DESC)` | 登录尝试审计与锁定计数 |
| `ui_chat_sessions` | `(tenant_id, conversation_id)` 主键、`title`、`created`、`updated`；索引 `(tenant_id, updated DESC)` | 租户可见会话映射与本地标题 |
| `ui_state` | `key` PK、`value`（JSON 文本）、`updated` | 视图状态：`ui.page`、`ui.conversation.<tenant_id>`；以及演示自动填入凭据 `ui.demo.credentials`（含随机演示密码明文，仅本地演示；键 ≤200 字符、值 ≤64 KiB） |
| `ui_query_metrics` | `id` PK、`tenant_id`、`conversation_id`、`request_id`、`created_at`、`latency_ms`、`first_event_ms`、`status`、`abstained`、`answer_chars`、`citation_count`、`stream`、`question_chars`、`error_code`、`trace_json`；索引 `(tenant_id, created_at)`、`(created_at)` | 每次问答的耗时、首事件、状态、引用数与节点 trace（只记长度，不记问题/答案正文） |

写入边界：界面只写自己的状态库；`CHAT_HISTORY_FILE`（后端会话历史）与评测报告 JSON 由后端/
评测命令维护，控制台不修改它们（报告目录只读扫描）。删除状态库**不会**删除后端会话历史
（`data/chat-history.sqlite3`）、文档任务或 `outputs/reports` 下的评测报告。

备份与重置（操作前先停止界面，避免边写边复制）：

```powershell
.\start-ui.cmd -Stop
Copy-Item data\streamlit\ui-state.sqlite3 outputs\ui-state-backup.sqlite3   # 备份
# 删除状态库 = 重置账号、会话映射、问答指标、视图状态（下次启动自动重建空库）
Remove-Item data\streamlit\ui-state.sqlite3
# 只清空问答指标（保留账号/会话映射/视图状态）
.\.venv\Scripts\python.exe -c "import sqlite3; c = sqlite3.connect(r'data\streamlit\ui-state.sqlite3'); c.execute('DELETE FROM ui_query_metrics'); c.commit(); c.close()"
# 只重置视图状态（保留账号、会话映射、指标）
.\.venv\Scripts\python.exe -c "import sqlite3; c = sqlite3.connect(r'data\streamlit\ui-state.sqlite3'); c.execute('DELETE FROM ui_state'); c.commit(); c.close()"
```

Docker 方式的状态在命名卷 `medical_rag_ui_state` 中，`down` 保留，`down -v` 会连同账号/
指标一起删除。

## 7. 配置项
配置由 `Settings` 从进程环境与仓库根 `.env` 读取（`.env.example` 有注释示例）；改动后需
重启控制台，Docker 方式需重新 `up -d`。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `UI_STATE_FILE` | `data/streamlit/ui-state.sqlite3` | 界面状态库（租户/登录审计/会话映射/指标/视图状态）；相对路径按仓库根解析 |
| `UI_REPORT_DIRS` | `outputs/reports` | 评测报告扫描目录，逗号/分号分隔多个；相对路径按仓库根解析 |
| `UI_API_BASE_URL` | `http://127.0.0.1:8001` | RAG 后端地址，必须是回环 `http(s)` 且不含路径/查询串；其它地址会被拒绝（登录后提示后端地址配置无效） |
| `UI_REQUEST_TIMEOUT_SECONDS` | `120` | 单次问答/请求超时（秒，1-600；连接超时 5 秒） |
| `UI_STREAM_ENABLED` | `true` | `true` 走 `POST /chat/runs/stream`（SSE）；`false` 回退一次性 `POST /chat/runs` |
| `UI_TOP_K` | `5` | 每个问题请求的检索条数（1-20） |
| `UI_SESSION_TIMEOUT_MINUTES` | `480` | 登录会话空闲有效期（分钟，5-10080），每次操作滑动续期 |
| `UI_LOGIN_MAX_ATTEMPTS` | `5` | 锁定窗口内允许的连续失败次数（1-20） |
| `UI_LOGIN_LOCKOUT_SECONDS` | `300` | 登录锁定窗口（秒，30-3600） |
| `UI_DEMO_LOGIN_ENABLED` | `true` | 演示账号自动创建+账号密码自动填入；仅本机演示，共享环境必须设 false |
| `UI_DEMO_USERNAME` | `demo` | 自动填入的演示账号用户名（3-32 位，非法时登录页提示配置无效） |
| `UI_DEMO_PASSWORD` | 空 | 固定演示密码（8-128 字符）；留空则首次随机生成并写入 `ui.demo.credentials` |
| `CHAT_HISTORY_FILE` | `data/chat-history.sqlite3` | 兼容配置：启动日志仍打印，但当前控制台的会话历史经后端 API 读取，不直接打开该库 |

主题：仓库根 `.streamlit/config.toml` 提供 DeepSeek 风格浅色主题（`primaryColor = #4d6bfe`），
本地从仓库根启动时由 Streamlit 自动读取；容器镜像未打包该文件，容器内使用默认主题。

## 8. 启动方式（4 种）
### 8.1 `start-ui.cmd` / `start-ui.ps1`（推荐）
```powershell
.\start-ui.cmd                  # cmd/双击入口：后台启动并打开 http://127.0.0.1:8501/
.\start-ui.ps1                  # 已在 PowerShell 中时可直接运行
.\start-ui.ps1 -Install         # 缺少 Streamlit 时允许用 uv 安装 .[ui]
.\start-ui.ps1 -Port 8502       # 换端口（仍只绑定 127.0.0.1）
.\start-ui.ps1 -NoBrowser       # 不自动打开浏览器
.\start-ui.ps1 -Foreground      # 前台运行，Ctrl+C 停止；不写 PID、不打开浏览器
.\start-ui.ps1 -Status          # 运行状态、PID、URL、状态库、健康检查、RAG 后端可达性
.\start-ui.ps1 -Stop            # 停止本项目拉起的控制台进程，保留状态库
.\start-ui.ps1 -TimeoutSeconds 120   # 后台启动等待 /_stcore/health 的秒数（默认 90）
```

Windows 的 `.ps1` 没有默认文件关联，在 cmd/双击里直接执行 `.\start-ui.ps1` 可能用记事本打开文件；
请用 `.\start-ui.cmd`，或在 PowerShell 中显式执行
`pwsh -NoProfile -ExecutionPolicy Bypass -File .\start-ui.ps1`（`.cmd` 会原样转发上述参数）。后台
日志在 `outputs/logs/streamlit-ui.log` / `.err.log`，PID 文件在 `outputs/run/streamlit-ui.pid`；
`-Stop` 只结束进程、不删数据。

### 8.2 直接运行 Python 入口
```powershell
.\.venv\Scripts\python.exe -m medical_rag.ui
.\.venv\Scripts\python.exe -m medical_rag.ui --port 8502 --no-browser
```

| 参数 | 说明 |
|---|---|
| `--host` | 监听地址，只允许 `127.0.0.1` / `localhost` / `::1`，默认 `127.0.0.1` |
| `--port` | 监听端口，默认 `8501` |
| `--state-file` | 覆盖 `UI_STATE_FILE` |
| `--history-file` | 覆盖 `CHAT_HISTORY_FILE`；当前页面经后端 API 读取会话，此项为兼容保留 |
| `--reports-dir` | 覆盖 `UI_REPORT_DIRS`，可重复传入多个目录 |
| `--no-browser` | 启动后不自动打开浏览器 |

默认启动后约 2.5 秒自动打开 `http://127.0.0.1:8501/`。启动器以 headless、关闭文件监听与用量统计、
最小工具栏的模式拉起 Streamlit，并在启动前校验地址、端口与路径参数。

### 8.3 随一键启动一起启动
```powershell
.\start.ps1 -WithStreamlit
```

在启动 RAG 后端之后额外启动 `127.0.0.1:8501` 的控制台（不自动打开该页面）；缺少 `.[ui]` 依赖时
报错并中止本次启动，不会自动联网安装。`.\start.ps1 -Stop` / `-StopAll` 会一并停止由它启动的控制台
进程，保留 `data/streamlit/ui-state.sqlite3`。

### 8.4 Docker Compose（仅离线查看，问答不可用）
```powershell
docker compose -f docker-compose.streamlit.yml up -d      # 构建并启动
docker compose -f docker-compose.streamlit.yml logs -f    # 查看日志
docker compose -f docker-compose.streamlit.yml down       # 停止并删除容器（保留命名卷）
docker compose -f docker-compose.streamlit.yml down -v    # 同时删除命名卷（账号/指标丢失）
```

镜像由 `deploy/streamlit/Dockerfile` 构建（`python:3.11-slim`，只装 streamlit / pydantic-settings /
loguru 等最小依赖，不含 torch 或整套 RAG 依赖）。容器以非 root（uid 10001）运行、只读根文件系统、
`tmpfs` 仅 `/tmp`、`cap_drop: ALL`、`no-new-privileges`，带 `restart: unless-stopped` 与
`/_stcore/health` 健康检查；宿主机发布端口只绑定 `127.0.0.1:8501`。`./data`、`./outputs` 以 `:ro`
挂载，命名卷 `medical_rag_ui_state` 覆盖 `/app/data/streamlit` 作为可写状态目录，因此租户账号、会话
映射与指标可跨容器重建保留。

**关键限制：容器里问答不可用。** RAG 后端只接受本机回环来源，而容器内的 `127.0.0.1:8001` 指向容器
自身；compose 未注入 `UI_API_BASE_URL`，默认值在容器内不可达。因此容器版只能登录、查看
`outputs/reports` 下的评测报告以及命名卷里已有的指标，问答页、侧边栏任务进程与统计页「任务与入库」
都会提示后端不可用，也不会产生新的问答指标。需要真实问答时请在宿主机用 `start-ui.cmd` /
`python -m medical_rag.ui` 运行控制台。容器内维护账号（可选）：
`docker compose -f docker-compose.streamlit.yml exec medical-rag-ui python -m medical_rag.ui.tenants list`。

## 9. 多轮问答与流式事件
- 请求体与 React 前端一致：`request_id`（随机 hex）、`conversation_id`、`workflow_id =
  medical-answer`、`inputs = {question, provider: "qwen", top_k}`、`memory_consent: false`；请求头带
  本机回环 `Origin`（后端 `unsafe-local-demo` 要求）。
- 默认 `POST /chat/runs/stream`（`Accept: text/event-stream`）逐条解析 SSE；`UI_STREAM_ENABLED=false`
  时改用 `POST /chat/runs` 一次性返回。两条路径返回结构相同，界面记录同口径指标。
- 多轮上下文由后端 `CHAT_HISTORY_FILE` 提供：界面只传同一个 `conversation_id`，后端按会话历史补上下文；
  界面用 `GET /conversations/{id}` 渲染历史、引用与失败运行，并用 `GET /conversations` 的标题覆盖本地
  标题。删除会话调用后端 `DELETE /conversations/{id}` 并移除 `ui_chat_sessions` 映射。

| SSE 事件 | 界面行为 |
|---|---|
| `chat.progress` / `node.progress` / `status` | 按 `stage` 显示阶段文案；`evidence` 为整数时追加「已召回 N 条」 |
| `claim` | 把 `text` 追加到「已核验的结论（流式）」列表，逐条出现 |
| `analysis` | 保存 `summary`/`extensions`，结束时显示「解读与延伸」折叠区 |
| `done` | 权威最终结果：`status`、`output.answer`、`citations`（或 `evidence`）、`abstained`、`abstain_reason`、`answerType`、`trace`、`error` |
| `error` | 转为 `BackendError` 并按错误码给出排障提示 |

阶段文案映射：`intent` 理解问题、`profile` 检查画像与记忆、`answer` 检索资料并生成回答、`assessing`
评估问题复杂度、`planning` 规划检索路径、`retrieving` 召回资料中、`stopped` 检索完成。指标中的
`first_event_ms` 为提交到首个事件的时间，`latency_ms` 为提交到 `done`/错误的时间；运行轨迹表格按节点
耗时倒序，并追加一行「客户端总耗时」。常见错误码提示：`BACKEND_UNREACHABLE`（先运行
`.\start.ps1`）、`BACKEND_TIMEOUT`（调大 `UI_REQUEST_TIMEOUT_SECONDS`）、`SECURITY_NOT_READY`
（后端 locked，需以 `unsafe-local-demo` 启动）、`LOCAL_DEMO_ONLY`（非本机来源被拒）、
`DEPENDENCY_ERROR`（检查 Milvus 与模型 Key）、`STREAM_INTERRUPTED`（可重试或关闭流式）。

## 10. 验证命令
```powershell
# 控制台单元测试（不产生模型费用；auth/demo/state/stats/api_client/data/launcher）
.\.venv\Scripts\python.exe -m pytest tests/test_ui_auth.py tests/test_ui_demo.py tests/test_ui_state.py tests/test_ui_stats.py tests/test_ui_api_client.py tests/test_ui_data.py tests/test_ui_launcher.py -q
# 页面装配冒烟测试（需要已安装 streamlit；未安装时该文件整体跳过）
.\.venv\Scripts\python.exe -m pytest tests/test_ui_app.py -q
# 静态检查
.\.venv\Scripts\python.exe -m ruff check src tests
# 启动后检查 Streamlit 健康路由（返回 ok 即可用）
Invoke-RestMethod http://127.0.0.1:8501/_stcore/health
# 后端健康与启动器状态（-Status 也会探测 RAG 后端）
Invoke-RestMethod http://127.0.0.1:8001/health
.\start-ui.cmd -Status
# 租户 CLI 核对（列出现有账号）
.\.venv\Scripts\python.exe -m medical_rag.ui.tenants list
```

说明：`/_stcore/health` 是 Streamlit 自带健康路由，只说明进程在监听，不是项目 API，也不代表身份认证
或数据安全；`tests/test_ui_app.py` 用 Streamlit `AppTest` 驱动登录/问答/统计页，未安装 streamlit 时由
`pytest.importorskip` 跳过。测试数量与版本不在本文固化，以实际输出为准。

## 11. 故障排查
| 现象 | 排查 |
|---|---|
| 登录页没有自动填入账号密码 | 默认应自动填入 `demo`。若提示「没有可自动填入的凭据」，说明同名租户不是自动登录创建的、或旧状态库缺少凭据：用 CLI `reset-password demo --generate` 重置，或删除 `UI_STATE_FILE` 让演示账号重建；若配置了 `UI_DEMO_LOGIN_ENABLED=false` 则属于预期行为 |
| 提示演示账号密码已变更、不再自动填入 | 有人用 CLI 改过演示账号密码。自动填入**不会覆盖**现有密码；手输新密码，或 `reset-password demo --generate` 后刷新页面 |
| 演示账号总被锁定 | 自动填入后若有人用错误密码反复点登录，仍会触发失败锁定；等待 `UI_LOGIN_LOCKOUT_SECONDS`（默认 300 秒）后重试，或用 CLI 重置密码 |
| 想关闭演示自动登录 / 公开演示环境 | 设置 `UI_DEMO_LOGIN_ENABLED=false` 并重启控制台；共享或非演示环境必须关闭，改用 CLI 建号，登录页会回到手工表单 |
| 执行 `.\start-ui.ps1` 却用记事本打开了文件 | Windows `.ps1` 文件关联问题。改用 `.\start-ui.cmd`，或执行 `pwsh -NoProfile -ExecutionPolicy Bypass -File .\start-ui.ps1` |
| 提示缺少 Streamlit / `No module named 'streamlit'` | 未安装 `.[ui]`。执行 `uv pip install --python .venv\Scripts\python.exe -e ".[ui]"`，或 `.\start-ui.ps1 -Install`（需有 uv） |
| 问答页提示「无法连接 RAG 服务」 | 后端未启动或未以 `unsafe-local-demo` 运行。先在仓库根运行 `.\start.ps1`；确认 `http://127.0.0.1:8001/health` 可用；`.\start-ui.ps1 -Status` 会显示后端可达性 |
| 后端返回 503 / `SECURITY_NOT_READY` | 后端处于 `locked` 模式，业务接口全部关闭；用 `start.ps1` 以 `unsafe-local-demo` 启动，仅限本机虚构数据 |
| 能登录但提问报依赖错误 | Milvus 未就绪或 `.env` 未配置模型 Key；检查 `docker compose up -d` 与 `QWEN_*`/`DEEPSEEK_*` 配置 |
| 登录提示「尝试次数过多」 | 触发连续失败锁定。等待提示秒数（默认窗口 300 秒）后重试；忘记密码用 CLI `reset-password` 重置 |
| 登录后很快需要重新登录 | 登录态在 `session_state` 中：进程重启、换浏览器会话或超过 `UI_SESSION_TIMEOUT_MINUTES` 空闲都会要求重登；账号被禁用/删除也会使会话失效 |
| 登录页提示「本地界面数据库不可用」/「读取配置失败」 | 检查 `UI_STATE_FILE` 路径、目录权限与磁盘空间；容器方式确认命名卷可写、只读挂载目录不能作为状态目录；`.env` 中 `UI_API_BASE_URL` 必须为回环地址 |
| 账号/指标突然全空或「最近会话」看不到旧会话 | 状态库被删除或重建（见第 6 节），或 Docker 执行了 `down -v`；会话映射只存在状态库，可点「＋ 新对话」重新开始；这不影响后端会话历史与评测报告 |
| 数据统计页没有延迟/吞吐数据 | 该页只统计本界面产生的真实问答请求，且按当前租户过滤；先去智能问答页提问，或把时间范围调到「全部」 |
| 多版本检索指标为空 | `UI_REPORT_DIRS` 目录不存在或没有顶层含 `summary` 的 JSON；用 `python -m medical_rag.evaluate --dataset data/evaluations/medical-health-v2/questions.jsonl --output outputs/reports/medical-health-v2.json` 生成报告，或检查目录配置 |
| 「任务进程」/「任务与入库」为空或报错 | 该数据来自后端 `/document-jobs`；确认后端已启动，文档入库任务需 `.\start.ps1 -WithWorkers` |
| 端口 8501 被占用 | `Get-NetTCPConnection -LocalPort 8501 -State Listen` 找到占用进程；换端口用 `-Port` / `--port`；启动器只会在确认是本项目进程时停止它 |
| 页面样式不是 DeepSeek 主题 | `.streamlit/config.toml` 只在从仓库根启动时读取；容器镜像未打包该文件，容器内为默认主题 |
| Docker 里问答/任务都提示后端不可用 | 预期限制：容器内 `127.0.0.1:8001` 指向容器自身，且后端只接受回环来源。容器版只用于离线查看评测报告与已有指标；真实问答请在宿主机运行 |
| 容器启动失败 | `docker compose -f docker-compose.streamlit.yml logs` 查看；确认 8501 未占用、镜像构建网络可用；不要改绑非回环端口 |
| 浏览器打不开 | 只监听回环，必须在本机访问 `http://127.0.0.1:8501/`；不要通过反向代理、隧道或端口映射访问 |

## 12. 安全与限制声明
**本控制台未通过生产医疗数据准入，不能处理真实患者数据，也不是生产界面。**

- **界面登录 ≠ 后端鉴权**：RAG 后端没有认证、没有租户/角色/患者隔离，只以 `unsafe-local-demo`
  监听本机回环。租户账号只隔离界面层的会话映射与问答指标；能访问本机 8001 的任意程序都可绕过界面
  直接调用后端。不要把它当作访问控制、审计或合规边界。
- **仅限本机、无 TLS**：控制台与后端都只允许绑定回环地址，不得转发、反向代理、内网穿透、容器端口
  映射到非回环地址；Docker 方式也只发布到 `127.0.0.1:8501`。本地 HTTP 明文传输，登录口令与问答
  内容不要跨主机或经过不可信网络。
- **数据边界**：只用于本机虚构数据与公开医疗资料演示，不要输入患者隐私或机构内部资料。SQLite 状态
  库以本机文件保存租户、会话映射与遥测（未加密），问答正文保存在后端会话库；截图、备份、共享前先
  确认不含敏感信息，且不得外泄 API Key。
- **指标不是生产监控**：`ui_query_metrics` 是本地轻量遥测，不包含问题/答案正文，没有采样、聚合、
  告警与保留策略；只反映本界面产生的请求，不能用于容量规划或审计。
- **演示自动登录是便利功能，不是认证**：默认开启时任何人打开本机登录页都能一键进入演示租户；生成
  的演示密码以明文保存在本机状态库。共享、多人或非演示环境必须设置 `UI_DEMO_LOGIN_ENABLED=false`，
  并按机构流程建号与保管凭据。
- **已知限制**：真实回答依赖后端、Milvus 与模型 Key；后端 `locked` 时业务接口 503；容器方式无法
  访问宿主机回环后端，问答不可用；侧边栏任务进程当前只在页面重跑时刷新，没有定时轮询；同一状态库
  多标签页共享，不保证跨进程高并发写入性能。生产环境应使用机构批准的部署方式、独立认证与安全评审；
  本控制台不属于生产系统。
