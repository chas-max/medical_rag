"""Auto-filled demo login for the local Streamlit console.

This is a **demo-only convenience**, not an authentication feature: when the
operator allows it, the login page provisions a local ``demo`` tenant and fills
the form with credentials that are known to work, so a reviewer can enter the
console with one click. The credential is generated randomly on first use and
kept in the local UI state database (the same file that stores tenant accounts),
never in the repository.

Safety rules:

- the module only ever pre-fills credentials it has just proven with
  :meth:`TenantStore.password_matches`; it never overwrites a password the
  operator changed through the CLI or the password-reset flow;
- an existing, disabled or password-changed demo account degrades to "no
  pre-fill" with a reason instead of silently recreating/overwriting anything;
- the login itself still goes through the normal ``verify_login`` path, so
  lockout and audit rules are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .auth import (
    TenantExistsError,
    TenantStore,
    generate_password,
    normalize_username,
    validate_password,
)
from .state import UiStateStore

DEMO_CREDENTIALS_KEY = "ui.demo.credentials"
DEFAULT_DEMO_USERNAME = "demo"
DEFAULT_DEMO_DISPLAY_NAME = "演示租户"

# Reasons surfaced by the login page when a demo account exists but must not be
# auto-filled. They are stable identifiers, not user-facing prose.
REASON_PASSWORD_CHANGED = "password_changed"
REASON_MISSING_CREDENTIALS = "missing_credentials"
REASON_DISABLED = "disabled"
REASON_INVALID_CONFIG = "invalid_config"


@dataclass(frozen=True)
class DemoAccount:
    """Outcome of :func:`ensure_demo_login`."""

    username: str
    password: str | None
    created: bool = False
    disabled: bool = False
    reason: str | None = None

    @property
    def usable(self) -> bool:
        """Whether the login form can be pre-filled with working credentials."""
        return self.password is not None and not self.disabled and self.reason is None


def _read_stored_credentials(state_store: UiStateStore) -> dict[str, str] | None:
    try:
        value: Any = state_store.get(DEMO_CREDENTIALS_KEY, None)
    except Exception:  # noqa: BLE001 - a broken state DB simply disables pre-fill
        return None
    if not isinstance(value, dict):
        return None
    username = value.get("username")
    password = value.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    if not username.strip() or not password:
        return None
    return {"username": username.strip(), "password": password}


def _remember_credentials(state_store: UiStateStore, username: str, password: str) -> None:
    try:
        state_store.set(DEMO_CREDENTIALS_KEY, {"username": username, "password": password})
    except Exception:  # noqa: BLE001 - pre-fill is optional, never fail the login page
        pass


def _forget_credentials(state_store: UiStateStore) -> None:
    try:
        state_store.delete(DEMO_CREDENTIALS_KEY)
    except Exception:  # noqa: BLE001 - best effort cleanup
        pass


def ensure_demo_login(tenants: TenantStore, state_store: UiStateStore, *,
                      enabled: bool, username: str = DEFAULT_DEMO_USERNAME,
                      password: str | None = None) -> DemoAccount | None:
    """Provision/verify the demo account and return what the form may pre-fill.

    Args:
        tenants: Tenant account store.
        state_store: Local state store that keeps the generated demo credential.
        enabled: ``UI_DEMO_LOGIN_ENABLED`` — disables the whole feature when false.
        username: ``UI_DEMO_USERNAME``; normalized before use.
        password: Optional fixed ``UI_DEMO_PASSWORD``; when empty the first visit
            generates a random password and persists it in the state database.

    Returns:
        ``None`` when the feature is disabled, otherwise a :class:`DemoAccount`
        whose ``password`` is only set when the login form may safely pre-fill
        it (``DemoAccount.usable``).
    """
    if not enabled:
        return None
    try:
        name = normalize_username(username)
    except ValueError:
        return DemoAccount(username=str(username), password=None,
                           reason=REASON_INVALID_CONFIG)
    fixed_password: str | None = None
    if password is not None and password.strip():
        try:
            fixed_password = validate_password(password)
        except ValueError:
            return DemoAccount(username=name, password=None,
                               reason=REASON_INVALID_CONFIG)

    try:
        tenant = tenants.get_by_username(name)
    except Exception:  # noqa: BLE001 - the login page reports storage failures itself
        return DemoAccount(username=name, password=None, reason=REASON_MISSING_CREDENTIALS)

    if tenant is None:
        secret = fixed_password or generate_password()
        try:
            tenant = tenants.create_tenant(name, secret, display_name=DEFAULT_DEMO_DISPLAY_NAME)
        except TenantExistsError:
            # Another tab raced us; fall through to the existing-account branch.
            tenant = tenants.get_by_username(name)
            if tenant is None:  # pragma: no cover - defensive
                return DemoAccount(username=name, password=None,
                                   reason=REASON_MISSING_CREDENTIALS)
        except Exception:  # noqa: BLE001 - storage failure: no pre-fill, form still renders
            return DemoAccount(username=name, password=None, reason=REASON_MISSING_CREDENTIALS)
        else:
            if fixed_password is None:
                _remember_credentials(state_store, name, secret)
            return DemoAccount(username=name, password=secret, created=True)

    if tenant.disabled:
        return DemoAccount(username=name, password=None, disabled=True,
                           reason=REASON_DISABLED)

    if fixed_password is not None:
        if tenants.password_matches(tenant.tenant_id, fixed_password):
            return DemoAccount(username=name, password=fixed_password)
        return DemoAccount(username=name, password=None, reason=REASON_PASSWORD_CHANGED)

    stored = _read_stored_credentials(state_store)
    if stored is not None and stored["username"] == name:
        if tenants.password_matches(tenant.tenant_id, stored["password"]):
            return DemoAccount(username=name, password=stored["password"])
        # The operator changed the password behind our back; a stale local copy
        # must not be pre-filled (and must not be resurrected).
        _forget_credentials(state_store)
        return DemoAccount(username=name, password=None, reason=REASON_PASSWORD_CHANGED)
    return DemoAccount(username=name, password=None, reason=REASON_MISSING_CREDENTIALS)
