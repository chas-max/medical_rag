"""租户账号管理命令行：``python -m medical_rag.ui.tenants``。

Streamlit 问答界面没有自助注册，账号由本机管理员用这个 CLI 维护。账号和界面
状态共用 ``Settings().ui_state_file`` 指向的本地 SQLite：

    python -m medical_rag.ui.tenants add alice --generate
    python -m medical_rag.ui.tenants list
    python -m medical_rag.ui.tenants reset-password alice --generate
    python -m medical_rag.ui.tenants disable alice
    python -m medical_rag.ui.tenants enable alice
    python -m medical_rag.ui.tenants delete alice

密码输入的优先顺序：``--generate`` 生成随机密码并打印一次；``-p/--password``
直接传明文（会留在 shell 历史与进程列表里，仅限脚本/演示，不建议人工使用）；
两者都没有时用 ``getpass`` 交互输入两次确认。除 ``--generate`` 外任何模式都
不回显密码。
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from collections.abc import Sequence
from datetime import datetime
from getpass import getpass

from ..config import Settings
from .auth import AuthError, Tenant, TenantStore, generate_password
from .storage import UiStoreError

PASSWORD_HELP = "直接指定明文密码（不安全：会留在命令历史/进程列表，仅限非交互脚本）"


def parser() -> argparse.ArgumentParser:
    """构造参数解析器；拆成独立函数便于测试而不真正执行命令。"""
    result = argparse.ArgumentParser(
        prog="python -m medical_rag.ui.tenants",
        description="本机 Streamlit 问答界面的租户账号管理（账号存放在 UI_STATE_FILE）",
    )
    subparsers = result.add_subparsers(dest="command", required=True, metavar="命令")

    add = subparsers.add_parser("add", help="创建租户账号")
    add.add_argument("username", help="登录用户名（3-32 个字符，不区分大小写）")
    add.add_argument("--display-name", default=None, help="界面显示名（默认等于用户名）")
    _add_password_options(add)

    subparsers.add_parser("list", help="列出全部租户账号")

    reset = subparsers.add_parser("reset-password", help="重置租户密码")
    reset.add_argument("target", help="用户名或 tenant_id")
    _add_password_options(reset)

    disable = subparsers.add_parser("disable", help="禁用租户账号（保留数据）")
    disable.add_argument("target", help="用户名或 tenant_id")

    enable = subparsers.add_parser("enable", help="重新启用租户账号")
    enable.add_argument("target", help="用户名或 tenant_id")

    delete = subparsers.add_parser("delete", help="删除租户账号及其登录记录")
    delete.add_argument("target", help="用户名或 tenant_id")
    return result


def _add_password_options(command: argparse.ArgumentParser) -> None:
    group = command.add_mutually_exclusive_group()
    group.add_argument("-p", "--password", default=None, help=PASSWORD_HELP)
    group.add_argument("--generate", action="store_true",
                       help="生成随机密码并打印一次（推荐）")


def _display_width(text: str) -> int:
    """按终端显示宽度计算（中日韩全角字符占两列），让表格对齐。"""
    return sum(
        2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text
    )


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _format_time(value: float) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def format_tenant_table(tenants: Sequence[Tenant]) -> str:
    """把租户列表渲染成对齐的文本表格（ID 只显示前 8 位）。"""
    headers = ("ID", "用户名", "显示名", "禁用", "创建时间")
    rows = [
        (
            tenant.tenant_id[:8],
            tenant.username,
            tenant.display_name,
            "是" if tenant.disabled else "否",
            _format_time(tenant.created_at),
        )
        for tenant in tenants
    ]
    widths = [max(_display_width(cell) for cell in column) for column in zip(headers, *rows)]
    lines = ["  ".join(_pad(cell, width) for cell, width in zip(row, widths)) for row in rows]
    return "\n".join(["  ".join(_pad(cell, width) for cell, width in zip(headers, widths)),
                      *lines])


def _resolve_password(args: argparse.Namespace) -> str | None:
    """按生成/明文/交互三种方式取得密码；交互取消或确认失败返回 None。"""
    if args.generate:
        return generate_password()
    if args.password is not None:
        return args.password
    first = getpass("密码（8-128 个字符）: ")
    second = getpass("再次输入密码: ")
    if first != second:
        print("两次输入的密码不一致，已取消。", file=sys.stderr)
        return None
    return first


def _find_tenant(store: TenantStore, target: str) -> Tenant | None:
    """先按用户名定位，再按 tenant_id 定位（CLI 的 target 两者都接受）。"""
    try:
        tenant = store.get_by_username(target)
    except ValueError:
        tenant = None
    if tenant is not None:
        return tenant
    try:
        return store.get_tenant(target)
    except ValueError:
        return None


def _command_add(args: argparse.Namespace, store: TenantStore) -> int:
    password = _resolve_password(args)
    if password is None:
        return 1
    tenant = store.create_tenant(args.username, password, args.display_name)
    print(f"已创建租户 {tenant.username}（tenant_id={tenant.tenant_id}）")
    if args.generate:
        print(f"初始密码（仅本次显示，请立即保存）: {password}")
    return 0


def _command_list(store: TenantStore) -> int:
    tenants = store.list_tenants()
    if not tenants:
        print("（暂无租户，可用 add 命令创建）")
        return 0
    print(format_tenant_table(tenants))
    return 0


def _command_reset_password(args: argparse.Namespace, store: TenantStore) -> int:
    tenant = _find_tenant(store, args.target)
    if tenant is None:
        print(f"未找到租户：{args.target}", file=sys.stderr)
        return 1
    password = _resolve_password(args)
    if password is None:
        return 1
    store.set_password(tenant.tenant_id, password)
    print(f"已重置租户 {tenant.username} 的密码。")
    if args.generate:
        print(f"新密码（仅本次显示，请立即保存）: {password}")
    return 0


def _command_set_disabled(args: argparse.Namespace, store: TenantStore, disabled: bool) -> int:
    tenant = _find_tenant(store, args.target)
    if tenant is None:
        print(f"未找到租户：{args.target}", file=sys.stderr)
        return 1
    updated = store.set_disabled(tenant.tenant_id, disabled)
    print(f"已{'禁用' if updated.disabled else '启用'}租户 {updated.username}。")
    return 0


def _command_delete(args: argparse.Namespace, store: TenantStore) -> int:
    tenant = _find_tenant(store, args.target)
    if tenant is None:
        print(f"未找到租户：{args.target}", file=sys.stderr)
        return 1
    if not store.delete_tenant(tenant.tenant_id):
        print(f"删除失败：租户 {tenant.username} 已不存在", file=sys.stderr)
        return 1
    print(f"已删除租户 {tenant.username}（tenant_id={tenant.tenant_id}）。")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """执行一个子命令：成功返回 0，业务失败返回 1，参数错误由 argparse 返回 2。"""
    args = parser().parse_args(argv)
    try:
        store = TenantStore(Settings().ui_state_file)
        if args.command == "add":
            return _command_add(args, store)
        if args.command == "list":
            return _command_list(store)
        if args.command == "reset-password":
            return _command_reset_password(args, store)
        if args.command == "disable":
            return _command_set_disabled(args, store, True)
        if args.command == "enable":
            return _command_set_disabled(args, store, False)
        if args.command == "delete":
            return _command_delete(args, store)
    except (AuthError, UiStoreError, ValueError) as error:
        print(f"操作失败：{error}", file=sys.stderr)
        return 1
    # argparse 的 required 子命令已排除未知命令，这里只是防御。
    print(f"未知命令：{args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
