# 公开医疗资料 RAG 问答系统

面向公开医疗资料的中文检索问答项目。文档经父子分块和向量化后写入 Milvus；问答使用原问题向量检索、BM25 检索与 HyDE 向量检索，RRF 融合、重排、证据门控、回答规划和引用校验。React 提供文档上传、流式问答与来源查看。

本项目用于学习和公开资料检索，**不能替代医生诊断或治疗，不要输入患者隐私**。引用块 ID 校验只能确认引用指向检索到的资料，不能保证医学结论正确；关键结论应核查原文。

## 目录结构

后端模块、前端、测试、数据库/部署、安全文档及生成物清理规则见 [项目结构说明](docs/project-structure.md)。测试临时目录、缓存、覆盖率与前端测试报告均为可再生成产物，已清理并加入 `.gitignore`。

## 检索与安全

HyDE 由模型生成一段未经验证的假设性回答，仅用于向量检索；它不是证据，不会直接传给回答生成或引用校验。原问题始终参与向量和 BM25 检索，并用于重排与生成。HyDE 模型请求默认限时 10 秒，可通过 `HYDE_TIMEOUT_SECONDS` 调整；请求失败或输出无效时该支路返回空结果，正常使用原问题的两路检索，运行记录标注降级。设置 `HYDE_ENABLED=false` 后重启服务可关闭该支路，用于同口径评测基线。HyDE 可能增加模型费用和延迟，其收益需要实验验证，不能假设必然提升。

本地重排不可用时默认拒答；证据不足时不输出未锚定的医疗结论。可选联网检索未默认启用。

**安全状态：尚未通过生产医疗数据准入，不得处理真实患者数据。** 旧单用户业务接口和后台工作进程仍默认锁定；旧应用的 `GET /health` 仅返回 `security_not_ready`，其他接口返回 503，工作进程拒绝启动。另有与旧业务隔离的 OIDC 患者开发应用，已连接当前数据库身份、用途授权和本人会话记录；它不开放旧聊天、记忆、模型设置、文档或后台任务。具体边界见下文与 [生产准入清单](docs/security/production-readiness.md)。

如需在本机使用原有单用户演示，须显式设置 `MEDICAL_RAG_ACCESS_MODE=unsafe-local-demo`。该模式**完全没有登录、授权或患者隔离**，且只能使用虚构数据；不得连接反向代理、容器端口映射或任何其他转发服务，不得暴露给他人。应用会限制客户端、Host 和 Origin 为本机地址并拒绝常见转发头；使用命令行直接调用写接口时须显式设置本机 Origin，例如 `Origin: http://127.0.0.1:8001`。这些检查不能阻止未声明来源的本机代理或其他本机程序访问，不能视为安全边界。

## 独立患者身份与会话应用

`oidc_login.py`、`identity_sessions.py`、`patient_service.py` 与 `patient_api.py` 组成独立开发链路：OIDC 授权码及 S256 PKCE → 浏览器绑定的一次性登录交易 → 安全 Cookie 会话 → 当前账号、角色及核验绑定 → 当前健康信息用途授权 → 本人的 PostgreSQL 会话和文字记录。身份提供方的角色或患者字段不直接成为本地权限；服务端不接受客户端指定用户、机构或患者归属。

登录交易中的 nonce 与 PKCE 验证值由机构提供的 AES-GCM 密钥加密；会话和 CSRF 令牌只保存摘要。浏览器使用 `__Host-`、Secure、HttpOnly、SameSite=Lax Cookie，写操作还要求固定 Origin 与 CSRF 证明。每次患者操作在独立的主库事务内获取当前身份及患者行锁，等待后重新读取核验状态和权限，再检查最新用途授权。管理员、客服和平台角色不会因此获得患者正文；本入口暂不开放未成年人、监护人或年龄未知路径。

健康信息授权记录按患者串行追加，保存版本、签署者和有效期；撤回停止后续相应用途访问，但不会自动删除历史记录。已获准且持有锁的操作属于在途操作，撤回等待其事务结束，不等于取消已开始的流或后台任务。问答生成不持有患者行锁：撤回后持久化前会重新校验，撤回期间生成的回答直接丢弃、不写库。角色、绑定、账号或机构变化使既有会话失效。签名 `POST /auth/backchannel-logout` 已连接机构身份提供方撤权、重复事件保护及待完成旧登录的持久阻断；严格支持的令牌格式和机构注册要求见 [接入说明](docs/security/provider-logout-setup.md)。撤权阻止下一次受保护请求，不会推送清空闲置页面或取消在途操作。机构批准的公钥环已支持人工部署的重叠与退役，登录和退出通知共用同一信任集；[操作说明](docs/security/signing-keyring-runbook.md)不等于自动发现或密钥泄露应急方案。客户端密钥、登录加密密钥轮换及待完成登录期间账号停用再启用的处理仍待实现。

患者功能包括新建会话、查看本人记录、追加文字记录，以及在机构显式配置 `[chat]` 且当前授权有效时，把问题发送给本机公开资料 RAG 答案服务并保存带引用的问答。患者入口仍不接通患者记忆、病历、医院内网或旧后台任务系统；问答只基于公开医疗资料，不替代医生诊断，也不能用于处理真实患者资料。另有需当前 MFA 的机构审计元数据视图，以及不含医疗正文的账户支持功能。数据库迁移 `0001`—`0008` 只用于新的隔离库，不自动迁移或分配旧 SQLite 数据。数据库角色仍是受信的 SQL 执行边界；RLS 和启动诊断不能代替完整权限、密钥、备份、审计和运维评估。

有效登录账号可在“账户支持”创建、查看和取消本人的固定类别请求，无需健康信息授权；这不授予患者资料访问权。只有当前本地客服角色可查看本机构队列、受理及记录固定处理结果，仍遵循组合角色的 MFA 规则。首期没有医疗文字、附件、账号解锁、角色修改或患者绑定修改；完全无法登录时不能使用这个渠道。开发限额为每账号滚动 24 小时 20 次、最多 5 个未结束请求，取消不重置每日计数；不是生产网关限流或机构批准的支持政策。

开发验证使用虚构账号、签名测试身份提供方和临时 PostgreSQL；另已用官方 Keycloak 26.8.1 验证实际 HTTPS 登录、令牌签名和退出通知。后者的应用路由使用 TestClient，不是完整浏览器或全链路网络验收。**尚未与真实医院身份服务联调，也未通过生产安全、医学或法律验收**。所有代理和服务器必须关闭回调查询及请求/响应正文日志，不记录授权码、令牌或患者内容。旧业务和工作进程不能因为新入口存在而解除锁定。

## 一键启动：RAG + 患者问答门户（推荐）

本机演示只需要一个入口脚本：`start.ps1`。它会启动检索依赖（Milvus/RabbitMQ）、本机 RAG 答案服务（8001）、患者演示环境（PostgreSQL + Keycloak，首次或容器未运行时自动重建）和需要登录的患者门户（8443），完成后打开 `https://localhost:8443/`。

```powershell
.\start.ps1                 # PowerShell 中启动并打开浏览器
.\start.cmd                 # cmd 或双击入口（内部调用 pwsh/powershell）
.\start.ps1 -NoBrowser      # 只启动，不打开浏览器
.\start.ps1 -WithWorkers    # 额外启动 relay/ingest/memory（文档入库/记忆）
.\start.ps1 -ResetPatient   # 强制重建患者 PostgreSQL/Keycloak
.\start.ps1 -Stop           # 停止进程，保留容器与数据
.\start.ps1 -StopAll        # 停止进程并删除患者容器/关闭 compose
```

Windows 的 `.ps1` 没有默认文件关联；在 **cmd** 里直接输入 `.\start.ps1` 会用记事本打开文件而不是执行。请改用 `.\start.cmd`，或在 PowerShell 中运行 `.\start.ps1`；也可以在 cmd 中显式调用解释器：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
# 或
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

演示账号在 `outputs\patient-demo\credentials.txt`。登录后直接进入问答界面；个人信息在左下角头像菜单。首次访问浏览器会提示演示自签证书，仅在本机演示中继续。旧的 `http://127.0.0.1:8001/ui/chat` **不再是一键启动的入口**：它仍可访问，但那是无登录的单用户 `unsafe-local-demo`，只能用于本机虚构数据，不能转发或接入代理。

脚本依赖 `outputs/patient-demo/` 下的演示装配脚本和自签证书；这是本机一次性演示环境，不是生产部署方式。首次启动或容器停止后需要重建（约 1–2 分钟），患者数据库使用 tmpfs，不持久。

## 患者开发入口启动（PowerShell，与旧演示隔离）

先构建独立页面，并确保 Python 导入的是当前工作区。使用虚拟环境；不要把本项目安装覆盖到另一个项目的全局环境。

```powershell
.\.venv\Scripts\python.exe -m pip install --no-user -e '.[dev]'
npm --prefix frontend run build:patient
.\.venv\Scripts\python.exe -c "import medical_rag; print(medical_rag.__file__)"
```

由机构运维在**新的隔离 PostgreSQL 库**中依次执行 `database/migrations/0001_patient_scope.sql` 至 `0008_conversation_answers.sql`，审查并通过 `database/patient_runtime_grants.sql` 给非所有者运行角色授权。新患者入口要求审计、撤权事件表、支持请求表、问答 turn 角色/引用列及最小权限，缺失时拒绝启动，不能通过关闭审计或跳过撤权阻断继续运行。脚本不创建生产账号、不自动授予核验状态，也不覆盖既有数据库；真实账号、角色、患者映射与核验来源须通过可信机构流程配置。

以 `deploy/patient/config.example.toml` 为参考，在机构控制的私密目录配置实际 TOML、单独的 DSN/客户端密钥文件、32 字节登录加密密钥、IdP 公钥、TLS 证书/私钥及经审核的告知文本。相对路径按 TOML 所在目录解析；该入口不读取旧 `.env`。不要提交密钥、复制占位值假装已接入身份服务，或把测试核验记录当作真实核验。

机构身份服务使用私有 CA 时，可在 `[oidc]` 设置 `provider_ca_file`，指向机构批准的 PEM 信任包；未配置时使用公共 CA 校验。启动及每次实际令牌交换都会校验显式信任文件，失效时拒绝登录，不退回到不安全连接，也不修改系统证书存储。

```powershell
.\.venv\Scripts\python.exe -m medical_rag.security_server --config C:\secure\medical\config.toml
```

入口直接使用 HTTPS，默认示例绑定回环地址；外部来源和 Host 必须匹配固定回调来源。服务器不信任转发身份/协议头，不记录访问查询、正文和令牌。远程数据库要求 `sslmode=verify-full`；明文回环连接只用于隔离的虚构数据开发。配置、密钥、必要权限、主库状态或强制 RLS 检查失败时拒绝启动。仅挂载独立 `patient.html` 与 `/assets`，不会回退到旧 `/ui`。

另行提供的独立患者网关（`docker-compose.patient-gateway.yml` 与 `deploy/patient-gateway/`）只连接上述患者入口，使用校验上游证书链和主机名的 HTTPS、精确 Host、请求头白名单和只读根文件系统；它不读取旧 `.env`，也不解除旧接口锁定。旧的 `deploy/gateway/nginx.conf` 明文 HTTP 实验仍是独立未完成配置，**不能接到此入口**。完整审计、数据库日志参数脱敏、网络边界、备份/存储加密、密钥轮换、机构管理业务及医院/模型接入仍见 [生产准入清单](docs/security/production-readiness.md)。独立页面提供身份、健康信息授权、带引用的公开资料问答、文字记录及受限的审计与账户支持；回答不构成医疗建议。

患者入口登录成功后直接进入 ChatGPT 式界面：左侧是「新对话」、最近会话与分页，中间是消息区和底部输入框，左下角头像菜单显示本系统账户、所属机构、系统角色与核验状态，并提供退出登录；身份、健康信息授权、安全审计与账户支持从侧栏进入。配置 `[chat]` 后，发送问题会走患者鉴权的 `POST /chat`：先校验会话、角色、绑定和当前健康信息授权，再调用本机 RAG 答案服务，最后在持久化前重新校验并以 `user`/`assistant` 角色保存问答与引用；撤回期间生成的回答会被丢弃。未配置 `[chat]` 时退化为只保存文字记录。回答只基于公开医疗资料，不替代医生诊断；界面和告知都要求不要输入患者隐私。

`[chat]` 是可选配置段：`base_url` 必须是回环 `http(s)` 来源，另有 `timeout_seconds`（1–300）、`top_k`（1–20）与可选的 `streaming`（默认 false）。当前演示在 `outputs/patient-demo/config.toml` 中指向本机 8001 的 `unsafe-local-demo` `/answers`；这是**演示桥接**，不是生产部署方式。生产环境必须换成内部认证的 RAG 服务，并补齐账号级限流和机构模型审批。

设置 `streaming = true` 后，患者入口改用 `POST /chat/stream`（Server-Sent Events），逐条显示已通过引用与数值校验的结论，而不是等整段答案返回。流式与非流式共用同一套校验和同一笔落库事务，因此两条路径存储的记录结构一致；区别是**终态被否定时界面会清空已经显示的文字**：证据门控拒答显示拒答文案，生成期间撤回授权或会话被改写则显示"本次回答未保存"，且不写入任何轮次。启用前提是 RAG 服务提供 `POST /answers/stream`，并且访问路径上不存在缓冲响应的代理；独立患者网关已关闭代理缓冲。患者界面通过 `permissions.chat_stream` 判断能力，不探测端点，未启用时该路由不存在。

## 旧单用户本机演示启动（PowerShell）

需要 Python 3.11+、Node.js 20.19+/22.12+、Docker Desktop 和本地 BGE 模型缓存。首次准备嵌入与重排模型可能需要下载模型权重。新项目的 `.env` 已单独保存复制的模型配置和独立服务配置，包含明文密钥；请勿提交或分享该文件。

```powershell
Set-Location D:\medical-rag-resume
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
docker compose up -d
npm --prefix frontend install
npm --prefix frontend run build
$env:MEDICAL_RAG_ACCESS_MODE = 'unsafe-local-demo' # 仅用于本机虚构数据演示
python -m medical_rag.api_server --host 127.0.0.1 --port 8001
```

在三个单独终端、同一项目目录及其虚拟环境中启动后台进程：

```powershell
$env:MEDICAL_RAG_ACCESS_MODE = 'unsafe-local-demo' # 每个工作进程终端都要单独设置
python -m medical_rag.worker relay
python -m medical_rag.worker ingest
python -m medical_rag.worker memory
```

## 日志（Loguru）

`api`、`relay`、`ingest`、`memory` 四个进程启动时各自建立一套 Loguru sink：控制台可读文本 + `outputs/logs/<role>/<role>.log`。文件默认按 20 MB 轮转、保留 14 天、滚动后的历史文件压缩成 zip；`LOG_JSON=true` 时文件与控制台都改成单行 JSON，便于外部采集。第三方库（uvicorn/httpx/openai/pymilvus/aio_pika 等）的标准库日志会被转发到同一套 sink。

`LOG_ENABLED=false` 只保留控制台、不写文件；测试与 CI 不应在仓库落盘，`tests/conftest.py` 会把 `LOG_DIR` 指向临时目录。

日志只记录固定短语与降级事件，不写问题原文、患者记录、答案正文、API Key、连接串或文件路径；`backtrace`/`diagnose` 固定关闭，避免 traceback 携带局部变量。凭证样式（`sk-…`、`Bearer …`、`scheme://user:pass@host`、`password=`/`api_key=`）会被 patcher 兜底遮蔽。

患者门户默认**完全不落日志**：只有机构在 TOML 里显式配置 `[logging] enabled = true` 时才会写文件，且只记录启动/关闭/preflight 结果等运维元事件，请求级证据仍只进 PostgreSQL 审计表。

## HTTP 客户端依赖（httpx / httpx2）

本项目有意同时使用两个 HTTP 客户端，这不是重复依赖：

- `httpx`（`>=0.27,<0.29`）：OpenAI 1.x SDK、联网检索 `web_search.py` 和患者问答客户端 `patient_chat.py` 使用；
- `httpx2`（`>=2.16,<3`）：OIDC 登录（Authlib ≥1.9 的 HTTP 集成内部使用 `httpx2`）以及 Starlette ≥1.10 的 `TestClient` 使用。

两者模块名不同、可以共存，但**不能把 `httpx` 的 transport 传给 Authlib**：`oidc_login.py` 已整体使用 `httpx2`，因为 Authlib 1.9 会把 transport 交给 `httpx2.AsyncClient`，遇到 v1 的响应流会在 `httpx2` 内部触发 `isinstance(response.stream, AsyncByteStream)` 断言失败。对应测试的 mock transport 也已改成 `httpx2`。

`pyproject.toml` 已显式钉住范围，并把 `openai` 限制在 `<2`；否则全新安装可能解析到 OpenAI 3.x（改用 `httpx2`、不再提供 `httpx`）或 `httpx` 0.29+，造成新旧客户端混用。升级 Starlette、Authlib 或 OpenAI 主版本时，需要同时核对这两条约束，并至少重跑 `python -m pytest tests/test_frontend_hosting.py tests/test_oidc_login.py tests/test_patient_chat.py tests/test_web_search.py -q` 与 `uv pip check`。

## 网关实验配置

`docker-compose.gateway.yml` 与 `deploy/gateway/nginx.conf` 提供独立的 HTTPS 网关配置和容器加固。网关只将端口绑定在本机；要求显式指定 `GATEWAY_TLS_CERT`、`GATEWAY_TLS_KEY` 和 `GATEWAY_BACKEND_NETWORK`，并将后端以 `medical-rag-api:8000` 的网络别名连接到单独创建的内部网络。证书和密钥必须由部署方提供，密钥文件仅授予网关容器用户必要的读取权限；不能使用测试证书处理真实数据。

这只是**未完成集成的实验配置**：该网关尚未连接上文独立的 OIDC 患者应用，也没有完成配套后端容器部署；网关到模拟后端目前是内部网络上的明文 HTTP，部署方还需实现并验证传输保护。网关只做基于连接来源 IP 的限流，尚无账号级限流或完整审计。不允许将 `unsafe-local-demo` 模式通过网关、反向代理或端口转发公开；默认锁定状态不会因部署网关而解除。生产部署需要独立完成网络策略、证书管理、安全评估和机构审批。

网关 Docker 集成测试只创建临时证书、模拟后端、网络和容器，不连接已有医疗数据。需要 Docker 与 OpenSSL：`$env:RUN_GATEWAY_INTEGRATION='1'; python -m pytest tests/test_gateway.py -q`。

## 独立患者 HTTPS 网关（开发验证）

`docker-compose.patient-gateway.yml`、`deploy/patient-gateway/` 与 `src/medical_rag/patient_gateway.py` 提供只面向上述隔离患者入口的独立网关。运维使用严格 TOML（六个显式字段）渲染固定 nginx 配置；渲染器校验公开来源、上游 HTTPS 地址、验证 DNS 名和只读挂载路径，未知/重复键、注入字符和覆盖已有输出都会拒绝，失败只打印静态信息。网关容器沿用固定的 nginx 镜像摘要、非 root `101:101`、只读根文件系统、`cap_drop: ALL`、`no-new-privileges` 和受限 tmpfs；证书、私钥与上游 CA 只作为运维控制的只读挂载，配置中只出现路径，不嵌入或回显密钥内容。

网关要求精确公开 Host，使用 TLS 1.2/1.3、显式上游 CA、SNI 和验证主机名，不提供明文降级；只转发 `Cookie/Origin/Content-Type/Accept/Accept-Language/X-CSRF-Token`，剥离 `Authorization` 和转发身份头，保留应用 `X-Request-ID` 并单独生成 `X-Gateway-Request-ID`。请求体上限 100000 字节，响应不缓冲/不缓存，按连接来源 IP 做 5r/s、burst 20、32 并发的开发准入（429 为静态错误）；**这不是账号级或机构批准的限流**。只允许当前患者路由与 `/assets`，旧 `/ui`、`/api`、`/settings`、`/documents` 不进入上游。Compose 只绑定回环端口并分离 edge 与 internal backend 网络，不部署应用、数据库或 IdP；应用须自行以 TLS 部署并加入 backend 网络。具体配置、渲染、启动和验证命令见 [网关运维说明](deploy/patient-gateway/README.md)。

该网关是开发验证增量，不构成生产网关、账号级限流、完整浏览器登录或医院接入验收。旧 `deploy/gateway/nginx.conf` 明文 HTTP 实验保持独立且未完成，不得改接到此入口。独立复审记录见 [患者网关审查报告](docs/security/patient-gateway-review-report.md)。

## PostgreSQL 隔离实验

`database/migrations/0001_patient_scope.sql` 是一次性实验建表脚本，只覆盖患者主体、会话和会话消息；尚未替换现有 SQLite、记忆、知识库、任务或向量存储，也不会自动迁移和分配旧数据。它没有与应用、登录、授权和密钥管理集成，不能用于真实患者数据。

`src/medical_rag/patient_repository.py` 是独立实验会话仓储，使用患者账号与患者主体的显式绑定、角色、监护关系和健康信息用途授权，并在每个数据库事务内约束机构和患者。它尚未连接业务接口，也不能验证调用者是否真的通过登录或身份核验；所接收的身份和授权仅是调用方提供的快照，无法自行实现即时撤权。授权来源、机构审定的其他未成年人处理规则、身份提供方和审计均未接入，医院病历与外部模型用途也未接入业务流程，因此不能用于真实患者。仓储的 PostgreSQL 集成测试必须在 Docker 环境可用后单独运行：`$env:RUN_POSTGRES_INTEGRATION='1'; python -m pytest tests/test_patient_repository.py -q`。

实验授权策略将健康资料、病历读取和外部模型处理区分为不同用途；授权须匹配机构配置的版本和有效期。未满 14 周岁患者的实验代理路径需要匹配在签署和访问时均有效的已核验监护关系；无个人账号的儿童也可以作为已核验的患者主体。14 至 17 周岁的实验处理路径在机构规则完成审核和实现前默认拒绝。这些检查不能替代医院身份核验、合法有效的授权记录、实时撤权、模型目标审批和合规审查。

实验性行级策略要求在已验证身份与权限后，在每个读写事务中将机构、患者和当前事务 ID 作为一个事务本地值设置；事务 ID 检查防止直接使用旧事务的范围，因此当前方案需要读写事务，不支持只读副本。设置值的代码仍须确保全部身份字段来自当前可信授权上下文，并正确清理连接池，不能依赖数据库设置完成身份验证。运行时数据库角色不得拥有表、创建架构或角色、绕过 RLS、截断表、属于 PostgreSQL 内置特权角色或继承这些权限；实验仓储会调用脚本提供的 `runtime_role_basic_check()`，但业务应用尚未接入该仓储。这只是部分诊断，不检查可执行的 `SECURITY DEFINER` 函数等所有权限，不能代替全面的最小权限审计。能够任意执行 SQL 的数据库客户端仍可伪造范围；数据库超级用户及具有 `BYPASSRLS` 的角色也能绕过策略。生产实现仍须独立完成数据库角色、连接池、可信身份上下文及加密等控制。

可使用隔离的临时 PostgreSQL 容器运行实验测试：`$env:RUN_POSTGRES_INTEGRATION='1'; python -m pytest tests/test_postgres_isolation.py -q`。容器不联网、不发布端口，只使用虚构测试数据。

访问 `http://127.0.0.1:8001/ui/chat`；API 文档在 `http://127.0.0.1:8001/docs`。上传后等待入库任务成功，再开始检索。环境独立使用 Milvus 19531、RabbitMQ 5673、管理界面 15673、集合 `medical_rag_resume_chunks` 和本项目目录下的会话数据；这些服务的数据与原项目不共享。启动前请检查端口是否空闲。已复制的模型 API Key 仍属于同一上游账户，会产生该账户费用；不要在日志或截图中暴露它。

若使用 Vite 开发服务器，可在 `frontend/vite.config.ts` 中将代理目标设为本项目后端端口。独立环境需要重新入库资料；不要把新项目配置改为原项目的集合或消息队列，除非明确接受共享数据的风险。

项目优先读取进程环境变量，然后读取本项目根目录的 `.env`。如果终端中已经设置了 `MILVUS_URI`、`COLLECTION_NAME` 或 `RABBITMQ_URL` 等配置，它们会覆盖独立项目的配置；启动前请检查并清除不希望继承的变量，避免意外连接原项目的服务。

## 测试与评测

```powershell
python -m pytest -q -m 'not integration' --deselect tests/test_medical_health_release_integration.py::test_pinned_sources_remap_all_500_questions_with_default_chunking
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
```

普通自动化测试应使用替身依赖，不应产生真实模型费用。真实模型、Milvus 和 RabbitMQ 的端到端测试需要单独准备环境，且可能产生费用。对比 HyDE 前后，应使用相同语料、索引、题集、模型和其他参数，分别记录 Recall@k、MRR、答案质量、证据不足时的拒答率、延迟及模型成本；尚未进行同口径比较，不能宣称 HyDE 已带来特定提升。

**当前验证状态：** 2026-11-03，客服阶段广范围后端回归 1526 项通过、5 项跳过、344 项按条件排除、2 条既有依赖警告；后续测试诊断修正另有专项验证。已知的 500 题原文锚点映射失败仍明确排除，不能称为完整套件全通过。客服阶段实际 PostgreSQL 及受影响回归 256 项、实际 MFA 启动场景 1 项通过；控制端另复核 31 项支持与隐私测试。客服服务覆盖率 98%，新前端覆盖率 98.97%；导航加载竞态经测试先失败、修复后前端 210 项及 lint、两个构建通过，独立复审通过。虚构数据浏览器预览不是真实认证或全链路验收。此前双因素阶段实际 PostgreSQL 389 项、提供方等组合 90 项通过。参考密码＋动态验证码策略不是机构生产 MFA 认证。旧提供方登出测试的时钟边界误差已通过测试专用固定时钟修正，14 项纯测试及 85 项 PostgreSQL 测试通过，独立审查通过；生产鉴权规则未修改。患者网关阶段：专用严格 TOML/固定 nginx 渲染器与实际 nginx HTTPS→实际隔离应用/临时 PostgreSQL 套件 48 项通过（45 纯配置与 3 集成），纯模块覆盖率 97%，聚焦启动/Host/CSRF 73 项通过，scoped Ruff 通过；旧明文网关与患者应用未改动，该阶段仍只提供连接来源 IP 的开发准入，不是账号级限流或生产部署；独立复审结论为规范符合、质量通过，无 Critical/Important，7 项非阻断 Minor 已记录在实施进度。完整仓库仍有 41 项既有 Ruff 问题，两条依赖弃用警告未隐藏。详细命令、冻结范围与审查记录见 [实施进度](docs/security/progress.md) 及同目录报告；真实医院、临床、法律和生产准入尚未完成。

复现实际 Keycloak 协议测试需 Docker 和报告指定的已缓存镜像，只创建并清理独立虚构数据环境；选中后缺少基础设施会失败，不会替换成模拟身份。测试环境的 `start-dev`/H2 不是生产部署方案。

```powershell
$env:RUN_KEYCLOAK_INTEGRATION='1'
.\.venv\Scripts\python.exe -m pytest tests/test_keycloak_identity.py tests/test_provider_tls.py tests/test_keycloak_runtime.py -q --basetemp outputs/keycloak-check
```

`data/evaluations/medical-health-v1` 包含评测资料与工具；评测块 ID 需要按当前独立索引重新映射，模型裁判分不能替代医学专家判断。部分源资料可能有独立的版权和使用限制，发布前需核对。
