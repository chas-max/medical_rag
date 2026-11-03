"""Purpose-specific consent is separate from identity and resource access."""

import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from medical_rag.access_policy import Action, GuardianGrant

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_consent_policy_exists():
    assert importlib.util.find_spec("medical_rag.consent_policy") is not None


def _module():
    from medical_rag import consent_policy
    return consent_policy


def _adult():
    cp = _module()
    return cp.PatientIdentity("hospital-a", "patient-1", "account-1", cp.SubjectAge.ADULT, True)


def _record(**changes):
    cp = _module()
    record = cp.ConsentRecord("hospital-a", "patient-1", cp.ConsentPurpose.HEALTH_INFORMATION,
                              "v1", "account-1", True, False, NOW - timedelta(days=1),
                              NOW + timedelta(days=1), NOW - timedelta(days=1))
    return replace(record, **changes)


def _check(subject, consent, *, purpose=None, action=Action.PATIENT_READ, version="v1", guardian=None, now=NOW):
    cp = _module()
    if purpose is None:
        purpose = cp.ConsentPurpose.HEALTH_INFORMATION
    return cp.has_current_consent(subject, consent, purpose, action, required_version=version,
                                  guardian_grant=guardian, now=now)


def _guardian(**changes):
    grant = GuardianGrant("hospital-a", "guardian-1", "patient-1", frozenset({Action.PATIENT_READ}),
                          True, True, NOW + timedelta(days=1), valid_from=NOW - timedelta(days=1), revoked=False)
    return replace(grant, **changes)


def test_adult_current_matching_consent_is_valid():
    assert _check(_adult(), _record())


def test_adult_must_grant_own_consent_through_bound_account():
    assert not _check(_adult(), _record(grantor_user_id="somebody-else"))


@pytest.mark.parametrize("purpose_name", ["HEALTH_INFORMATION", "HOSPITAL_RECORD", "EXTERNAL_MODEL"])
def test_each_purpose_requires_its_own_specific_consent(purpose_name):
    cp = _module()
    purpose = getattr(cp.ConsentPurpose, purpose_name)
    assert _check(_adult(), _record(purpose=purpose), purpose=purpose)
    assert not _check(_adult(), _record(purpose=purpose), purpose="unverified-purpose")
    for other in cp.ConsentPurpose:
        if other is not purpose:
            assert not _check(_adult(), _record(purpose=purpose), purpose=other)


@pytest.mark.parametrize(("field", "value"), [
    ("tenant_id", "hospital-b"), ("patient_id", "patient-2"), ("version", "v0"),
    ("version", ""), ("grantor_user_id", ""), ("granted", False), ("granted", "true"),
    ("revoked", True), ("revoked", "false"), ("effective_from", NOW + timedelta(seconds=1)),
    ("effective_from", datetime(2025, 1, 1)), ("expires_at", NOW), ("expires_at", "tomorrow"),
])
def test_invalid_consent_record_is_denied(field, value):
    assert not _check(_adult(), _record(**{field: value}))


@pytest.mark.parametrize(("field", "value"), [
    ("tenant_id", ""), ("tenant_id", 1), ("patient_id", " "), ("patient_id", 1),
    ("account_id", ""), ("account_id", 1), ("identity_verified", False), ("identity_verified", "true"),
    ("age", "adult"), ("age", None),
])
def test_invalid_subject_identity_is_denied(field, value):
    assert not _check(replace(_adult(), **{field: value}), _record())


@pytest.mark.parametrize("version", [None, "", " ", 1, "v2"])
def test_expected_approved_consent_version_is_required(version):
    assert not _check(_adult(), _record(), version=version)


@pytest.mark.parametrize("age_name", ["OLDER_MINOR", "UNKNOWN"])
def test_other_minors_and_unknown_age_are_denied_until_reviewed_rules_exist(age_name):
    cp = _module()
    assert not _check(replace(_adult(), age=getattr(cp.SubjectAge, age_name)), _record())


def test_under_14_requires_verified_guardian_and_consent_from_that_guardian():
    cp = _module()
    child = replace(_adult(), age=cp.SubjectAge.UNDER_14)
    consent = _record(grantor_user_id="guardian-1")
    assert _check(child, consent, guardian=_guardian())
    assert not _check(child, consent)
    assert not _check(child, _record(grantor_user_id="account-1"), guardian=_guardian())
    assert not _check(child, consent, guardian=_guardian(), action=Action.PATIENT_WRITE)


def test_under_14_without_personal_account_can_use_verified_guardian_consent():
    cp = _module()
    child = replace(_adult(), age=cp.SubjectAge.UNDER_14, account_id=None)
    consent = _record(grantor_user_id="guardian-1")
    assert _check(child, consent, guardian=_guardian())
    assert not _check(child, consent)
    assert not _check(replace(child, identity_verified=False), consent, guardian=_guardian())


@pytest.mark.parametrize("account_id", ["", " ", 1, False])
def test_under_14_rejects_malformed_non_null_account_id(account_id):
    cp = _module()
    child = replace(_adult(), age=cp.SubjectAge.UNDER_14, account_id=account_id)
    assert not _check(child, _record(grantor_user_id="guardian-1"), guardian=_guardian())


def test_guardian_must_be_valid_when_child_consent_was_signed():
    cp = _module()
    child = replace(_adult(), age=cp.SubjectAge.UNDER_14)
    consent = _record(grantor_user_id="guardian-1", signed_at=NOW - timedelta(days=2))
    assert not _check(child, consent, guardian=_guardian())


@pytest.mark.parametrize("signed_at", [
    NOW + timedelta(seconds=1), NOW - timedelta(hours=1), datetime(2025, 1, 1), "2025-01-01", None,
])
def test_consent_requires_valid_signature_before_effective_time(signed_at):
    assert not _check(_adult(), _record(signed_at=signed_at))


def test_hospital_record_consent_cannot_authorize_write():
    cp = _module()
    purpose = cp.ConsentPurpose.HOSPITAL_RECORD
    assert not _check(_adult(), _record(purpose=purpose), purpose=purpose, action=Action.PATIENT_WRITE)


@pytest.mark.parametrize(("field", "value"), [
    ("tenant_id", "hospital-b"), ("patient_id", "patient-2"), ("guardian_id", "guardian-2"),
    ("verified", False), ("verified", "true"), ("consented", False), ("consented", "true"),
    ("revoked", True), ("revoked", "false"), ("valid_from", NOW + timedelta(seconds=1)),
    ("valid_from", datetime(2025, 1, 1)), ("expires_at", NOW), ("expires_at", "tomorrow"),
    ("actions", frozenset({"patient.read"})),
])
def test_under_14_rejects_wrong_or_invalid_guardian_grants(field, value):
    cp = _module()
    child = replace(_adult(), age=cp.SubjectAge.UNDER_14)
    assert not _check(child, _record(grantor_user_id="guardian-1"), guardian=_guardian(**{field: value}))


@pytest.mark.parametrize("now", [datetime(2026, 1, 1), "2026-01-01", 1])
def test_invalid_current_time_is_denied(now):
    assert not _check(_adult(), _record(), now=now)


@pytest.mark.parametrize("action", ["patient.read", Action.KNOWLEDGE_READ, None])
def test_invalid_operation_is_denied(action):
    assert not _check(_adult(), _record(), action=action)
