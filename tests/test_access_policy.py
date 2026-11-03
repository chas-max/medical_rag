"""Authorization policy must never infer access from administrative power."""

from datetime import UTC, datetime, timedelta

import pytest

from medical_rag.access_policy import (
    Action,
    GuardianGrant,
    Principal,
    Resource,
    Role,
    is_allowed,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_patient_without_verified_patient_binding_is_denied_even_when_ids_match():
    actor = Principal("patient-1", "hospital-a", frozenset({Role.PATIENT}), True, True)
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "patient-1"), now=NOW)


def test_patient_can_only_access_patient_id_from_verified_binding():
    actor = Principal("account-1", "hospital-a", frozenset({Role.PATIENT}), True, True,
                      patient_id="patient-1")
    assert is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "patient-1"), now=NOW)
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "account-1"), now=NOW)


@pytest.mark.parametrize(("role", "action"), [
    (Role.ORG_ADMIN, Action.MEMBER_MANAGE),
    (Role.INTEGRATION_ADMIN, Action.INTEGRATION_MANAGE),
    (Role.AUDITOR, Action.AUDIT_READ),
])
def test_sensitive_staff_role_requires_verified_mfa(role, action):
    actor = Principal("user-1", "hospital-a", frozenset({role}), True, True)
    assert not is_allowed(actor, action, Resource("hospital-a"), now=NOW)


def test_privileged_account_cannot_avoid_mfa_by_using_its_patient_role():
    actor = Principal("account-1", "hospital-a", frozenset({Role.PATIENT, Role.ORG_ADMIN}),
                      True, True, patient_id="patient-1")
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "patient-1"), now=NOW)


@pytest.mark.parametrize("mfa_verified", ["true", 1, None])
def test_mfa_state_requires_a_real_boolean(mfa_verified):
    actor = Principal("admin", "hospital-a", frozenset({Role.ORG_ADMIN}), True, True,
                      mfa_verified=mfa_verified)
    assert not is_allowed(actor, Action.MEMBER_MANAGE, Resource("hospital-a"), now=NOW)


def principal(*roles, user_id="user-1", tenant_id="hospital-a", active=True, tenant_active=True):
    return Principal(user_id, tenant_id, frozenset(roles), active, tenant_active,
                     patient_id=user_id, mfa_verified=True)


@pytest.mark.parametrize("role", [Role.ORG_ADMIN, Role.KNOWLEDGE_ADMIN, Role.INTEGRATION_ADMIN,
                                   Role.AUDITOR, Role.SUPPORT, Role.PLATFORM_OPERATOR])
def test_staff_cannot_read_patient_content(role):
    assert not is_allowed(principal(role), Action.PATIENT_READ,
                          Resource("hospital-a", "patient-1"), now=NOW)


def test_patient_access_requires_matching_patient_and_tenant():
    actor = principal(Role.PATIENT)
    assert is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "user-1"), now=NOW)
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "user-2"), now=NOW)
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-b", "user-1"), now=NOW)
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a"), now=NOW)


@pytest.mark.parametrize(("active", "tenant_active"), [(False, True), (True, False), (False, False)])
def test_disabled_user_or_tenant_is_denied(active, tenant_active):
    actor = principal(Role.PATIENT, active=active, tenant_active=tenant_active)
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "user-1"), now=NOW)


def test_guardian_requires_matching_verified_unexpired_and_consented_grant():
    actor = principal(Role.GUARDIAN, user_id="guardian")
    resource = Resource("hospital-a", "child")
    grant = GuardianGrant("hospital-a", "guardian", "child", frozenset({Action.PATIENT_READ}),
                          True, True, NOW + timedelta(days=1), valid_from=NOW, revoked=False)
    assert is_allowed(actor, Action.PATIENT_READ, resource, guardian_grant=grant, patient_is_minor=True, now=NOW)
    assert not is_allowed(actor, Action.PATIENT_WRITE, resource, guardian_grant=grant, patient_is_minor=True, now=NOW)
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "other"), guardian_grant=grant,
                          patient_is_minor=True, now=NOW)
    assert not is_allowed(actor, Action.PATIENT_READ, resource, guardian_grant=grant,
                          patient_is_minor=True, now=NOW + timedelta(days=1))


def test_guardian_access_requires_trusted_minor_classification():
    actor = principal(Role.GUARDIAN, user_id="guardian")
    resource = Resource("hospital-a", "child")
    grant = GuardianGrant("hospital-a", "guardian", "child", frozenset({Action.PATIENT_READ}),
                          True, True, NOW + timedelta(days=1), valid_from=NOW, revoked=False)
    assert not is_allowed(actor, Action.PATIENT_READ, resource, guardian_grant=grant, now=NOW)
    assert not is_allowed(actor, Action.PATIENT_READ, resource, guardian_grant=grant,
                          patient_is_minor=False, now=NOW)
    assert not is_allowed(actor, Action.PATIENT_READ, resource, guardian_grant=grant,
                          patient_is_minor="true", now=NOW)
    assert is_allowed(actor, Action.PATIENT_READ, resource, guardian_grant=grant,
                      patient_is_minor=True, now=NOW)


@pytest.mark.parametrize(("verified", "consented"), [(False, True), (True, False), (False, False)])
def test_guardian_is_denied_when_verification_or_consent_missing(verified, consented):
    actor = principal(Role.GUARDIAN, user_id="guardian")
    grant = GuardianGrant("hospital-a", "guardian", "child", frozenset({Action.PATIENT_READ}),
                          verified, consented, NOW + timedelta(days=1), valid_from=NOW, revoked=False)
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "child"),
                          guardian_grant=grant, patient_is_minor=True, now=NOW)


@pytest.mark.parametrize(("role", "allowed_action", "denied_action"), [
    (Role.ORG_ADMIN, Action.MEMBER_MANAGE, Action.AUDIT_READ),
    (Role.KNOWLEDGE_ADMIN, Action.KNOWLEDGE_MANAGE, Action.MEMBER_MANAGE),
    (Role.INTEGRATION_ADMIN, Action.INTEGRATION_MANAGE, Action.KNOWLEDGE_MANAGE),
    (Role.AUDITOR, Action.AUDIT_READ, Action.MEMBER_MANAGE),
    (Role.SUPPORT, Action.SUPPORT_CASE_MANAGE, Action.INTEGRATION_MANAGE),
])
def test_staff_permissions_are_separate(role, allowed_action, denied_action):
    actor = principal(role)
    resource = Resource("hospital-a")
    assert is_allowed(actor, allowed_action, resource, now=NOW)
    assert not is_allowed(actor, denied_action, resource, now=NOW)
    assert not is_allowed(actor, allowed_action, Resource("hospital-b"), now=NOW)


def test_platform_operator_has_no_implicit_institution_permissions():
    actor = principal(Role.PLATFORM_OPERATOR)
    assert not is_allowed(actor, Action.MEMBER_MANAGE, Resource("hospital-a"), now=NOW)
    assert not is_allowed(actor, Action.AUDIT_READ, Resource("hospital-a"), now=NOW)


def test_guardian_grant_cannot_be_reused_by_another_tenant_or_guardian():
    actor = principal(Role.GUARDIAN, user_id="guardian")
    resource = Resource("hospital-a", "child")
    wrong_tenant = GuardianGrant("hospital-b", "guardian", "child", frozenset({Action.PATIENT_READ}),
                                 True, True, NOW + timedelta(days=1), valid_from=NOW, revoked=False)
    wrong_guardian = GuardianGrant("hospital-a", "other", "child", frozenset({Action.PATIENT_READ}),
                                   True, True, NOW + timedelta(days=1), valid_from=NOW, revoked=False)
    assert not is_allowed(actor, Action.PATIENT_READ, resource, guardian_grant=wrong_tenant, patient_is_minor=True, now=NOW)
    assert not is_allowed(actor, Action.PATIENT_READ, resource, guardian_grant=wrong_guardian, patient_is_minor=True, now=NOW)
    assert not is_allowed(actor, Action.PATIENT_READ, resource, now=NOW)


def test_guardian_grant_with_naive_times_is_denied():
    actor = principal(Role.GUARDIAN, user_id="guardian")
    grant = GuardianGrant("hospital-a", "guardian", "child", frozenset({Action.PATIENT_READ}),
                          True, True, datetime(2027, 1, 1), valid_from=NOW, revoked=False)
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "child"),
                          guardian_grant=grant, patient_is_minor=True, now=NOW)


@pytest.mark.parametrize(("valid_from", "revoked"), [
    (NOW + timedelta(seconds=1), False),
    (NOW - timedelta(days=1), True),
    (NOW - timedelta(days=1), "false"),
    (datetime(2025, 1, 1), False),
])
def test_guardian_grant_requires_started_nonrevoked_validity(valid_from, revoked):
    actor = principal(Role.GUARDIAN, user_id="guardian")
    grant = GuardianGrant("hospital-a", "guardian", "child", frozenset({Action.PATIENT_READ}),
                          True, True, NOW + timedelta(days=1), valid_from=valid_from, revoked=revoked)
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "child"),
                          guardian_grant=grant, patient_is_minor=True, now=NOW)


def test_guardian_grant_without_validity_or_revocation_state_is_denied():
    actor = principal(Role.GUARDIAN, user_id="guardian")
    grant = GuardianGrant("hospital-a", "guardian", "child", frozenset({Action.PATIENT_READ}),
                          True, True, NOW + timedelta(days=1))
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "child"),
                          guardian_grant=grant, patient_is_minor=True, now=NOW)


@pytest.mark.parametrize(("user_id", "tenant_id", "patient_id"), [
    (1, "hospital-a", "patient-1"),
    ("account-1", 1, "patient-1"),
    ("account-1", "hospital-a", 1),
    (" ", "hospital-a", "patient-1"),
])
def test_malformed_identifiers_cannot_authorize_patient_access(user_id, tenant_id, patient_id):
    actor = Principal(user_id, tenant_id, frozenset({Role.PATIENT}), True, True,
                      patient_id=patient_id)
    assert not is_allowed(actor, Action.PATIENT_READ, Resource(tenant_id, patient_id), now=NOW)


@pytest.mark.parametrize("roles", [None, "patient", frozenset({"patient"})])
def test_malformed_roles_are_denied(roles):
    actor = Principal("account-1", "hospital-a", roles, True, True, patient_id="patient-1")
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "patient-1"), now=NOW)


def test_malformed_guardian_time_is_denied():
    actor = principal(Role.GUARDIAN, user_id="guardian")
    grant = GuardianGrant("hospital-a", "guardian", "child", frozenset({Action.PATIENT_READ}),
                          True, True, "tomorrow", valid_from=NOW, revoked=False)
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "child"),
                          guardian_grant=grant, patient_is_minor=True, now=NOW)


def test_string_guardian_actions_are_not_trusted_as_verified_actions():
    actor = principal(Role.GUARDIAN, user_id="guardian")
    grant = GuardianGrant("hospital-a", "guardian", "child", frozenset({"patient.read"}),
                          True, True, NOW + timedelta(days=1), valid_from=NOW, revoked=False)
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "child"),
                          guardian_grant=grant, patient_is_minor=True, now=NOW)


def test_adding_patient_role_does_not_grant_an_admin_access_to_other_patients():
    actor = principal(Role.ORG_ADMIN, Role.PATIENT, user_id="admin")
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "patient"), now=NOW)


@pytest.mark.parametrize(("active", "tenant_active"), [("false", True), (True, "false"), (1, True)])
def test_malformed_active_flags_are_denied(active, tenant_active):
    actor = principal(Role.PATIENT, active=active, tenant_active=tenant_active)
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "user-1"), now=NOW)


@pytest.mark.parametrize(("verified", "consented"), [("false", True), (True, "false"), (1, True)])
def test_malformed_guardian_flags_are_denied(verified, consented):
    actor = principal(Role.GUARDIAN, user_id="guardian")
    grant = GuardianGrant("hospital-a", "guardian", "child", frozenset({Action.PATIENT_READ}),
                          verified, consented, NOW + timedelta(days=1), valid_from=NOW, revoked=False)
    assert not is_allowed(actor, Action.PATIENT_READ, Resource("hospital-a", "child"),
                          guardian_grant=grant, patient_is_minor=True, now=NOW)
