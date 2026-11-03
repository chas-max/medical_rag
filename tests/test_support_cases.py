"""Structured account support against disposable PostgreSQL; fictional data only."""
# ruff: noqa: F401,F811 -- shared pytest fixtures
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from uuid import uuid4

import psycopg
import pytest
from test_patient_vertical import (
    auth_system,
    database,
    oidc_database,
    signing_key,
    system,
)


@dataclass(repr=False)
class Private:
    value: object = field(repr=False)


@pytest.fixture
def support_system(system):
    return Private(system)


@pytest.fixture
def support_database(database):
    return Private(database)


@pytest.mark.integration
def test_owner_structured_case_is_available_without_health_consent(support_system):
    from test_patient_vertical import logged_in
    client, _, _, _ = support_system.value
    headers = logged_in(support_system.value)
    response = client.post('/support/cases', headers=headers, json={'category': 'login_help'})
    available = response.status_code == 200
    assert available, 'Authenticated account support must be available without health consent'


def check(value: bool) -> None:
    if type(value) is not bool:
        raise TypeError('Boolean assertion required')
    assert value, 'Structured support contract failed'


def issue(private, account=None, roles=('support',), mfa=True):
    from test_patient_vertical import ACCOUNT, ISSUER, OTHER_ACCOUNT, TENANT, mutate

    from medical_rag.oidc_identity import VerifiedIdentity
    client, login, _, admin = private.value
    account = account or ACCOUNT
    mutate(admin, 'DELETE FROM medical_rag.identity_roles WHERE account_id=%s', (account,))
    for role in roles:
        mutate(admin, 'INSERT INTO medical_rag.identity_roles VALUES (%s,%s,%s)', (TENANT, account, role))
    issued = login.sessions.issue(VerifiedIdentity(ISSUER, 'subject-2' if account == OTHER_ACCOUNT else 'subject-1', mfa))
    client.cookies.clear()
    client.cookies.set('__Host-medical-session', issued.token)
    from test_patient_vertical import APP_ORIGIN
    return {'Origin': APP_ORIGIN, 'X-CSRF-Token': issued.csrf_token}


def create(private, headers):
    response = private.value[0].post('/support/cases', headers=headers, json={'category': 'account_help'})
    check(response.status_code == 200)
    return response.json()['data']['id']


@pytest.mark.integration
def test_owner_and_staff_state_machine_minimal_representation_and_health_denial(support_system):
    from test_patient_vertical import OTHER_ACCOUNT
    client = support_system.value[0]
    owner = issue(support_system, roles=('guardian',))
    identifier = create(support_system, owner)
    own = client.get('/support/cases/' + identifier).json()['data']
    check(set(own) == {'id','category','status','resolution_code','created_at','updated_at'})
    check(client.get('/me').json()['data']['permissions']['support_self'] is True)
    check(client.get('/conversations').status_code == 403)
    staff = issue(support_system, OTHER_ACCOUNT)
    check(client.get('/support/cases/' + identifier).status_code == 404)
    check(client.delete('/support/cases/' + identifier, headers=staff).status_code == 404)
    queue = client.get('/support/queue').json()['data']['items']
    check(len(queue) == 1 and set(queue[0]) == set(own) | {'requester_account_id','assigned_account_id'})
    check(client.post('/support/queue/' + identifier + '/resolve', headers=staff,
                      json={'resolution_code':'guidance_provided'}).status_code == 409)
    check(client.post('/support/queue/' + identifier + '/take', headers=staff, json={}).status_code == 200)
    check(client.post('/support/queue/' + identifier + '/take', headers=staff, json={}).status_code == 409)
    other = issue(support_system)
    check(client.post('/support/queue/' + identifier + '/resolve', headers=other,
                      json={'resolution_code':'not_reproducible'}).status_code == 409)
    staff = issue(support_system, OTHER_ACCOUNT)
    check(client.post('/support/queue/' + identifier + '/resolve', headers=staff,
                      json={'resolution_code':'referred_to_institution'}).status_code == 200)
    owner = issue(support_system, roles=('guardian',))
    check(client.delete('/support/cases/' + identifier, headers=owner).status_code == 409)
    check(client.get('/support/cases').json()['data']['items'][0]['status'] == 'resolved')
    check(client.get('/conversations').status_code == 403)


@pytest.mark.integration
@pytest.mark.parametrize('roles,mfa,allowed', [
    (('support',), True, True), (('support',), False, True),
    (('support','org_admin'), False, False), (('support','auditor'), False, False),
    (('support','patient'), True, True), (('org_admin',), True, False),
    (('auditor',), True, False), (('knowledge_admin',), True, False),
    (('integration_admin',), True, False), (('platform_operator',), True, False),
    (('patient',), True, False), (('guardian',), True, False),
])
def test_queue_uses_current_local_role_combination(support_system, roles, mfa, allowed):
    issue(support_system, roles=roles, mfa=mfa)
    client = support_system.value[0]
    check(client.get('/support/queue').status_code == (200 if allowed else 403))
    check(client.get('/me').json()['data']['permissions']['support_queue'] is allowed)


@pytest.mark.integration
def test_validation_authentication_origin_csrf_uniform_missing_and_minimal_audit(support_system, caplog):
    from test_patient_vertical import (
        ACCOUNT,
        ISSUER,
        OTHER_ACCOUNT,
        OTHER_TENANT,
        TENANT,
        mutate,
    )
    from test_security_audit import events
    client, _, _, admin = support_system.value
    check(client.post('/support/cases', content='invalid').status_code == 403)  # Origin boundary first
    check(client.get('/support/cases/not-uuid').status_code == 401)
    headers = issue(support_system, roles=('guardian',))
    identifier = create(support_system, headers)
    for body in [{'category':'login_help','patient_id':'private-canary'}, {'category':'other'}, {},
                 {'category':'login_help','account_id':ACCOUNT}, {'category':'login_help','status':'open'}]:
        check(client.post('/support/cases', headers=headers, json=body).status_code == 400)
    check(client.request('DELETE','/support/cases/'+identifier,headers=headers,json={'tenant_id':'private-canary'}).status_code == 400)
    check(client.request('DELETE','/support/cases/'+identifier,headers=headers,content='{"x":1,"x":2}').status_code == 400)
    check(client.post('/support/cases', headers=headers, content='{"category":"login_help","category":"usage_help"}').status_code == 400)
    check(client.post('/support/cases', headers=headers | {'Origin':'https://invalid.example'}, json={'category':'login_help'}).status_code == 403)
    check(client.post('/support/cases', headers=headers | {'X-CSRF-Token':'private-csrf-canary'}, json={'category':'login_help'}).status_code == 401)
    for query in ['limit=0','limit=101','offset=-1','offset=10001','limit=x','limit=1&limit=2','tenant_id=x']:
        check(client.get('/support/cases?' + query).status_code == 400)
    issue(support_system, OTHER_ACCOUNT, roles=('guardian',))
    missing = [client.get('/support/cases/' + x) for x in [identifier, str(uuid4()), 'not-a-uuid']]
    check(all(r.status_code == 404 and r.json() == missing[0].json() for r in missing))
    staff = issue(support_system)
    check(client.post('/support/queue/' + identifier + '/take', headers=staff, json={'assignee':ACCOUNT}).status_code == 400)
    check(client.post('/support/queue/' + identifier + '/resolve', headers=staff, json={'resolution_code':'free-text'}).status_code == 400)
    with psycopg.connect(**admin) as db:
        db.execute('INSERT INTO medical_rag.identity_accounts(tenant_id,account_id,issuer,subject) VALUES (%s,%s,%s,%s)',
                   (OTHER_TENANT, ACCOUNT, ISSUER, 'foreign-support'))
        foreign = uuid4()
        db.execute('INSERT INTO medical_rag.support_cases(tenant_id,case_id,requester_account_id,category) VALUES (%s,%s,%s,%s)',
                   (OTHER_TENANT, foreign, ACCOUNT, 'usage_help'))
    check(client.post('/support/queue/' + str(foreign) + '/take', headers=staff, json={}).status_code == 404)
    check(all(item['id'] != str(foreign) for item in client.get('/support/queue').json()['data']['items']))
    import json
    serialized = json.dumps(events(admin), default=str) + caplog.text
    check(not any(raw in serialized for raw in [identifier, str(foreign), 'private-canary','private-csrf-canary',headers['X-CSRF-Token']]))
    check(events(admin)[-1]['resource_type'] == 'support_case')


@pytest.mark.integration
def test_cancel_retention_and_rolling_daily_cap(support_system):
    client = support_system.value[0]
    headers = issue(support_system, roles=('guardian',))
    for _ in range(20):
        identifier = create(support_system, headers)
        check(client.delete('/support/cases/' + identifier, headers=headers).json()['data']['status'] == 'cancelled')
        check(client.delete('/support/cases/' + identifier, headers=headers).status_code == 409)
    check(client.post('/support/cases', headers=headers, json={'category':'usage_help'}).status_code == 429)
    check(len(client.get('/support/cases?limit=100').json()['data']['items']) == 20)


@pytest.mark.integration
def test_parallel_admissions_distinct_sessions_cannot_exceed_active_cap(support_system):
    from test_patient_vertical import ISSUER

    from medical_rag.oidc_identity import VerifiedIdentity
    from medical_rag.support_service import SupportLimit, SupportService
    _, login, _, _ = support_system.value
    issue(support_system, roles=('guardian',))
    sessions = [login.sessions.issue(VerifiedIdentity(ISSUER,'subject-1',False)) for _ in range(10)]
    service = SupportService(login.sessions)
    def attempt(index):
        try:
            service.create(sessions[index].token, csrf_token=sessions[index].csrf_token, category='usage_help')
            return True
        except SupportLimit:
            return False
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(attempt, range(10)))
    check(sum(results) == 5)


@pytest.mark.integration
def test_audit_loss_rolls_back_and_reads_fail_closed(support_system):
    from test_security_audit import audit_insert
    client, _, _, admin = support_system.value
    headers = issue(support_system)
    identifier = create(support_system, headers)
    audit_insert(admin, allowed=False)
    try:
        check(client.post('/support/cases', headers=headers, json={'category':'usage_help'}).status_code == 503)
        check(client.delete('/support/cases/' + identifier, headers=headers).status_code == 503)
        check(client.get('/support/cases').status_code == 503)
        check(client.post('/support/queue/' + identifier + '/take', headers=headers, json={}).status_code == 503)
    finally:
        audit_insert(admin, allowed=True)
    with psycopg.connect(**admin) as db:
        check(db.execute('SELECT status FROM medical_rag.support_cases').fetchall() == [('open',)])


@pytest.mark.integration
def test_preflight_requires_schema7_force_rls_minimal_support_grants(support_system, support_database):
    from types import SimpleNamespace

    from test_patient_vertical import TENANT, mutate

    from medical_rag.security_config import StartupError
    from medical_rag.security_server import preflight
    runtime, admin = support_database.value
    config = SimpleNamespace(database_dsn=psycopg.conninfo.make_conninfo(**{k:v for k,v in runtime.items() if k != 'autocommit'}), tenant_id=TENANT)
    preflight(config)
    for change,restore in [
        ('ALTER TABLE medical_rag.support_cases RENAME TO hidden_support','ALTER TABLE medical_rag.hidden_support RENAME TO support_cases'),
        ('ALTER TABLE medical_rag.support_cases NO FORCE ROW LEVEL SECURITY','ALTER TABLE medical_rag.support_cases FORCE ROW LEVEL SECURITY'),
        ('REVOKE UPDATE(status) ON medical_rag.support_cases FROM login_test','GRANT UPDATE(status) ON medical_rag.support_cases TO login_test'),
    ]:
        mutate(admin,change)
        try:
            with pytest.raises(StartupError):
                preflight(config)
        finally:
            mutate(admin,restore)
    for grant in ['UPDATE(category)','UPDATE(created_at)','INSERT(updated_at)','DELETE','TRUNCATE','REFERENCES','TRIGGER']:
        mutate(admin, f'GRANT {grant} ON medical_rag.support_cases TO login_test')
        try:
            with pytest.raises(StartupError):
                preflight(config)
        finally:
            mutate(admin, f'REVOKE {grant} ON medical_rag.support_cases FROM login_test')


@pytest.mark.integration
@pytest.mark.parametrize('operation,change', [('take','role'),('take','account'),('resolve','tenant'),('cancel','account')])
def test_post_case_lock_wait_refreshes_current_authority(support_system, support_database, operation, change):
    from test_patient_vertical import ACCOUNT, TENANT, mutate, wait_for_lock

    from medical_rag.identity_sessions import AuthenticationDenied, SessionStore
    from medical_rag.support_service import SupportService
    client, _, _, admin = support_system.value
    runtime, _ = support_database.value
    headers = issue(support_system)
    identifier = create(support_system, headers)
    if operation == 'resolve':
        check(client.post('/support/queue/' + identifier + '/take', headers=headers, json={}).status_code == 200)
    app_name = 'support-wait-' + uuid4().hex
    service = SupportService(SessionStore(lambda:psycopg.connect(**dict(runtime, application_name=app_name)), tenant_id=TENANT))
    token = client.cookies.get('__Host-medical-session')
    with psycopg.connect(**dict(admin,autocommit=False)) as locker:
        locker.execute('SELECT case_id FROM medical_rag.support_cases WHERE tenant_id=%s AND case_id=%s FOR UPDATE', (TENANT,identifier))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(service.transition,token,identifier,csrf_token=headers['X-CSRF-Token'],operation=operation,
                                  resolution_code='guidance_provided' if operation=='resolve' else None)
            try:
                wait_for_lock(admin,app_name)
                if change == 'role':
                    mutate(admin,'DELETE FROM medical_rag.identity_roles WHERE account_id=%s',(ACCOUNT,))
                elif change == 'account':
                    mutate(admin,'UPDATE medical_rag.identity_accounts SET active=false WHERE account_id=%s',(ACCOUNT,))
                else:
                    mutate(admin,'UPDATE medical_rag.tenants SET active=false WHERE tenant_id=%s',(TENANT,))
            finally:
                locker.commit()
            with pytest.raises(AuthenticationDenied):
                pending.result(timeout=5)
    with psycopg.connect(**admin) as db:
        check(db.execute('SELECT status FROM medical_rag.support_cases').fetchone()[0] == ('in_progress' if operation=='resolve' else 'open'))


@pytest.mark.integration
@pytest.mark.parametrize('race', ['take','cancel_resolve'])
def test_case_transition_races_have_exactly_one_winner(support_system, race):
    from test_patient_vertical import ISSUER, OTHER_ACCOUNT

    from medical_rag.oidc_identity import VerifiedIdentity
    from medical_rag.support_service import SupportConflict, SupportService
    _, login, _, _ = support_system.value
    headers = issue(support_system)
    identifier = create(support_system,headers)
    issue(support_system,OTHER_ACCOUNT)
    first = login.sessions.issue(VerifiedIdentity(ISSUER,'subject-1',True))
    second = login.sessions.issue(VerifiedIdentity(ISSUER,'subject-2',True))
    service = SupportService(login.sessions)
    if race == 'cancel_resolve':
        service.transition(second.token,identifier,csrf_token=second.csrf_token,operation='take')
    def attempt(index):
        issued = [first,second][index]
        operation = 'take' if race=='take' else ['cancel','resolve'][index]
        try:
            service.transition(issued.token,identifier,csrf_token=issued.csrf_token,operation=operation,
                               resolution_code='guidance_provided' if operation=='resolve' else None)
            return True
        except SupportConflict:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt,range(2)))
    check(sum(results) == 1)


@pytest.mark.integration
def test_rls_scope_lifetime_minimal_grants_constraints_and_owner_recipe(support_system,support_database):
    from test_patient_vertical import ACCOUNT, ISSUER, ROOT, TENANT

    from medical_rag.identity_sessions import SessionStore
    from medical_rag.oidc_identity import VerifiedIdentity
    from medical_rag.support_service import SupportService
    headers = issue(support_system)
    identifier = create(support_system,headers)
    runtime,admin = support_database.value
    with psycopg.connect(**runtime) as db:
        check(db.execute('SELECT count(*) FROM medical_rag.support_cases').fetchone() == (0,))
        db.execute("SELECT set_config('app.support_scope',pg_current_xact_id()::text||'/'||%s||'/owner/'||%s,false)",(TENANT,ACCOUNT))
        check(db.execute('SELECT count(*) FROM medical_rag.support_cases').fetchone() == (1,))
        db.commit()
        check(db.execute('SELECT count(*) FROM medical_rag.support_cases').fetchone() == (0,))
        db.commit()
        for sql in ['UPDATE medical_rag.support_cases SET category=category','DELETE FROM medical_rag.support_cases',
                    'TRUNCATE medical_rag.support_cases','UPDATE medical_rag.identity_accounts SET active=true']:
            with pytest.raises(psycopg.errors.InsufficientPrivilege),db.transaction():
                db.execute(sql)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute('INSERT INTO medical_rag.support_cases(tenant_id,case_id,requester_account_id,category) VALUES (%s,%s,%s,%s)',
                       (TENANT,uuid4(),ACCOUNT,'login_help'))
    with psycopg.connect(**admin) as db:
        with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
            db.execute("UPDATE medical_rag.support_cases SET status='resolved' WHERE case_id=%s",(identifier,))
        with pytest.raises(psycopg.errors.RaiseException), db.transaction():
            db.execute("UPDATE medical_rag.support_cases SET category='usage_help' WHERE case_id=%s",(identifier,))
        role = 'support_recipe_' + uuid4().hex[:12]
        db.execute(psycopg.sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE')
                   .format(psycopg.sql.Identifier(role),psycopg.sql.Literal(admin['password'])))
        recipe = (ROOT/'database/patient_runtime_grants.sql').read_text(encoding='utf-8')
        recipe = '\n'.join(line for line in recipe.splitlines() if not line.startswith('\\'))
        db.execute(recipe.replace(':"runtime_role"',psycopg.sql.Identifier(role).as_string(db)))
    params = dict(admin,user=role,autocommit=False)
    sessions = SessionStore(lambda:psycopg.connect(**params),tenant_id=TENANT)
    issued = sessions.issue(VerifiedIdentity(ISSUER,'subject-1',True))
    service = SupportService(sessions)
    created = service.create(issued.token,csrf_token=issued.csrf_token,category='login_help')
    service.transition(issued.token,created['id'],csrf_token=issued.csrf_token,operation='take')
    result = service.transition(issued.token,created['id'],csrf_token=issued.csrf_token,operation='resolve',resolution_code='guidance_provided')
    check(result['status'] == 'resolved')


@pytest.mark.integration
def test_parallel_rolling_daily_admission_cannot_exceed_twenty(support_system):
    from test_patient_vertical import ISSUER

    from medical_rag.oidc_identity import VerifiedIdentity
    from medical_rag.support_service import SupportLimit, SupportService
    _,login,_,_ = support_system.value
    headers = issue(support_system,roles=('guardian',))
    for _ in range(19):
        identifier = create(support_system,headers)
        check(support_system.value[0].delete('/support/cases/'+identifier,headers=headers).status_code==200)
    sessions = [login.sessions.issue(VerifiedIdentity(ISSUER,'subject-1',False)) for _ in range(6)]
    service = SupportService(login.sessions)
    def attempt(index):
        try:
            service.create(sessions[index].token,csrf_token=sessions[index].csrf_token,category='usage_help')
            return True
        except SupportLimit:
            return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(attempt,range(6)))
    check(sum(results)==1)


@pytest.mark.integration
def test_requester_can_cancel_an_assigned_case_without_takeover(support_system):
    from test_patient_vertical import OTHER_ACCOUNT
    client = support_system.value[0]
    owner = issue(support_system,roles=())
    identifier = create(support_system,owner)
    staff = issue(support_system,OTHER_ACCOUNT)
    check(client.post('/support/queue/'+identifier+'/take',headers=staff,json={}).status_code==200)
    owner = issue(support_system,roles=())
    check(client.delete('/support/cases/'+identifier,headers=owner).json()['data']['status']=='cancelled')
    staff = issue(support_system,OTHER_ACCOUNT)
    case = client.get('/support/queue').json()['data']['items'][0]
    check(case['status']=='cancelled' and case['assigned_account_id']==OTHER_ACCOUNT)
    check(client.post('/support/queue/'+identifier+'/take',headers=staff,json={}).status_code==409)
