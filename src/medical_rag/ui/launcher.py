"""Launcher for the persistent Streamlit viewer: ``python -m medical_rag.ui``.

Streamlit's CLI is invoked in-process so the viewer inherits this project's
environment resolution (``.env`` relative paths, ``UI_*`` settings) and so a
mistyped option fails with one clear message instead of a stack trace. The
viewer is a local operations page: non-loopback hosts are rejected on purpose.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from collections.abc import Sequence
from pathlib import Path

DEFAULT_STATE_FILE = "data/streamlit/ui-state.sqlite3"
DEFAULT_HISTORY_FILE = "data/chat-history.sqlite3"
DEFAULT_REPORT_DIRS = "outputs/reports"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_PORT = 8501
APP_PATH = Path(__file__).with_name("app.py")

INSTALL_HINT = (
    '缺少 Streamlit。请先在项目根目录安装可选依赖：\n'
    '  uv pip install --python .venv\\Scripts\\python.exe -e ".[ui]"\n'
    '或使用：.\\start-ui.ps1 -Install'
)


def parser() -> argparse.ArgumentParser:
    """Command line options; every path override is only an environment override."""
    result = argparse.ArgumentParser(
        prog="python -m medical_rag.ui",
        description="本机只读的 Streamlit 持久化展示界面（会话历史、文档任务、评测报告）",
    )
    result.add_argument("--host", default="127.0.0.1",
                        help="监听地址，仅允许回环地址（默认 127.0.0.1）")
    result.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"监听端口（默认 {DEFAULT_PORT}）")
    result.add_argument("--state-file", default=None,
                        help="界面状态数据库（默认 UI_STATE_FILE 或 "
                             f"{DEFAULT_STATE_FILE}）")
    result.add_argument("--history-file", default=None,
                        help="只读打开的会话/任务数据库（默认 CHAT_HISTORY_FILE 或 "
                             f"{DEFAULT_HISTORY_FILE}）")
    result.add_argument("--reports-dir", action="append", default=None, metavar="DIR",
                        help="评测报告目录，可重复；默认 UI_REPORT_DIRS 或 "
                             f"{DEFAULT_REPORT_DIRS}")
    result.add_argument("--no-browser", action="store_true",
                        help="启动后不自动打开浏览器")
    return result


def validate_options(args: argparse.Namespace) -> None:
    """Reject combinations the viewer must never run, before importing Streamlit."""
    if args.host not in LOOPBACK_HOSTS:
        raise ValueError("展示界面只允许绑定本机回环地址（127.0.0.1 / localhost / ::1）")
    if not 1 <= args.port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    for name, value in (("--state-file", args.state_file),
                        ("--history-file", args.history_file)):
        if value is not None and not str(value).strip():
            raise ValueError(f"{name} 不能为空")
    if args.reports_dir is not None:
        if not any(item.strip() for item in args.reports_dir):
            raise ValueError("--reports-dir 不能为空")


def environment_overrides(args: argparse.Namespace) -> dict[str, str]:
    """Translate CLI overrides into the settings variables the app reads."""
    overrides: dict[str, str] = {}
    if args.state_file:
        overrides["UI_STATE_FILE"] = str(args.state_file)
    if args.history_file:
        overrides["CHAT_HISTORY_FILE"] = str(args.history_file)
    if args.reports_dir:
        overrides["UI_REPORT_DIRS"] = ",".join(item.strip() for item in args.reports_dir)
    return overrides


def build_streamlit_args(host: str, port: int) -> list[str]:
    """``streamlit run`` arguments, including the service-mode switches."""
    return [
        "streamlit", "run", str(APP_PATH),
        "--server.address", host,
        "--server.port", str(port),
        # Headless + no usage stats: this is a local service started by scripts,
        # not an interactive tutorial. File watching is off to keep CPU and the
        # writable state surface minimal.
        "--server.headless", "true",
        "--server.fileWatcherType", "none",
        "--browser.gatherUsageStats", "false",
        "--client.toolbarMode", "minimal",
    ]


def _open_browser_later(url: str) -> None:
    def open_when_ready() -> None:
        try:
            webbrowser.open(url, new=2)
        except Exception:  # noqa: BLE001 - a missing browser must not stop the viewer
            pass

    timer = threading.Timer(2.5, open_when_ready)
    timer.daemon = True
    timer.start()


def main(argv: Sequence[str] | None = None) -> int:
    """Parse options, apply them to the environment and run Streamlit."""
    args = parser().parse_args(argv)
    try:
        validate_options(args)
    except ValueError as error:
        print(f"启动失败：{error}", file=sys.stderr)
        return 2

    try:
        from streamlit.web import cli as stcli
    except ModuleNotFoundError:
        print(INSTALL_HINT, file=sys.stderr)
        return 2

    for name, value in environment_overrides(args).items():
        os.environ[name] = value

    url = f"http://{args.host}:{args.port}"
    print("medical-rag Streamlit 展示界面")
    print(f"  地址: {url}")
    print(f"  状态文件: {args.state_file or os.environ.get('UI_STATE_FILE') or DEFAULT_STATE_FILE}")
    print(f"  会话数据库(只读): {args.history_file or os.environ.get('CHAT_HISTORY_FILE') or DEFAULT_HISTORY_FILE}")
    print(f"  评测报告目录: {args.reports_dir and ','.join(args.reports_dir) or os.environ.get('UI_REPORT_DIRS') or DEFAULT_REPORT_DIRS}")
    print("  停止: Ctrl+C（或由 start-ui.ps1 -Stop 管理后台进程）")

    if not args.no_browser:
        _open_browser_later(url)

    sys.argv = build_streamlit_args(args.host, args.port)
    try:
        result = stcli.main()
    except SystemExit as exit_signal:  # Streamlit's CLI may terminate explicitly.
        code = exit_signal.code
        return code if isinstance(code, int) else 0
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
