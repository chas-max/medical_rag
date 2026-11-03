"""Experimental purpose-specific consent decisions, not production authorization.

Inputs must be fresh records from verified, authoritative sources; never build
them from client-supplied flags. Callers must also independently authenticate,
authorize the operator's role and resource ownership, and check current patient,
consent and guardian status before processing. A snapshot cannot guarantee
immediate revocation or detect concurrent withdrawal. The policy is used by an
experimental data repository but is not connected to a production identity
source or business API and must not be used with real patient data.
It is not a substitute for legal, clinical or institutional review.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from .access_policy import Action, GuardianGrant


class ConsentPurpose(Enum):
    HEALTH_INFORMATION = "health_information"
    HOSPITAL_RECORD = "hospital_record"
    EXTERNAL_MODEL = "external_model"


class SubjectAge(Enum):
    ADULT = "adult"
    UNDER_14 = "under_14"
    OLDER_MINOR = "older_minor"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PatientIdentity:
    """Verified patient subject and, when present, its verified account binding."""

    tenant_id: str
    patient_id: str
    account_id: str | None
    age: SubjectAge
    identity_verified: bool


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    tenant_id: str
    patient_id: str
    purpose: ConsentPurpose
    version: str
    grantor_user_id: str
    granted: bool
    revoked: bool
    effective_from: datetime
    expires_at: datetime
    # Supplied by the trusted consent record, never inferred from effective_from.
    signed_at: datetime


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def has_current_consent(
    subject: PatientIdentity,
    consent: ConsentRecord,
    purpose: ConsentPurpose,
    action: Action,
    *,
    required_version: str,
    guardian_grant: GuardianGrant | None = None,
    now: datetime | None = None,
) -> bool:
    """Check a trusted consent snapshot for one purpose and patient operation.

    ``required_version`` must come from the institution's current approved
    notice configuration. This function does not retrieve or authorize records.
    """
    checked_at = datetime.now(UTC) if now is None else now
    if (not isinstance(subject, PatientIdentity) or not isinstance(consent, ConsentRecord)
            or not isinstance(purpose, ConsentPurpose) or not isinstance(action, Action)
            or action not in {Action.PATIENT_READ, Action.PATIENT_WRITE}
            or (purpose is ConsentPurpose.HOSPITAL_RECORD and action is not Action.PATIENT_READ)
            or not _valid_id(subject.tenant_id) or not _valid_id(subject.patient_id)
            or subject.identity_verified is not True
            or not isinstance(subject.age, SubjectAge)
            or (subject.account_id is None and subject.age is not SubjectAge.UNDER_14)
            or (subject.account_id is not None and not _valid_id(subject.account_id))
            or not _valid_id(consent.tenant_id) or not _valid_id(consent.patient_id)
            or not _valid_id(consent.grantor_user_id) or not _valid_id(consent.version)
            or not _valid_id(required_version) or not isinstance(consent.purpose, ConsentPurpose)
            or consent.purpose is not purpose or consent.version != required_version
            or consent.tenant_id != subject.tenant_id or consent.patient_id != subject.patient_id
            or consent.granted is not True or consent.revoked is not False
            or not _aware(checked_at) or not _aware(consent.signed_at)
            or not _aware(consent.effective_from) or not _aware(consent.expires_at)
            or not consent.signed_at <= consent.effective_from <= checked_at < consent.expires_at):
        return False
    if subject.age is SubjectAge.ADULT:
        return consent.grantor_user_id == subject.account_id
    if subject.age is not SubjectAge.UNDER_14 or not isinstance(guardian_grant, GuardianGrant):
        # An institution-reviewed rule is required for other minors; absent one,
        # do not guess their consent requirements.
        return False
    return (
        guardian_grant.tenant_id == subject.tenant_id
        and guardian_grant.patient_id == subject.patient_id
        and guardian_grant.guardian_id == consent.grantor_user_id
        and guardian_grant.verified is True
        and guardian_grant.consented is True
        and guardian_grant.revoked is False
        and isinstance(guardian_grant.actions, frozenset)
        and all(isinstance(item, Action) for item in guardian_grant.actions)
        and action in guardian_grant.actions
        and _aware(guardian_grant.valid_from)
        and _aware(guardian_grant.expires_at)
        and guardian_grant.valid_from <= consent.signed_at < guardian_grant.expires_at
        and guardian_grant.valid_from <= checked_at < guardian_grant.expires_at
    )
