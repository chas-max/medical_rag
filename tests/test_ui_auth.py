"""租户账号、登录锁定与租户 CLI 的单元测试。

测试全部使用 ``tmp_path`` + ``UI_STATE_FILE`` 环境变量隔离，互不共享数据库。
真实 scrypt（n=16384）单次约 0.1-0.2 秒，因此除专门的集成用例外，统一把
``auth._scrypt`` 换成便宜的 PBKDF2 替身：编码格式、盐与校验逻辑保持不变，
但整套用例可以在秒级跑完；真实参数与集成行为由
``test_real_scrypt_integration_and_production_constants`` 覆盖。
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import re
import time

import pytest

from medical_rag.config import Settings
from medical_rag.ui import auth, tenants
from medical_rag.ui.auth import (
    AccountLockedError,
    AuthError,
    InvalidCredentialsError,
    Tenant,
    TenantExistsError,
    TenantStore,
)

# 在 autouse 补丁之前保存真实实现，供唯一的真实 scrypt 集成测试恢复使用。
_REAL_SCRYPT = auth._scrypt

FAST_PASSWORD = "Passw0rd!x"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _cheap_scrypt(monkeypatch):
    """用等价语义的便宜 KDF 替换 scrypt，避免每个账号创建都付出百毫秒级开销。"""

    def cheap_scrypt(password, salt, *, n, r, p, dklen):
        del n, r, p
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 1000, dklen)

    monkeypatch.setattr(auth, "_scrypt", cheap_scrypt)


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """每个测试独立的 UI 状态库；CLI 通过 Settings() 读取该环境变量。"""
    path = tmp_path / "ui-state.sqlite3"
    monkeypatch.setenv("UI_STATE_FILE", str(path))
    return path


@pytest.fixture
def store(state_file):
    return TenantStore(Settings().ui_state_file)


def _insert_failures(store: TenantStore, username: str, count: int, attempted_at: float) -> None:
    """直接写入历史失败记录，用于模拟锁定窗口而不真正 sleep。"""
    with store.connect() as connection:
        for _ in range(count):
            connection.execute(
                "INSERT INTO ui_auth_attempts(username, attempted_at, success) VALUES (?, ?, 0)",
                (username, attempted_at),
            )
        connection.commit()


def _create(store: TenantStore, username: str = "alice") -> Tenant:
    return store.create_tenant(username, FAST_PASSWORD)


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------
def test_production_scrypt_constants():
    assert (
        auth.SCRYPT_N,
        auth.SCRYPT_R,
        auth.SCRYPT_P,
        auth.SCRYPT_DKLEN,
        auth.SALT_BYTES,
    ) == (2**14, 8, 1, 32, 16)
    assert auth.MAX_PASSWORD_CHARS == 128


def test_hash_encoding_format_and_roundtrip():
    encoded = auth.hash_password(FAST_PASSWORD)
    scheme, n_text, r_text, p_text, salt_text, hash_text = encoded.split("$")
    assert (scheme, n_text, r_text, p_text) == ("scrypt", "16384", "8", "1")
    assert len(base64.b64decode(salt_text)) == auth.SALT_BYTES
    assert len(base64.b64decode(hash_text)) == auth.SCRYPT_DKLEN
    assert auth.verify_password(FAST_PASSWORD, encoded) is True
    assert auth.verify_password(FAST_PASSWORD + "x", encoded) is False
    # 随机盐：同一密码两次编码必须不同，且各自可验证。
    other = auth.hash_password(FAST_PASSWORD)
    assert other != encoded
    assert auth.verify_password(FAST_PASSWORD, other) is True


def test_real_scrypt_integration_and_production_constants(monkeypatch):
    """至少一次走真实 scrypt：编码里的参数确实生效，且能与 stdlib 结果对齐。"""
    monkeypatch.setattr(auth, "_scrypt", _REAL_SCRYPT)
    encoded = auth.hash_password(FAST_PASSWORD)
    scheme, n_text, r_text, p_text, salt_text, hash_text = encoded.split("$")
    assert (scheme, n_text, r_text, p_text) == ("scrypt", "16384", "8", "1")
    salt = base64.b64decode(salt_text)
    digest = base64.b64decode(hash_text)
    assert len(salt) == 16 and len(digest) == 32
    expected = hashlib.scrypt(
        FAST_PASSWORD.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )
    assert digest == expected
    assert auth.verify_password(FAST_PASSWORD, encoded) is True
    assert auth.verify_password("Wrong!pass0", encoded) is False


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "plaintext",
        "scrypt$16384$8$1$onlyfiveparts",
        "bcrypt$16384$8$1$c2FsdHNhbHRzYWx0c2E=$aGFzaGhhc2hoYXNoaGFzaA==",
        "scrypt$16384$8$1$!!!$!!!",
        "scrypt$99999$8$1$c2FsdHNhbHRzYWx0c2E=$aGFzaGhhc2hoYXNoaGFzaA==",
        "scrypt$16384$0$1$c2FsdHNhbHRzYWx0c2E=$aGFzaGhhc2hoYXNoaGFzaA==",
        "scrypt$16384$8$1$c2FsdA==$aGFzaGhhc2hoYXNoaGFzaA==",
        "scrypt$16384$8$1$c2FsdHNhbHRzYWx0c2E=$",
    ],
)
def test_verify_password_rejects_malformed_encodings(encoded):
    assert auth.verify_password(FAST_PASSWORD, encoded) is False


def test_verify_password_rejects_non_text_inputs():
    encoded = auth.hash_password(FAST_PASSWORD)
    assert auth.verify_password(None, encoded) is False
    assert auth.verify_password(123, encoded) is False
    assert auth.verify_password(FAST_PASSWORD, None) is False
    assert auth.verify_password(FAST_PASSWORD, 123) is False


@pytest.mark.parametrize("password", ["", "short", "x" * 129, None, 123])
def test_hash_password_rejects_invalid_passwords(password):
    with pytest.raises(ValueError):
        auth.hash_password(password)


@pytest.mark.parametrize("length", [7, 0, -1, 129, True, 12.5, "16"])
def test_generate_password_rejects_invalid_length(length):
    with pytest.raises(ValueError):
        auth.generate_password(length)


def test_generate_password_length_diversity_and_distinctness():
    assert len(auth.generate_password()) == 16
    for _ in range(20):
        password = auth.generate_password(24)
        assert len(password) == 24
        assert any(char.isupper() for char in password)
        assert any(char.islower() for char in password)
        assert any(char.isdigit() for char in password)
        assert any(char in auth.GENERATED_PASSWORD_SYMBOLS for char in password)
    assert auth.generate_password(12) != auth.generate_password(12)


# ---------------------------------------------------------------------------
# 用户名规范化
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" Alice.01 ", "alice.01"),
        ("BOB", "bob"),
        ("a-b_c.d", "a-b_c.d"),
        ("user_123", "user_123"),
        ("A" * 32, "a" * 32),
    ],
)
def test_normalize_username(raw, expected):
    assert auth.normalize_username(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "ab", "a" * 33, "-alice", ".alice", "alice!", "a b", "用户账号", None, 123],
)
def test_normalize_username_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        auth.normalize_username(raw)


# ---------------------------------------------------------------------------
# 租户 CRUD
# ---------------------------------------------------------------------------
def test_schema_creates_expected_tables(store):
    with store.connect() as connection:
        tables = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"ui_tenants", "ui_auth_attempts"} <= tables


def test_create_tenant_defaults_and_lookup(store):
    tenant = store.create_tenant("Alice", FAST_PASSWORD)
    assert isinstance(tenant, Tenant)
    assert tenant.username == "alice"
    assert tenant.display_name == "alice"
    assert tenant.disabled is False
    assert len(tenant.tenant_id) == 32
    assert all(char in "0123456789abcdef" for char in tenant.tenant_id)
    assert tenant.created_at <= tenant.updated_at
    assert store.get_tenant(tenant.tenant_id) == tenant
    assert store.get_by_username("ALICE") == tenant
    assert store.get_tenant("missing") is None
    assert store.get_by_username("missing") is None
    # 租户对象只携带展示信息，不应带出密码或哈希。
    assert not hasattr(tenant, "password")
    assert not hasattr(tenant, "password_hash")


def test_tenant_dataclass_is_frozen(store):
    tenant = _create(store)
    with pytest.raises(dataclasses.FrozenInstanceError):
        tenant.username = "bob"


def test_create_tenant_custom_display_name(store):
    tenant = store.create_tenant("alice", FAST_PASSWORD, display_name="  操作员 Alice ")
    assert tenant.display_name == "操作员 Alice"


def test_create_tenant_blank_display_name_falls_back_to_username(store):
    tenant = store.create_tenant("alice", FAST_PASSWORD, display_name="   ")
    assert tenant.display_name == "alice"


def test_create_tenant_rejects_oversized_display_name(store):
    with pytest.raises(ValueError):
        store.create_tenant("alice", FAST_PASSWORD, display_name="x" * 81)


@pytest.mark.parametrize("username", ["ab", "-bad", "bad name", "", None])
def test_create_tenant_rejects_invalid_username(store, username):
    with pytest.raises(ValueError):
        store.create_tenant(username, FAST_PASSWORD)
    assert store.list_tenants() == []


def test_create_tenant_rejects_short_password(store):
    with pytest.raises(ValueError):
        store.create_tenant("alice", "short")
    assert store.list_tenants() == []


def test_create_duplicate_username_raises_tenant_exists(store):
    _create(store)
    with pytest.raises(TenantExistsError):
        store.create_tenant("ALICE", "Other0!pass")
    assert len(store.list_tenants()) == 1


def test_list_tenants_is_ordered_by_creation(store):
    created = [store.create_tenant(f"user{index}", FAST_PASSWORD) for index in range(3)]
    listed = store.list_tenants()
    assert [tenant.username for tenant in listed] == [tenant.username for tenant in created]


def test_set_disabled_toggles_flag(store):
    tenant = _create(store)
    disabled = store.set_disabled(tenant.tenant_id, True)
    assert disabled.disabled is True
    assert store.get_by_username("alice").disabled is True
    enabled = store.set_disabled(tenant.tenant_id, False)
    assert enabled.disabled is False
    assert enabled.updated_at >= tenant.updated_at
    with pytest.raises(AuthError):
        store.set_disabled("missing", True)


def test_set_password_updates_login(store):
    tenant = _create(store)
    updated = store.set_password(tenant.tenant_id, "N3w!passw0rd")
    assert updated.updated_at >= tenant.updated_at
    with pytest.raises(InvalidCredentialsError):
        store.verify_login("alice", FAST_PASSWORD)
    assert store.verify_login("alice", "N3w!passw0rd").tenant_id == tenant.tenant_id
    with pytest.raises(AuthError):
        store.set_password("missing", "N3w!passw0rd")


def test_delete_tenant_removes_account_and_attempts(store):
    tenant = _create(store)
    with pytest.raises(InvalidCredentialsError):
        store.verify_login("alice", "Wrong!pass0")
    assert any(event["username"] == "alice" for event in store.recent_auth_events())

    assert store.delete_tenant(tenant.tenant_id) is True
    assert store.get_tenant(tenant.tenant_id) is None
    assert [event for event in store.recent_auth_events() if event["username"] == "alice"] == []
    assert store.delete_tenant(tenant.tenant_id) is False


@pytest.mark.parametrize("target", ["", "   ", None])
def test_get_tenant_rejects_invalid_id(store, target):
    with pytest.raises(ValueError):
        store.get_tenant(target)


def test_get_by_username_rejects_invalid_name(store):
    with pytest.raises(ValueError):
        store.get_by_username("bad name!!")


def test_tenants_persist_across_store_instances(state_file):
    first = TenantStore(Settings().ui_state_file)
    tenant = first.create_tenant("alice", FAST_PASSWORD)
    second = TenantStore(Settings().ui_state_file)
    assert second.get_by_username("alice") == tenant


def test_password_is_not_stored_in_plaintext(store, state_file):
    store.create_tenant("alice", FAST_PASSWORD)
    assert FAST_PASSWORD.encode("utf-8") not in state_file.read_bytes()


# ---------------------------------------------------------------------------
# 登录校验与锁定
# ---------------------------------------------------------------------------
def test_exception_hierarchy_and_message():
    assert issubclass(InvalidCredentialsError, AuthError)
    assert issubclass(AccountLockedError, AuthError)
    assert issubclass(TenantExistsError, AuthError)
    locked = AccountLockedError(12.4)
    assert locked.retry_after_seconds == pytest.approx(12.4)
    assert "秒" in str(locked)
    assert AccountLockedError(-5).retry_after_seconds == 0.0


def test_verify_login_success_returns_tenant_and_records_event(store):
    tenant = _create(store)
    assert store.verify_login("Alice", FAST_PASSWORD) == tenant
    events = store.recent_auth_events()
    assert len(events) == 1
    assert events[0] == {
        "username": "alice",
        "attempted_at": events[0]["attempted_at"],
        "success": True,
    }


def test_verify_login_wrong_password_is_generic_invalid_credentials(store):
    _create(store)
    with pytest.raises(InvalidCredentialsError) as excinfo:
        store.verify_login("alice", "Wrong!pass0")
    assert str(excinfo.value) == "用户名或密码错误"
    assert "alice" not in str(excinfo.value)
    events = store.recent_auth_events()
    assert len(events) == 1
    assert events[0]["username"] == "alice"
    assert events[0]["success"] is False


def test_verify_login_unknown_user_is_invalid_credentials(store):
    with pytest.raises(InvalidCredentialsError):
        store.verify_login("ghost", FAST_PASSWORD)
    events = store.recent_auth_events()
    assert events[0]["username"] == "ghost" and events[0]["success"] is False


def test_verify_login_invalid_username_format_is_invalid_credentials(store):
    with pytest.raises(InvalidCredentialsError):
        store.verify_login("bad name!!", FAST_PASSWORD)


def test_verify_login_disabled_account_is_invalid_credentials(store):
    tenant = _create(store)
    store.set_disabled(tenant.tenant_id, True)
    with pytest.raises(InvalidCredentialsError):
        store.verify_login("alice", FAST_PASSWORD)


def test_verify_login_non_text_password_is_rejected(store):
    _create(store)
    with pytest.raises(InvalidCredentialsError):
        store.verify_login("alice", None)


@pytest.mark.parametrize(
    "kwargs",
    [{"max_attempts": 0}, {"max_attempts": -1}, {"lockout_seconds": 0}, {"lockout_seconds": -1}],
)
def test_verify_login_rejects_invalid_parameters(store, kwargs):
    _create(store)
    with pytest.raises(ValueError):
        store.verify_login("alice", FAST_PASSWORD, **kwargs)


def test_lockout_after_manual_failures_blocks_even_correct_password(store):
    _create(store)
    _insert_failures(store, "alice", 3, time.time())
    with pytest.raises(AccountLockedError) as excinfo:
        store.verify_login("alice", FAST_PASSWORD, max_attempts=3, lockout_seconds=300)
    assert 0 < excinfo.value.retry_after_seconds <= 300
    # 锁定期间的尝试不写入失败记录，避免攻击者无限延长锁定。
    assert len(store.recent_auth_events(limit=10)) == 3


def test_real_failures_reach_lockout_without_sleep(store):
    """真实走 3 次错误密码后第 4 次进入锁定（全部使用便宜 KDF，无 sleep）。"""
    _create(store)
    for _ in range(3):
        with pytest.raises(InvalidCredentialsError):
            store.verify_login("alice", "Wrong!pass0", max_attempts=3, lockout_seconds=300)
    with pytest.raises(AccountLockedError):
        store.verify_login("alice", FAST_PASSWORD, max_attempts=3, lockout_seconds=300)


def test_retry_after_reflects_remaining_window(store):
    _create(store)
    _insert_failures(store, "alice", 5, time.time() - 10)
    with pytest.raises(AccountLockedError) as excinfo:
        store.verify_login("alice", FAST_PASSWORD, max_attempts=5, lockout_seconds=300)
    assert 285 <= excinfo.value.retry_after_seconds <= 295


def test_failures_outside_window_do_not_lock(store):
    _create(store)
    _insert_failures(store, "alice", 5, time.time() - 400)
    with pytest.raises(InvalidCredentialsError):
        store.verify_login("alice", "Wrong!pass0", max_attempts=5, lockout_seconds=300)


def test_lockout_expires_and_correct_password_succeeds(store):
    _create(store)
    _insert_failures(store, "alice", 5, time.time() - 400)
    tenant = store.verify_login("alice", FAST_PASSWORD, max_attempts=5, lockout_seconds=300)
    assert tenant.username == "alice"


def test_success_clears_previous_failure_counter(store):
    _create(store)
    for _ in range(2):
        with pytest.raises(InvalidCredentialsError):
            store.verify_login("alice", "Wrong!pass0", max_attempts=3, lockout_seconds=300)
    assert store.verify_login("alice", FAST_PASSWORD, max_attempts=3, lockout_seconds=300)
    assert [event for event in store.recent_auth_events() if event["success"] is False] == []
    # 清零后再累计 2 次失败仍未触发锁定。
    for _ in range(2):
        with pytest.raises(InvalidCredentialsError):
            store.verify_login("alice", "Wrong!pass0", max_attempts=3, lockout_seconds=300)


def test_recent_auth_events_limit_shape_and_order(store):
    _create(store)
    for _ in range(3):
        with pytest.raises(InvalidCredentialsError):
            store.verify_login("alice", "Wrong!pass0")
    events = store.recent_auth_events(limit=2)
    assert len(events) == 2
    for event in events:
        assert set(event) == {"username", "attempted_at", "success"}
        assert isinstance(event["attempted_at"], float)
        assert isinstance(event["success"], bool)
    assert events[0]["attempted_at"] >= events[1]["attempted_at"]
    with pytest.raises(ValueError):
        store.recent_auth_events(limit=0)


# ---------------------------------------------------------------------------
# CLI：parser
# ---------------------------------------------------------------------------
def test_parser_parses_add_generate_options():
    args = tenants.parser().parse_args(["add", "alice", "--display-name", "Alice", "--generate"])
    assert args.command == "add"
    assert args.username == "alice"
    assert args.display_name == "Alice"
    assert args.generate is True
    assert args.password is None


def test_parser_parse_reset_password_defaults():
    args = tenants.parser().parse_args(["reset-password", "alice"])
    assert args.command == "reset-password"
    assert args.target == "alice"
    assert args.password is None
    assert args.generate is False


@pytest.mark.parametrize("command", ["disable", "enable", "delete"])
def test_parser_parses_target_commands(command):
    args = tenants.parser().parse_args([command, "alice"])
    assert args.command == command
    assert args.target == "alice"


def test_parser_password_and_generate_are_mutually_exclusive():
    with pytest.raises(SystemExit) as excinfo:
        tenants.parser().parse_args(["add", "alice", "-p", FAST_PASSWORD, "--generate"])
    assert excinfo.value.code == 2


def test_parser_requires_subcommand():
    with pytest.raises(SystemExit) as excinfo:
        tenants.parser().parse_args([])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        tenants.main([])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# CLI：main
# ---------------------------------------------------------------------------
def test_cli_add_generate_prints_password_once_and_creates_tenant(state_file, capsys):
    assert tenants.main(["add", "alice", "--generate"]) == 0
    captured = capsys.readouterr()
    match = re.search(r"初始密码[^:：]*[:：]\s*(\S+)", captured.out)
    assert match is not None
    password = match.group(1)
    assert len(password) == 16
    store = TenantStore(Settings().ui_state_file)
    assert store.verify_login("alice", password).username == "alice"


def test_cli_add_explicit_password_is_never_echoed(state_file, capsys):
    assert tenants.main(["add", "bob", "-p", FAST_PASSWORD]) == 0
    captured = capsys.readouterr()
    assert FAST_PASSWORD not in captured.out
    assert FAST_PASSWORD not in captured.err
    store = TenantStore(Settings().ui_state_file)
    assert store.verify_login("bob", FAST_PASSWORD).username == "bob"


def test_cli_add_interactive_getpass_flow(state_file, capsys, monkeypatch):
    answers = iter(["Interact!ve1", "Interact!ve1"])
    monkeypatch.setattr(tenants, "getpass", lambda prompt="": next(answers))
    assert tenants.main(["add", "carol"]) == 0
    captured = capsys.readouterr()
    assert "Interact!ve1" not in captured.out
    assert "Interact!ve1" not in captured.err
    store = TenantStore(Settings().ui_state_file)
    assert store.verify_login("carol", "Interact!ve1").username == "carol"


def test_cli_add_interactive_mismatch_cancels(state_file, capsys, monkeypatch):
    answers = iter(["Interact!ve1", "Different!ve2"])
    monkeypatch.setattr(tenants, "getpass", lambda prompt="": next(answers))
    assert tenants.main(["add", "dave"]) == 1
    captured = capsys.readouterr()
    assert "不一致" in captured.err
    assert TenantStore(Settings().ui_state_file).get_by_username("dave") is None


def test_cli_add_duplicate_returns_one_without_password(state_file, capsys):
    TenantStore(Settings().ui_state_file).create_tenant("alice", FAST_PASSWORD)
    assert tenants.main(["add", "alice", "--generate"]) == 1
    captured = capsys.readouterr()
    assert "已存在" in captured.err
    assert "初始密码" not in captured.out


def test_cli_add_rejects_short_password(state_file, capsys):
    assert tenants.main(["add", "alice", "-p", "short"]) == 1
    captured = capsys.readouterr()
    assert "密码长度" in captured.err
    assert TenantStore(Settings().ui_state_file).get_by_username("alice") is None


def test_cli_list_renders_table_with_short_ids(state_file, capsys):
    store = TenantStore(Settings().ui_state_file)
    first = store.create_tenant("alice", FAST_PASSWORD, display_name="Alice")
    store.create_tenant("bob", FAST_PASSWORD)
    assert tenants.main(["list"]) == 0
    out = capsys.readouterr().out
    for expected in (first.tenant_id[:8], "alice", "Alice", "bob", "用户名", "创建时间"):
        assert expected in out
    assert FAST_PASSWORD not in out


def test_cli_list_empty_state_prints_hint(state_file, capsys):
    assert tenants.main(["list"]) == 0
    assert "暂无租户" in capsys.readouterr().out


def test_cli_reset_password_explicit_is_not_echoed(state_file, capsys):
    store = TenantStore(Settings().ui_state_file)
    tenant = store.create_tenant("alice", FAST_PASSWORD)
    assert tenants.main(["reset-password", "alice", "-p", "N3w!passw0rd"]) == 0
    captured = capsys.readouterr()
    assert "N3w!passw0rd" not in captured.out
    assert "N3w!passw0rd" not in captured.err
    with pytest.raises(InvalidCredentialsError):
        store.verify_login("alice", FAST_PASSWORD)
    assert store.verify_login("alice", "N3w!passw0rd").tenant_id == tenant.tenant_id


def test_cli_reset_password_generate_prints_new_password(state_file, capsys):
    store = TenantStore(Settings().ui_state_file)
    store.create_tenant("alice", FAST_PASSWORD)
    assert tenants.main(["reset-password", "alice", "--generate"]) == 0
    out = capsys.readouterr().out
    match = re.search(r"新密码[^:：]*[:：]\s*(\S+)", out)
    assert match is not None
    assert store.verify_login("alice", match.group(1)).username == "alice"


def test_cli_reset_password_unknown_target_returns_one(state_file, capsys):
    assert tenants.main(["reset-password", "ghost", "-p", "N3w!passw0rd"]) == 1
    assert "未找到租户" in capsys.readouterr().err


def test_cli_disable_and_enable_by_username_and_tenant_id(state_file, capsys):
    store = TenantStore(Settings().ui_state_file)
    tenant = store.create_tenant("alice", FAST_PASSWORD)

    assert tenants.main(["disable", "alice"]) == 0
    assert "已禁用" in capsys.readouterr().out
    assert store.get_tenant(tenant.tenant_id).disabled is True

    assert tenants.main(["enable", tenant.tenant_id]) == 0
    assert "已启用" in capsys.readouterr().out
    assert store.get_tenant(tenant.tenant_id).disabled is False


def test_cli_disable_unknown_target_returns_one(state_file, capsys):
    assert tenants.main(["disable", "ghost"]) == 1
    assert "未找到租户" in capsys.readouterr().err


def test_cli_delete_by_username_removes_account(state_file, capsys):
    store = TenantStore(Settings().ui_state_file)
    tenant = store.create_tenant("alice", FAST_PASSWORD)
    assert tenants.main(["delete", "alice"]) == 0
    assert "已删除" in capsys.readouterr().out
    assert store.get_tenant(tenant.tenant_id) is None


def test_cli_delete_by_tenant_id_and_missing_target(state_file, capsys):
    store = TenantStore(Settings().ui_state_file)
    tenant = store.create_tenant("alice", FAST_PASSWORD)
    assert tenants.main(["delete", tenant.tenant_id]) == 0
    assert tenants.main(["delete", tenant.tenant_id]) == 1
    assert "未找到租户" in capsys.readouterr().err


def test_cli_uses_ui_state_file_environment(state_file, capsys):
    assert tenants.main(["add", "alice", "-p", FAST_PASSWORD]) == 0
    captured = capsys.readouterr()
    assert "已创建租户" in captured.out
    assert state_file.is_file()


def test_format_tenant_table_aligns_cjk_display_name():
    tenant = Tenant(
        tenant_id="0123456789abcdef0123456789abcdef",
        username="alice",
        display_name="操作员 Alice",
        disabled=False,
        created_at=0.0,
        updated_at=0.0,
    )
    lines = tenants.format_tenant_table([tenant]).splitlines()
    assert len(lines) == 2
    assert "alice" in lines[1]
    assert "操作员 Alice" in lines[1]
    assert tenants.format_tenant_table([]).splitlines() == [
        "ID  用户名  显示名  禁用  创建时间"
    ]
