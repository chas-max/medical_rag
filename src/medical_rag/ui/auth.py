"""租户账号、密码哈希与登录校验。

Streamlit 问答界面是给本机演示用的多租户入口：每个操作员账号是一个租户，
账号与登录失败记录和界面状态共用同一个本地 SQLite 文件（``UI_STATE_FILE``）。
这里只做密码学上正确的口令存储（stdlib ``hashlib.scrypt``）和简单的登录
锁定，不涉及令牌、会话或网络身份；会话空闲超时由界面层负责。

安全取舍：
- 用户名不存在、密码错误、账号被禁用都抛同一个 :class:`InvalidCredentialsError`，
  避免通过错误信息枚举账号。
- 失败次数按用户名在锁定窗口内统计，达到上限后抛 :class:`AccountLockedError`；
  锁定期间不再累加失败记录，避免攻击者把锁无限延长。
- 任何日志、异常信息与 ``repr`` 都不包含明文密码或密码哈希。

``UiSqliteStore`` 负责连接、busy timeout 与把底层错误包装成
:class:`~medical_rag.ui.storage.UiStoreError`；本模块只复用它的 ``SCHEMA`` 与
``connect()``，不修改该基类。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
import secrets
import sqlite3
import string
import time
from dataclasses import dataclass
from functools import lru_cache
from uuid import uuid4

from .storage import UiSqliteStore

# scrypt 参数：OWASP 推荐的本地口令存储下限（约 16 MiB 内存、单次约几十毫秒）。
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

MIN_PASSWORD_CHARS = 8
MAX_PASSWORD_CHARS = 128
MIN_USERNAME_CHARS = 3
MAX_USERNAME_CHARS = 32
MAX_DISPLAY_NAME_CHARS = 80

# 用户名统一小写后匹配；不允许中文、空格和首尾符号，避免和 tenant_id 混淆。
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")

# 生成密码用的字符集：去掉容易混淆的 0/O、1/l/I 之外，大小写数字符号都要有。
GENERATED_PASSWORD_SYMBOLS = "!@#$%^&*()-_=+"
GENERATED_PASSWORD_ALPHABET = (
    string.ascii_uppercase + string.ascii_lowercase + string.digits + GENERATED_PASSWORD_SYMBOLS
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ui_tenants (
    tenant_id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ui_auth_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    attempted_at REAL NOT NULL,
    success INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ui_auth_attempts_username_time
    ON ui_auth_attempts(username, attempted_at DESC);
"""


class AuthError(Exception):
    """租户/登录相关错误的基类。"""


class InvalidCredentialsError(AuthError):
    """用户名不存在、密码错误或账号已禁用（统一异常，避免枚举账号）。"""


class AccountLockedError(AuthError):
    """同一用户名在锁定窗口内失败次数过多。"""

    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        wait = max(1, math.ceil(self.retry_after_seconds))
        super().__init__(f"登录失败次数过多，请在 {wait} 秒后重试")


class TenantExistsError(AuthError):
    """用户名已被占用。"""


@dataclass(frozen=True)
class Tenant:
    """一个本地操作员账号（不含密码哈希，避免哈希随对象流出）。"""

    tenant_id: str
    username: str
    display_name: str
    disabled: bool
    created_at: float
    updated_at: float


# ---------------------------------------------------------------------------
# 用户名 / 密码基础校验
# ---------------------------------------------------------------------------
def normalize_username(value: str) -> str:
    """规范化用户名：去除首尾空白并转小写，同时校验格式。

    合法格式为 3-32 个字符，以字母或数字开头，只含小写字母、数字、下划线、
    点或连字符；非法时抛 :class:`ValueError`（中文错误信息）。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("用户名不能为空")
    username = value.strip().lower()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError(
            "用户名需为 3-32 个字符，以字母或数字开头，"
            "且只能包含小写字母、数字、下划线、点或连字符"
        )
    return username


def _normalize_display_name(value: str | None, username: str) -> str:
    """显示名默认等于用户名；只做去空白与长度校验，不改大小写。"""
    if value is None:
        return username
    if not isinstance(value, str):
        raise ValueError("显示名称必须是文本")
    name = value.strip()
    if not name:
        return username
    if len(name) > MAX_DISPLAY_NAME_CHARS:
        raise ValueError(f"显示名称不能超过 {MAX_DISPLAY_NAME_CHARS} 个字符")
    return name


def validate_password(password: str) -> str:
    """校验密码长度（8..128 字符）并返回原值，非法时抛 :class:`ValueError`。"""
    if not isinstance(password, str):
        raise ValueError("密码必须是文本")
    if not MIN_PASSWORD_CHARS <= len(password) <= MAX_PASSWORD_CHARS:
        raise ValueError(
            f"密码长度必须在 {MIN_PASSWORD_CHARS} 到 {MAX_PASSWORD_CHARS} 个字符之间"
        )
    return password


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


def _scrypt(password: str, salt: bytes, *, n: int, r: int, p: int, dklen: int) -> bytes:
    # stdlib scrypt 只接受字节串；使用 UTF-8 保证跨平台一致。
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen
    )


def hash_password(password: str) -> str:
    """生成 ``scrypt$16384$8$1$<salt_b64>$<hash_b64>`` 格式的自描述哈希。"""
    validate_password(password)
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
    return (
        f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}"
        f"${_b64encode(salt)}${_b64encode(digest)}"
    )


def verify_password(password: str, encoded: str) -> bool:
    """常量时间校验密码；编码损坏、参数异常或密码类型不对时返回 ``False``。"""
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    try:
        scheme, n_text, r_text, p_text, salt_text, hash_text = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_text), int(r_text), int(p_text)
        # 拒绝明显不合理的参数，避免损坏数据导致巨额内存/CPU 开销。
        if not (2**10 <= n <= 2**20) or not (1 <= r <= 32) or not (1 <= p <= 16):
            return False
        salt = _b64decode(salt_text)
        expected = _b64decode(hash_text)
        if len(salt) < SALT_BYTES or not expected:
            return False
        actual = _scrypt(password, salt, n=n, r=r, p=p, dklen=len(expected))
    except (ValueError, TypeError, UnicodeError):
        return False
    return hmac.compare_digest(actual, expected)


def generate_password(length: int = 16) -> str:
    """生成便于打印给用户的一次性随机密码（含大小写字母、数字和符号）。"""
    if not isinstance(length, int) or isinstance(length, bool):
        raise ValueError("密码长度必须是整数")
    if not MIN_PASSWORD_CHARS <= length <= MAX_PASSWORD_CHARS:
        raise ValueError(
            f"密码长度必须在 {MIN_PASSWORD_CHARS} 到 {MAX_PASSWORD_CHARS} 个字符之间"
        )
    # 先各取一个必备字符，其余随机，最后洗牌，确保字符多样性。
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(GENERATED_PASSWORD_SYMBOLS),
    ]
    required.extend(
        secrets.choice(GENERATED_PASSWORD_ALPHABET) for _ in range(length - len(required))
    )
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """账号不存在时也做一次真实的 scrypt 校验，弱化用户名枚举的时间侧信道。"""
    return hash_password(secrets.token_urlsafe(24))


def _row_to_tenant(row: sqlite3.Row) -> Tenant:
    return Tenant(
        tenant_id=str(row["tenant_id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        disabled=bool(row["disabled"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _require_tenant_id(tenant_id: str) -> str:
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("租户 ID 不能为空")
    return tenant_id.strip()


class TenantStore(UiSqliteStore):
    """租户账号表与登录失败记录表。

    每个方法使用短连接并在方法内提交；由父类统一处理目录创建、busy timeout
    与 SQLite 错误包装。所有 SQL 都是参数化的。
    """

    SCHEMA = SCHEMA

    # -- 账号 CRUD ----------------------------------------------------------
    def create_tenant(
        self,
        username: str,
        password: str,
        display_name: str | None = None,
    ) -> Tenant:
        """创建租户；用户名重复抛 :class:`TenantExistsError`。"""
        name = normalize_username(username)
        password_hash = hash_password(password)
        label = _normalize_display_name(display_name, name)
        now = time.time()
        tenant = Tenant(
            tenant_id=uuid4().hex,
            username=name,
            display_name=label,
            disabled=False,
            created_at=now,
            updated_at=now,
        )
        with self.connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO ui_tenants("
                    "tenant_id, username, display_name, password_hash, disabled, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        tenant.tenant_id,
                        tenant.username,
                        tenant.display_name,
                        password_hash,
                        0,
                        tenant.created_at,
                        tenant.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise TenantExistsError(f"用户名 {name} 已存在") from error
            connection.commit()
        return tenant

    def list_tenants(self) -> list[Tenant]:
        """按创建时间升序列出全部租户。"""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT tenant_id, username, display_name, disabled, created_at, updated_at "
                "FROM ui_tenants ORDER BY created_at ASC, username ASC"
            ).fetchall()
        return [_row_to_tenant(row) for row in rows]

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        """按 tenant_id 读取租户，不存在返回 ``None``。"""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT tenant_id, username, display_name, disabled, created_at, updated_at "
                "FROM ui_tenants WHERE tenant_id = ?",
                (_require_tenant_id(tenant_id),),
            ).fetchone()
        return _row_to_tenant(row) if row is not None else None

    def get_by_username(self, username: str) -> Tenant | None:
        """按规范化用户名读取租户，不存在返回 ``None``；格式非法抛 ValueError。"""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT tenant_id, username, display_name, disabled, created_at, updated_at "
                "FROM ui_tenants WHERE username = ?",
                (normalize_username(username),),
            ).fetchone()
        return _row_to_tenant(row) if row is not None else None

    def set_disabled(self, tenant_id: str, disabled: bool) -> Tenant:
        """启用/禁用租户并返回更新后的记录；租户不存在抛 :class:`AuthError`。"""
        tenant = _require_tenant_id(tenant_id)
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                "UPDATE ui_tenants SET disabled = ?, updated_at = ? WHERE tenant_id = ?",
                (int(bool(disabled)), now, tenant),
            )
            row = connection.execute(
                "SELECT tenant_id, username, display_name, disabled, created_at, updated_at "
                "FROM ui_tenants WHERE tenant_id = ?",
                (tenant,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise AuthError(f"租户 {tenant} 不存在")
        return _row_to_tenant(row)

    def set_password(self, tenant_id: str, password: str) -> Tenant:
        """重置密码并返回更新后的记录；租户不存在抛 :class:`AuthError`。"""
        tenant = _require_tenant_id(tenant_id)
        password_hash = hash_password(password)
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                "UPDATE ui_tenants SET password_hash = ?, updated_at = ? WHERE tenant_id = ?",
                (password_hash, now, tenant),
            )
            row = connection.execute(
                "SELECT tenant_id, username, display_name, disabled, created_at, updated_at "
                "FROM ui_tenants WHERE tenant_id = ?",
                (tenant,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise AuthError(f"租户 {tenant} 不存在")
        return _row_to_tenant(row)

    def delete_tenant(self, tenant_id: str) -> bool:
        """删除租户及其登录失败记录；返回是否真的删除了账号。"""
        tenant = _require_tenant_id(tenant_id)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT username FROM ui_tenants WHERE tenant_id = ?", (tenant,)
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "DELETE FROM ui_auth_attempts WHERE username = ?", (str(row["username"]),)
            )
            changed = connection.execute(
                "DELETE FROM ui_tenants WHERE tenant_id = ?", (tenant,)
            ).rowcount
            connection.commit()
        return bool(changed)

    # -- 登录校验 -----------------------------------------------------------
    def verify_login(
        self,
        username: str,
        password: str,
        *,
        max_attempts: int = 5,
        lockout_seconds: int = 300,
    ) -> Tenant:
        """校验用户名/密码，成功返回租户，失败抛类型化异常。

        - 锁定窗口内该用户名的失败次数达到 ``max_attempts`` 时抛
          :class:`AccountLockedError`，``retry_after_seconds`` 为窗口剩余秒数；
        - 用户名不存在、密码错误、账号禁用统一抛
          :class:`InvalidCredentialsError`；
        - 每次真正执行的尝试都会写入 ``ui_auth_attempts``；成功时清空该用户名
          的失败记录，让下一次锁定从零开始（成功记录保留用于审计）。
        """
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("max_attempts 必须是正整数")
        if (
            not isinstance(lockout_seconds, (int, float))
            or isinstance(lockout_seconds, bool)
            or lockout_seconds <= 0
        ):
            raise ValueError("lockout_seconds 必须大于 0")

        try:
            normalized: str | None = normalize_username(username)
        except ValueError:
            normalized = None
        attempt_name = (
            username.strip().lower() if isinstance(username, str) else ""
        )

        now = time.time()
        self._raise_if_locked(
            normalized or attempt_name,
            now=now,
            max_attempts=max_attempts,
            lockout_seconds=float(lockout_seconds),
        )

        tenant = None
        if normalized is not None:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT tenant_id, username, display_name, disabled, created_at, "
                    "updated_at FROM ui_tenants WHERE username = ?",
                    (normalized,),
                ).fetchone()
            if row is not None:
                tenant = _row_to_tenant(row)

        if tenant is None:
            # 统一走一次真实 scrypt，避免"账号不存在"明显更快。
            candidate_hash = _dummy_password_hash()
        else:
            candidate_hash = self._password_hash(tenant.tenant_id)
        password_ok = bool(candidate_hash) and verify_password(password, candidate_hash or "")
        success = tenant is not None and not tenant.disabled and password_ok

        with self.connect() as connection:
            if success:
                # 成功即清零失败计数，避免历史失败继续触发锁定。
                connection.execute(
                    "DELETE FROM ui_auth_attempts WHERE username = ? AND success = 0",
                    (normalized or attempt_name,),
                )
            connection.execute(
                "INSERT INTO ui_auth_attempts(username, attempted_at, success) VALUES (?, ?, ?)",
                (normalized or attempt_name, now, int(success)),
            )
            connection.commit()

        if not success or tenant is None:
            raise InvalidCredentialsError("用户名或密码错误")
        return tenant

    def password_matches(self, tenant_id: str, password: str) -> bool:
        """Check a password without recording an attempt or applying lockout.

        Only the demo-login bootstrap uses this: it must verify that an existing
        account still accepts the credential it is about to pre-fill, without
        polluting the audit trail or counting a failed attempt.
        """
        if not isinstance(password, str) or not password:
            return False
        encoded = self._password_hash(tenant_id)
        return bool(encoded) and verify_password(password, encoded)

    def _password_hash(self, tenant_id: str) -> str | None:
        """读取密码哈希供校验；该值只在 :meth:`verify_login` 内部使用。"""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM ui_tenants WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
        return str(row["password_hash"]) if row is not None else None

    def _raise_if_locked(
        self,
        username: str,
        *,
        now: float,
        max_attempts: int,
        lockout_seconds: float,
    ) -> None:
        """窗口内失败次数达到上限则抛锁定异常，并计算解锁剩余时间。"""
        window_start = now - lockout_seconds
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT attempted_at FROM ui_auth_attempts "
                "WHERE username = ? AND success = 0 AND attempted_at >= ? "
                "ORDER BY attempted_at DESC LIMIT ?",
                (username, window_start, max_attempts),
            ).fetchall()
        if len(rows) < max_attempts:
            return
        # 最近 max_attempts 次失败里最早的一次滑出窗口时，窗口内失败数才会降到
        # 上限以下，因此它加锁定窗口就是最早可重试的时刻。
        oldest_counted = min(float(row["attempted_at"]) for row in rows)
        retry_after = oldest_counted + lockout_seconds - now
        raise AccountLockedError(retry_after)

    # -- 审计 ---------------------------------------------------------------
    def recent_auth_events(self, limit: int = 50) -> list[dict[str, object]]:
        """最近的登录尝试记录（新的在前）：username / attempted_at / success。"""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT username, attempted_at, success FROM ui_auth_attempts "
                "ORDER BY attempted_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "username": str(row["username"]),
                "attempted_at": float(row["attempted_at"]),
                "success": bool(row["success"]),
            }
            for row in rows
        ]
