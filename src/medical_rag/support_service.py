"""Structured account support, independent of health processing authority.

Every call uses live SessionStore authority and the mandatory same-TX audit.
Scope is trusted-app defense in depth, not arbitrary-SQL or owner protection.
Development admission caps are not institution-approved production rate policy.
"""

from hashlib import sha256
from uuid import UUID, uuid4

from .access_policy import Action, Resource, is_allowed
from .identity_sessions import AuthorizedTransaction, SessionStore
from .patient_service import JsonObject, ResourceMissing, _identifier, _page
from .security_audit import AuditAction, AuditStore

CATEGORIES = frozenset({'login_help', 'account_help', 'usage_help'})
RESOLUTIONS = frozenset({'guidance_provided', 'referred_to_institution', 'not_reproducible'})
_FIELDS = 'case_id,category,status,resolution_code,created_at,updated_at,requester_account_id,assigned_account_id'


class SupportDenied(Exception):
    """No current local institution support permission."""


class SupportConflict(Exception):
    """Visible case has no permitted transition from its current state."""


class SupportLimit(Exception):
    """Development admission cap reached."""


def _item(row: tuple, *, queue: bool = False) -> JsonObject:
    result = {'id': str(row[0]), 'category': row[1], 'status': row[2], 'resolution_code': row[3],
              'created_at': row[4].isoformat(), 'updated_at': row[5].isoformat()}
    if queue:
        result.update(requester_account_id=str(row[6]), assigned_account_id=str(row[7]) if row[7] else None)
    return result


class SupportService:
    """No detached principal, patient scope, generic lookup or authority writes."""

    def __init__(self, sessions: SessionStore) -> None:
        if not isinstance(sessions, SessionStore):
            raise TypeError('Trusted session store required')
        self.sessions = sessions
        self.audit = AuditStore(sessions)

    @staticmethod
    def _scope(authorized: AuthorizedTransaction, *, queue: bool = False) -> tuple[str, str]:
        principal = authorized.principal
        if queue and not is_allowed(principal, Action.SUPPORT_CASE_MANAGE, Resource(principal.tenant_id)):
            raise SupportDenied()
        mode = 'queue' if queue else 'owner/' + principal.user_id
        authorized.connection.execute(
            "SELECT set_config('app.support_scope',pg_current_xact_id()::text||'/'||%s||'/'||%s,true)",
            (principal.tenant_id, mode))
        return principal.tenant_id, principal.user_id

    def create(self, token: str, *, csrf_token: str, category: str) -> JsonObject:
        with self.audit.operation(token, AuditAction.SUPPORT_CREATE, mutation=True, csrf_token=csrf_token) as authorized:
            tenant, account = self._scope(authorized)
            if type(category) is not str or category not in CATEGORIES:
                raise ValueError('Invalid request')
            key = int.from_bytes(sha256(b'medical-rag/support-admission/' + UUID(tenant).bytes +
                                       UUID(account).bytes).digest()[:8], 'big', signed=True)
            db = authorized.connection
            db.execute('SELECT pg_catalog.pg_advisory_xact_lock(%s)', (key,))
            authorized.refresh()
            # Fresh bounded indexed counts AFTER advisory lock wait. Cancellation
            # deliberately does not erase the rolling 24-hour admission count.
            recent = db.execute("SELECT count(*) FROM (SELECT 1 FROM medical_rag.support_cases "
                                "WHERE tenant_id=%s AND requester_account_id=%s "
                                "AND created_at>statement_timestamp()-interval '24 hours' LIMIT 20) s",
                                (tenant, account)).fetchone()[0]
            active = db.execute("SELECT count(*) FROM (SELECT 1 FROM medical_rag.support_cases "
                                "WHERE tenant_id=%s AND requester_account_id=%s "
                                "AND status IN ('open','in_progress') LIMIT 5) s", (tenant, account)).fetchone()[0]
            if recent >= 20 or active >= 5:
                raise SupportLimit()
            row = db.execute('INSERT INTO medical_rag.support_cases(tenant_id,case_id,requester_account_id,category) '
                             'VALUES (%s,%s,%s,%s) RETURNING ' + _FIELDS,
                             (tenant, uuid4(), account, category)).fetchone()
            return _item(row)

    def list(self, token: str, *, queue: bool = False, limit: int = 20, offset: int = 0) -> JsonObject:
        with self.audit.operation(token, AuditAction.SUPPORT_QUEUE if queue else AuditAction.SUPPORT_LIST) as authorized:
            tenant, account = self._scope(authorized, queue=queue)
            _page(limit, offset)
            params = (tenant,) if queue else (tenant, account)
            predicate = 'tenant_id=%s' + ('' if queue else ' AND requester_account_id=%s')
            rows = authorized.connection.execute('SELECT ' + _FIELDS + ' FROM medical_rag.support_cases WHERE ' +
                                                 predicate + ' ORDER BY created_at DESC,case_id LIMIT %s OFFSET %s',
                                                 (*params, limit, offset)).fetchall()
            return {'items': [_item(row, queue=queue) for row in rows], 'limit': limit, 'offset': offset}

    def get(self, token: str, identifier: str) -> JsonObject:
        with self.audit.operation(token, AuditAction.SUPPORT_READ) as authorized:
            tenant, account = self._scope(authorized)
            row = authorized.connection.execute('SELECT ' + _FIELDS + ' FROM medical_rag.support_cases '
                                                 'WHERE tenant_id=%s AND requester_account_id=%s AND case_id=%s',
                                                 (tenant, account, _identifier(identifier))).fetchone()
            if row is None:
                raise ResourceMissing()
            return _item(row)

    def transition(self, token: str, identifier: str, *, csrf_token: str, operation: str,
                   resolution_code: str | None = None) -> JsonObject:
        if operation not in {'cancel', 'take', 'resolve'}:
            raise ValueError('Invalid request')
        queue = operation != 'cancel'
        action = {'cancel': AuditAction.SUPPORT_CANCEL, 'take': AuditAction.SUPPORT_TAKE,
                  'resolve': AuditAction.SUPPORT_RESOLVE}[operation]
        with self.audit.operation(token, action, mutation=True, csrf_token=csrf_token) as authorized:
            tenant, account = self._scope(authorized, queue=queue)
            case_id = _identifier(identifier)
            if operation == 'resolve' and (type(resolution_code) is not str or resolution_code not in RESOLUTIONS):
                raise ValueError('Invalid request')
            db = authorized.connection
            params = (tenant, case_id) if queue else (tenant, case_id, account)
            predicate = 'tenant_id=%s AND case_id=%s' + ('' if queue else ' AND requester_account_id=%s')
            db.execute('SELECT case_id FROM medical_rag.support_cases WHERE ' + predicate + ' FOR UPDATE', params)
            authorized.refresh()
            self._scope(authorized, queue=queue)
            # Fresh SQL after lock wait: never authorize from the waiting SELECT snapshot.
            row = db.execute('SELECT ' + _FIELDS + ' FROM medical_rag.support_cases WHERE ' + predicate, params).fetchone()
            if row is None:
                raise ResourceMissing()
            if ((operation == 'cancel' and row[2] not in {'open', 'in_progress'})
                    or (operation == 'take' and row[2] != 'open')
                    or (operation == 'resolve' and (row[2] != 'in_progress' or str(row[7]) != account))):
                raise SupportConflict()
            status = {'cancel': 'cancelled', 'take': 'in_progress', 'resolve': 'resolved'}[operation]
            assignee = account if operation == 'take' else row[7]
            row = db.execute('UPDATE medical_rag.support_cases SET status=%s,assigned_account_id=%s,resolution_code=%s '
                             'WHERE ' + predicate + ' RETURNING ' + _FIELDS,
                             (status, assignee, resolution_code, *params)).fetchone()
            return _item(row, queue=queue)
