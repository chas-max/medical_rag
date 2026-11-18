# 日志设施（Loguru）需求与边界

状态：已实现，等待独立复审。适用范围：`api` / `relay` / `ingest` / `memory` 进程，以及默认关闭的患者门户日志。未通过生产医疗数据准入。

## 需求

1. 用 Loguru 统一应用与第三方库（uvicorn/httpx/httpclient/openai/pymilvus/aio_pika/sentence-transformers/transformers/psycopg）的日志出口。
2. 按进程分文件：`outputs/logs/<role>/<role>.log`，与 `start.ps1` 重定向的 `outputs/logs/<role>.log` 不冲突。
3. 轮转（默认 20 MB）、有效期（默认 14 天）、压缩（默认 zip，可用 `gz/bz2/xz/tar`，留空表示不压缩）。
4. 输出格式默认可读文本；`LOG_JSON=true` 时文件与控制台都改为单行 JSON。
5. 患者门户新增可选 `[logging]` TOML 段，**默认关闭**。

## 硬性边界

- 允许记录：固定的运维/降级短语、枚举化的状态与计数、`role` 名、固定告警文本。
- 禁止记录：问题原文、答案正文、患者记录与标识、账号/租户标识、API Key/token、模型 endpoint、DSN、文件路径、请求体与响应体。
- 禁止 `logger.exception(...)`/`exc_info`；`backtrace=False`、`diagnose=False` 必须固定，`diagnose` 会把局部变量值写入 traceback，属于硬性泄露点。
- 凭证样式（`sk-…`、`Bearer …`、`scheme://user:pass@host`、`password=`/`api_key=`）必须由 patcher 兜底遮蔽，即使调用点写错也不落盘。
- 初始化只发生在进程入口；模块导入期不得配置日志，否则测试会在仓库落盘。
- 患者门户关闭时行为与改造前一致：stderr 为空、不创建文件、不安装任何 sink；`uvicorn` 的 `NullHandler` 静默与 `print(StartupError())` 静态输出保留。
- 患者门户开启时只记录启动/关闭、preflight 结果、非回环监听告警、启动失败固定短语；请求级与患者级证据继续只进 PostgreSQL `security_audit_events`，不在日志重复。
- 测试不得产生真实模型费用，也不得在仓库落盘；`caplog` 语义必须保持"真捕获"，不允许退化成恒真断言。

## 配置面

- `Settings`：`LOG_ENABLED`、`LOG_DIR`、`LOG_LEVEL`、`LOG_ROTATION`、`LOG_RETENTION`、`LOG_COMPRESSION`、`LOG_JSON`。非法 `rotation/retention/compression/level` 回退默认值，不让进程启动失败。
- 患者 TOML `[logging]`：`enabled`（必须是真 `bool`）、`directory`、`level`、`rotation`、`retention`、`compression`、`json`。任何类型/取值/长度越界一律 `StartupError()`，不泄露路径或 DSN；`enabled=false` 时不解析其余键的语义（仍校验白名单）。

## 非目标

- 不引入集中式日志采集、日志服务端或密钥托管。
- 不把日志当作审计存储；审计仍以 PostgreSQL 表为唯一权威。
- 不改变原有访问模式锁定与患者/旧入口隔离。
- 不以本改造声称任何生产安全、医学或法律验收。
