"""Experimental patient-scoped conversation access, not connected to the API.

The caller must retrieve the principal, patient identity, purpose-specific
consent and any guardian grants from authoritative sources for each operation.
The institution's approved consent version must be configured at repository
construction, not supplied by an untrusted request. These are snapshots;
this module does not authenticate users, refresh revoked grants, encrypt data,
or replace a complete PostgreSQL privilege and SECURITY DEFINER audit. Never
use it for real patient data until those controls and application integration
are in place. Rechecking the snapshot after a pool wait cannot detect a newly
revoked grant or expiry during a database query. Exceptions from PostgreSQL
must not be exposed to API callers.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from uuid import UUID

from psycopg import Connection
from psycopg.pq import TransactionStatus

from .access_policy import Action, GuardianGrant, Principal, Resource, Role, is_allowed
from .consent_policy import (
    ConsentPurpose,
    ConsentRecord,
    PatientIdentity,
    SubjectAge,
    has_current_consent,
)


class PatientResourceNotFound(Exception):
    """An absent, inaccessible or invalid patient resource."""


class UnsafeConnection(Exception):
    """A connection is unsuitable for an independently scoped transaction."""


@dataclass(frozen=True, slots=True)
class Conversation:
    conversation_id: UUID
    title: str


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    turn_id: UUID
    content: str


ConnectionFactory = Callable[[], AbstractContextManager[Connection]]


class PatientConversationRepository:
    """Use one independently scoped transaction for every repository call."""

    def __init__(self, connection_factory: ConnectionFactory, *,
                 required_health_consent_version: str | None = None) -> None:
        self._connection_factory = connection_factory
        self._required_health_consent_version = required_health_consent_version

    @contextmanager
    def _scope(
        self,
        principal: Principal,
        resource: Resource,
        action: Action,
        guardian_grant: GuardianGrant | None,
        subject: PatientIdentity | None,
        consent: ConsentRecord | None,
        consent_guardian_grant: GuardianGrant | None,
    ) -> Iterator[tuple[Connection, UUID, UUID, Callable[[], None]]]:
        def authorized() -> bool:
            if (not isinstance(subject, PatientIdentity) or not isinstance(principal, Principal)
                    or not isinstance(principal.roles, frozenset)
                    or not all(isinstance(role, Role) for role in principal.roles)
                    or not isinstance(resource, Resource) or subject.tenant_id != resource.tenant_id
                    or subject.patient_id != resource.patient_id):
                return False
            # Only an independently verified account/patient binding may use
            # self-access. Guardian access is limited to verified under-14s;
            # other minor rules have not been approved yet.
            self_access = (principal.user_id == subject.account_id and principal.patient_id == resource.patient_id
                           and Role.PATIENT in principal.roles)
            if not self_access and subject.age is not SubjectAge.UNDER_14:
                return False
            effective_principal = principal if self_access else replace(principal, patient_id=None)
            return (
                is_allowed(effective_principal, action, resource, guardian_grant=guardian_grant,
                           patient_is_minor=(subject.age is SubjectAge.UNDER_14))
                and has_current_consent(subject, consent, ConsentPurpose.HEALTH_INFORMATION, action,
                                        required_version=self._required_health_consent_version,
                                        guardian_grant=consent_guardian_grant)
            )

        def ensure_authorized() -> None:
            if not authorized():
                raise PatientResourceNotFound()

        ensure_authorized()
        try:
            tenant_id = UUID(resource.tenant_id)
            patient_id = UUID(resource.patient_id or "")
        except (ValueError, TypeError, AttributeError):
            raise PatientResourceNotFound() from None
        with self._connection_factory() as connection:
            # A pool wait can extend beyond the snapshot's expiration time.
            ensure_authorized()
            # A nested transaction would only create a savepoint; SET LOCAL
            # could then survive our call and leak into the outer transaction.
            if connection.info.transaction_status != TransactionStatus.IDLE:
                raise UnsafeConnection()
            with connection.transaction():
                # Do not inherit a previous borrower's function/type lookup path.
                connection.execute("SET LOCAL search_path TO pg_catalog")
                if connection.execute("SELECT medical_rag.runtime_role_basic_check()").fetchone() != (True,):
                    raise UnsafeConnection()
                ensure_authorized()
                connection.execute(
                    "SELECT pg_catalog.set_config('app.patient_scope', "
                    "pg_catalog.pg_current_xact_id()::text || '/' || %s::uuid::text || '/' || %s::uuid::text, true)",
                    (tenant_id, patient_id),
                )
                ensure_authorized()
                yield connection, tenant_id, patient_id, ensure_authorized

    @staticmethod
    def _id(value: str) -> UUID:
        try:
            return UUID(value)
        except (ValueError, TypeError, AttributeError):
            raise PatientResourceNotFound() from None

    def get_conversation(
        self, principal: Principal, resource: Resource, conversation_id: str,
        *, guardian_grant: GuardianGrant | None = None,
        subject: PatientIdentity | None = None, consent: ConsentRecord | None = None,
        consent_guardian_grant: GuardianGrant | None = None,
    ) -> Conversation:
        """Return one accessible conversation or the same not-found error."""
        conversation_uuid = self._id(conversation_id)
        with self._scope(principal, resource, Action.PATIENT_READ, guardian_grant,
                         subject, consent, consent_guardian_grant) as (connection, tenant, patient, _check):
            row = connection.execute(
                "SELECT conversation_id, title FROM medical_rag.conversations "
                "WHERE tenant_id = %s AND patient_id = %s AND conversation_id = %s",
                (tenant, patient, conversation_uuid),
            ).fetchone()
            if row is None:
                raise PatientResourceNotFound()
            return Conversation(*row)

    def list_conversations(
        self, principal: Principal, resource: Resource, *, guardian_grant: GuardianGrant | None = None,
        subject: PatientIdentity | None = None, consent: ConsentRecord | None = None,
        consent_guardian_grant: GuardianGrant | None = None,
    ) -> list[Conversation]:
        """List only conversations belonging to the authorized patient."""
        with self._scope(principal, resource, Action.PATIENT_READ, guardian_grant,
                         subject, consent, consent_guardian_grant) as (connection, tenant, patient, _check):
            rows = connection.execute(
                "SELECT conversation_id, title FROM medical_rag.conversations "
                "WHERE tenant_id = %s AND patient_id = %s ORDER BY created_at, conversation_id",
                (tenant, patient),
            ).fetchall()
            return [Conversation(*row) for row in rows]

    def create_conversation(
        self, principal: Principal, resource: Resource, conversation_id: str, title: str,
        *, guardian_grant: GuardianGrant | None = None,
        subject: PatientIdentity | None = None, consent: ConsentRecord | None = None,
        consent_guardian_grant: GuardianGrant | None = None,
    ) -> None:
        """Create a conversation only for the authorized patient subject."""
        conversation_uuid = self._id(conversation_id)
        with self._scope(principal, resource, Action.PATIENT_WRITE, guardian_grant,
                         subject, consent, consent_guardian_grant) as (connection, tenant, patient, _check):
            result = connection.execute(
                "INSERT INTO medical_rag.conversations (tenant_id, patient_id, conversation_id, title) "
                "SELECT tenant_id, patient_id, %s, %s FROM medical_rag.patient_subjects "
                "WHERE tenant_id = %s AND patient_id = %s",
                (conversation_uuid, title, tenant, patient),
            )
            if result.rowcount != 1:
                raise PatientResourceNotFound()

    def append_turn(
        self, principal: Principal, resource: Resource, conversation_id: str, turn_id: str, content: str,
        *, guardian_grant: GuardianGrant | None = None,
        subject: PatientIdentity | None = None, consent: ConsentRecord | None = None,
        consent_guardian_grant: GuardianGrant | None = None,
    ) -> None:
        """Append a turn after verifying the parent belongs to the patient."""
        conversation_uuid = self._id(conversation_id)
        turn_uuid = self._id(turn_id)
        with self._scope(principal, resource, Action.PATIENT_WRITE, guardian_grant,
                         subject, consent, consent_guardian_grant) as (connection, tenant, patient, check):
            parent = connection.execute(
                "SELECT 1 FROM medical_rag.conversations "
                "WHERE tenant_id = %s AND patient_id = %s AND conversation_id = %s FOR KEY SHARE",
                (tenant, patient, conversation_uuid),
            ).fetchone()
            if parent is None:
                raise PatientResourceNotFound()
            check()
            connection.execute(
                "INSERT INTO medical_rag.conversation_turns "
                "(tenant_id, patient_id, conversation_id, turn_id, content) VALUES (%s, %s, %s, %s, %s)",
                (tenant, patient, conversation_uuid, turn_uuid, content),
            )

    def list_turns(
        self, principal: Principal, resource: Resource, conversation_id: str,
        *, guardian_grant: GuardianGrant | None = None,
        subject: PatientIdentity | None = None, consent: ConsentRecord | None = None,
        consent_guardian_grant: GuardianGrant | None = None,
    ) -> list[ConversationTurn]:
        """Return turns for an accessible conversation, including an empty one."""
        conversation_uuid = self._id(conversation_id)
        with self._scope(principal, resource, Action.PATIENT_READ, guardian_grant,
                         subject, consent, consent_guardian_grant) as (connection, tenant, patient, check):
            parent = connection.execute(
                "SELECT 1 FROM medical_rag.conversations "
                "WHERE tenant_id = %s AND patient_id = %s AND conversation_id = %s",
                (tenant, patient, conversation_uuid),
            ).fetchone()
            if parent is None:
                raise PatientResourceNotFound()
            check()
            rows = connection.execute(
                "SELECT turn_id, content FROM medical_rag.conversation_turns "
                "WHERE tenant_id = %s AND patient_id = %s AND conversation_id = %s "
                "ORDER BY created_at, turn_id",
                (tenant, patient, conversation_uuid),
            ).fetchall()
            return [ConversationTurn(*row) for row in rows]
