"""Fail-closed authorization primitives; not yet wired to the legacy API.

Callers must obtain principals and grants from authoritative, verified storage,
never from client-supplied role or ownership fields.
This is only an ownership/role decision, not approval for processing health
data: separate current purpose-specific consent and minor protections are not
implemented. Long-lived snapshots cannot enforce immediate revocation.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class Role(StrEnum):
    PATIENT = "patient"
    GUARDIAN = "guardian"
    ORG_ADMIN = "org_admin"
    KNOWLEDGE_ADMIN = "knowledge_admin"
    INTEGRATION_ADMIN = "integration_admin"
    AUDITOR = "auditor"
    SUPPORT = "support"
    PLATFORM_OPERATOR = "platform_operator"


class Action(StrEnum):
    PATIENT_READ = "patient.read"
    PATIENT_WRITE = "patient.write"
    KNOWLEDGE_READ = "knowledge.read"
    KNOWLEDGE_MANAGE = "knowledge.manage"
    MEMBER_MANAGE = "member.manage"
    INTEGRATION_MANAGE = "integration.manage"
    AUDIT_READ = "audit.read"
    SUPPORT_CASE_MANAGE = "support_case.manage"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    tenant_id: str
    roles: frozenset[Role]
    active: bool
    tenant_active: bool
    # Set only from an authoritative, verified account-to-patient binding.
    patient_id: str | None = None
    # Derived from the current verified authentication session, never a client flag.
    mfa_verified: bool = False


@dataclass(frozen=True, slots=True)
class Resource:
    tenant_id: str
    patient_id: str | None = None


@dataclass(frozen=True, slots=True)
class GuardianGrant:
    tenant_id: str
    guardian_id: str
    patient_id: str
    actions: frozenset[Action]
    verified: bool
    consented: bool
    expires_at: datetime
    valid_from: datetime | None = None
    revoked: bool = True


_ROLE_ACTIONS = {
    Role.PATIENT: frozenset({Action.KNOWLEDGE_READ}),
    Role.GUARDIAN: frozenset({Action.KNOWLEDGE_READ}),
    Role.ORG_ADMIN: frozenset({Action.MEMBER_MANAGE}),
    Role.KNOWLEDGE_ADMIN: frozenset({Action.KNOWLEDGE_READ, Action.KNOWLEDGE_MANAGE}),
    Role.INTEGRATION_ADMIN: frozenset({Action.INTEGRATION_MANAGE}),
    Role.AUDITOR: frozenset({Action.AUDIT_READ}),
    Role.SUPPORT: frozenset({Action.SUPPORT_CASE_MANAGE}),
    Role.PLATFORM_OPERATOR: frozenset(),
}
_PATIENT_ACTIONS = frozenset({Action.PATIENT_READ, Action.PATIENT_WRITE})
_MFA_REQUIRED_ROLES = frozenset({Role.ORG_ADMIN, Role.INTEGRATION_ADMIN, Role.AUDITOR,
                                 Role.PLATFORM_OPERATOR})


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def is_allowed(
    principal: Principal,
    action: Action,
    resource: Resource,
    *,
    guardian_grant: GuardianGrant | None = None,
    patient_is_minor: bool = False,
    now: datetime | None = None,
) -> bool:
    """Evaluate verified role and ownership, not purpose-specific consent.

    ``patient_is_minor`` must come from the current trusted patient record,
    never from a request. Missing or malformed classification denies guardians.
    """
    if (principal.active is not True or principal.tenant_active is not True
            or not _valid_id(principal.user_id) or not _valid_id(principal.tenant_id)
            or not _valid_id(resource.tenant_id) or not isinstance(action, Action)
            or not isinstance(principal.roles, frozenset)
            or not all(isinstance(role, Role) for role in principal.roles)):
        return False
    if principal.roles & _MFA_REQUIRED_ROLES and principal.mfa_verified is not True:
        return False
    if not principal.tenant_id or principal.tenant_id != resource.tenant_id:
        return False
    if action in _PATIENT_ACTIONS:
        if not _valid_id(resource.patient_id):
            return False
        if (Role.PATIENT in principal.roles and _valid_id(principal.patient_id)
                and resource.patient_id == principal.patient_id):
            return True
        if Role.GUARDIAN not in principal.roles or guardian_grant is None or patient_is_minor is not True:
            return False
        checked_at = now if now is not None else datetime.now(UTC)
        if (not isinstance(checked_at, datetime) or not isinstance(guardian_grant.expires_at, datetime)
                or not isinstance(guardian_grant.valid_from, datetime)
                or checked_at.utcoffset() is None or guardian_grant.expires_at.utcoffset() is None
                or guardian_grant.valid_from.utcoffset() is None
                or not isinstance(guardian_grant.actions, frozenset)
                or not all(isinstance(item, Action) for item in guardian_grant.actions)):
            return False
        return (
            guardian_grant.tenant_id == resource.tenant_id
            and guardian_grant.guardian_id == principal.user_id
            and guardian_grant.patient_id == resource.patient_id
            and action in guardian_grant.actions
            and guardian_grant.verified is True
            and guardian_grant.consented is True
            and guardian_grant.revoked is False
            and guardian_grant.valid_from <= checked_at < guardian_grant.expires_at
        )
    return any(action in _ROLE_ACTIONS.get(role, frozenset()) for role in principal.roles)
