"""Auto-filled demo login: provisioning, persistence and safety degradation."""

from medical_rag.ui.auth import TenantStore
from medical_rag.ui.demo import (
    DEMO_CREDENTIALS_KEY,
    REASON_DISABLED,
    REASON_INVALID_CONFIG,
    REASON_MISSING_CREDENTIALS,
    REASON_PASSWORD_CHANGED,
    ensure_demo_login,
)
from medical_rag.ui.state import UiStateStore


def _stores(tmp_path):
    return (TenantStore(tmp_path / "ui-state.sqlite3"),
            UiStateStore(tmp_path / "ui-state.sqlite3"))


def test_disabled_feature_returns_none(tmp_path):
    tenants, state = _stores(tmp_path)
    assert ensure_demo_login(tenants, state, enabled=False, username="demo") is None
    assert tenants.list_tenants() == []


def test_first_visit_creates_demo_account_and_prefills(tmp_path):
    tenants, state = _stores(tmp_path)
    account = ensure_demo_login(tenants, state, enabled=True, username="demo")
    assert account is not None
    assert account.usable is True
    assert account.created is True
    assert account.username == "demo"
    assert isinstance(account.password, str) and len(account.password) >= 8

    tenant = tenants.get_by_username("demo")
    assert tenant is not None
    assert tenants.password_matches(tenant.tenant_id, account.password)
    assert state.get(DEMO_CREDENTIALS_KEY) == {
        "username": "demo", "password": account.password}


def test_second_visit_reuses_persisted_credentials(tmp_path):
    tenants, state = _stores(tmp_path)
    first = ensure_demo_login(tenants, state, enabled=True, username="demo")
    second = ensure_demo_login(TenantStore(state.path), UiStateStore(state.path),
                               enabled=True, username="demo")
    assert first is not None and second is not None
    assert second.created is False
    assert second.usable is True
    assert second.password == first.password


def test_fixed_password_is_used_without_being_stored(tmp_path):
    tenants, state = _stores(tmp_path)
    account = ensure_demo_login(tenants, state, enabled=True, username="demo",
                                password="fixed-pass-123")
    assert account is not None and account.usable
    assert account.password == "fixed-pass-123"
    tenant = tenants.get_by_username("demo")
    assert tenant is not None
    assert tenants.password_matches(tenant.tenant_id, "fixed-pass-123")
    assert state.get(DEMO_CREDENTIALS_KEY) is None


def test_password_changed_outside_console_is_not_overwritten(tmp_path):
    tenants, state = _stores(tmp_path)
    first = ensure_demo_login(tenants, state, enabled=True, username="demo")
    tenant = tenants.get_by_username("demo")
    assert tenant is not None and first is not None
    tenants.set_password(tenant.tenant_id, "changed-pass-456")

    second = ensure_demo_login(tenants, state, enabled=True, username="demo")
    assert second is not None
    assert second.usable is False
    assert second.reason == REASON_PASSWORD_CHANGED
    # The stale local copy must not be resurrected, and the new password stays valid.
    assert state.get(DEMO_CREDENTIALS_KEY) is None
    assert tenants.password_matches(tenant.tenant_id, "changed-pass-456")


def test_fixed_password_mismatch_degrades_to_password_changed(tmp_path):
    tenants, state = _stores(tmp_path)
    tenants.create_tenant("demo", "manual-pass-123")
    account = ensure_demo_login(tenants, state, enabled=True, username="demo",
                                password="different-pass-123")
    assert account is not None
    assert account.usable is False
    assert account.reason == REASON_PASSWORD_CHANGED


def test_disabled_tenant_is_not_prefilled(tmp_path):
    tenants, state = _stores(tmp_path)
    ensure_demo_login(tenants, state, enabled=True, username="demo")
    tenant = tenants.get_by_username("demo")
    assert tenant is not None
    tenants.set_disabled(tenant.tenant_id, True)

    account = ensure_demo_login(tenants, state, enabled=True, username="demo")
    assert account is not None
    assert account.disabled is True
    assert account.usable is False
    assert account.reason == REASON_DISABLED


def test_existing_account_without_stored_credentials_is_not_prefilled(tmp_path):
    tenants, state = _stores(tmp_path)
    tenants.create_tenant("demo", "manual-pass-123")
    account = ensure_demo_login(tenants, state, enabled=True, username="demo")
    assert account is not None
    assert account.usable is False
    assert account.reason == REASON_MISSING_CREDENTIALS


def test_invalid_config_does_not_crash(tmp_path):
    tenants, state = _stores(tmp_path)
    bad_user = ensure_demo_login(tenants, state, enabled=True, username="--bad--")
    assert bad_user is not None and bad_user.usable is False
    assert bad_user.reason == REASON_INVALID_CONFIG

    bad_password = ensure_demo_login(tenants, state, enabled=True, username="demo",
                                     password="short")
    assert bad_password is not None and bad_password.usable is False
    assert bad_password.reason == REASON_INVALID_CONFIG


def test_deleted_demo_tenant_is_recreated_with_new_credentials(tmp_path):
    tenants, state = _stores(tmp_path)
    first = ensure_demo_login(tenants, state, enabled=True, username="demo")
    tenant = tenants.get_by_username("demo")
    assert tenant is not None and first is not None
    tenants.delete_tenant(tenant.tenant_id)

    second = ensure_demo_login(tenants, state, enabled=True, username="demo")
    assert second is not None
    assert second.created is True
    assert second.usable is True
    assert state.get(DEMO_CREDENTIALS_KEY) == {
        "username": "demo", "password": second.password}


def test_corrupt_stored_record_degrades_without_crashing(tmp_path):
    tenants, state = _stores(tmp_path)
    tenants.create_tenant("demo", "manual-pass-123")
    state.set(DEMO_CREDENTIALS_KEY, "not-a-dict")
    account = ensure_demo_login(tenants, state, enabled=True, username="demo")
    assert account is not None
    assert account.reason == REASON_MISSING_CREDENTIALS


def test_configured_password_accepts_existing_matching_account(tmp_path):
    tenants, state = _stores(tmp_path)
    tenants.create_tenant("demo", "configured-pass-123")
    account = ensure_demo_login(tenants, state, enabled=True, username="demo",
                                password="configured-pass-123")
    assert account is not None and account.usable
    assert account.password == "configured-pass-123"
    assert account.created is False


def test_demo_username_is_normalized(tmp_path):
    tenants, state = _stores(tmp_path)
    account = ensure_demo_login(tenants, state, enabled=True, username="  Demo  ")
    assert account is not None
    assert account.username == "demo"
    assert tenants.get_by_username("DEMO") is not None
