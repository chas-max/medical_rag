# 日志设施（Loguru）实施与验证记录

状态：已实现，**未通过独立复审**，未通过生产医疗数据准入。范围仅限本机虚构数据演示。

## 实施

- 新增 `src/medical_rag/logging_setup.py`：`configure_logging(role, *, settings, patient)` 是唯一初始化点，幂等；`logger.remove()` 后重建 sink；文件 sink 带 `rotation`/`retention`/`compression`/`serialize`、`enqueue=True`、`catch=True`、`backtrace=False`、`diagnose=False`、`encoding="utf-8"`。附 `normalize_*`/`is_valid_*`（Settings 用回退、患者 TOML 用拒绝）、`redact()` 与 `logger.configure(patcher=...)`、`InterceptHandler`、`silence_logging()`、`flush_logging()`、`reset_logging()`。
- 新增 `src/medical_rag/api_server.py`：`configure_logging("api")` 后再导入 `app`，`uvicorn.run(..., log_config=None, access_log=False)`，`finally` 中 `flush_logging()`。`start.ps1` 的 api 启动改为 `python -m medical_rag.api_server --host 127.0.0.1 --port 8001`。
- 10 个业务模块从 `logging.getLogger` 改为 `from loguru import logger`：`api.py`、`chat_runtime.py`、`conversation_index.py`、`embeddings.py`、`jobs.py`、`memory_worker.py`、`settings_store.py`、`summarize.py`、`tokenization.py`、`worker.py`。`worker.py` 的 3 处 `%s` 惰性格式化改为 f-string（Loguru 不做 `%` 插值），`main()` 用 `configure_logging(role, settings=settings)` 取代 `logging.basicConfig`，`finally` 中 `flush_logging()`。
- `security_config.py`：新增 `PatientLoggingConfig` 与严格 `[logging]` 解析（`enabled` 必须是真 `bool`；`directory` 相对 TOML 解析且长度受限；`level/rotation/retention/compression` 走白名单；`json` 必须是 `bool`；任何越界统一 `StartupError()`）。
- `security_server.py`：保留 `import logging` 的 uvicorn `NullHandler` 静默；`main()` 先 `silence_logging()`，再按 TOML 配置 sink，只新增启动/关闭、preflight 通过、非回环监听告警、启动失败四条固定短语记录；`finally` 中 `flush_logging()`。`_application()` 里的配置是纯增量：`enabled=false` 时不安装任何 sink。
- `config.py`：`Settings` 新增 `log_enabled`、`log_dir`、`log_level`、`log_rotation`、`log_retention`、`log_compression`、`log_json`。
- 测试：`tests/conftest.py` 用 Loguru sink 覆盖同名 `caplog` fixture（暴露 `.text`/`.records`/无操作 `.set_level()`，`exc_info` 由 `record["exception"]` 映射），既有脱敏断言因此没有退化成恒真；autouse 把 `LOG_DIR` 指向 `tmp_path`，任何走到真实入口的测试不会在仓库落盘；新增 `tests/test_logging_setup.py`（轮转/压缩/保留期清理/JSON/关闭不落盘/脱敏/`diagnose` 关闭/stdlib 拦截/患者默认关闭）与 `tests/test_security_server.py` 的 `[logging]` 配置用例。

## 验证

| 命令 | 结果 |
| --- | --- |
| `python -m ruff check src tests` | All checks passed |
| `python -m pytest -q -m "not integration" --deselect tests/test_medical_health_release_integration.py::test_pinned_sources_remap_all_500_questions_with_default_chunking` | 1901 passed, 5 skipped, 372 deselected, 1 warning in 178.12s |
| `python -m pytest tests/test_logging_setup.py tests/test_security_server.py -q -m "not integration"` | 99 passed, 14 deselected |
| `python -m pytest tests/test_log_redaction.py tests/test_stream_error_safety.py tests/test_worker_cli.py tests/test_support_cases.py tests/test_security_audit.py -q -m "not integration"` | 46 passed, 62 deselected |

另做一次端到端冒烟：`LOG_DIR=<临时目录> python -m medical_rag.api_server --host 127.0.0.1 --port 8123`，`GET /health` 返回 200，生成 `<临时目录>/api/api.log`；uvicorn 的记录以 `uvicorn.error` 名称进入同一文件、没有重复行，验证了 `log_config=None` 确实避免了双重输出。

未执行：需要真实 Milvus/RabbitMQ/PostgreSQL/Keycloak 的集成组（本轮未改动其行为），以及已知失败的 500 题锚点映射用例（与日志无关，保持原先排除口径）。

仓库内没有新增日志产物：`outputs/logs/` 下仍然只有 `start.ps1` 重定向的 stdout/stderr 文件，没有 `<role>/` 子目录。

## 未覆盖与后续

- 未做独立复审；按仓库节奏需要 `logging-review.diff` 冻结审查包与独立复审结论后才能记为完成。
- 轮转/保留期/压缩仅在 `tmp_path` 的单元用例里验证，未做长时间运行观察。
- 患者门户日志开启路径没有真实机构环境验证，只验证了配置解析与静态短语写入。
- 未评估日志落盘对磁盘配额、备份策略与日志留存合规的影响，仍是生产准入清单之外的事项。
