"""Negative rewritten diagnostics for new support tests use synthetic canaries."""

import ast
from pathlib import Path

import pytest
from _pytest.assertion.rewrite import rewrite_asserts


def test_support_fixtures_and_assertions_do_not_expose_credentials():
    tree = ast.parse(Path(__file__).with_name('test_support_cases.py').read_text(encoding='utf-8'))
    scenarios = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith('test_')]
    private_fixtures = all(not {'system','database','auth_system','oidc_database'} &
                           {arg.arg for arg in scenario.args.args} for scenario in scenarios)
    assert private_fixtures, 'Credential-bearing legacy fixtures must be wrapped before new support test arguments'
    assertions = [node for node in ast.walk(tree) if isinstance(node,ast.Assert)]
    private_assertions = all(isinstance(node.test,ast.Name) and isinstance(node.msg,ast.Constant)
                             and isinstance(node.msg.value,str) for node in assertions)
    assert private_assertions, 'Support assertion operands must be boolean locals with static messages'


def test_rewritten_support_boolean_failure_hides_synthetic_private_state():
    source_path = Path(__file__).with_name('test_support_cases.py')
    tree = ast.parse(source_path.read_text(encoding='utf-8'))
    helper = next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='check')
    source = ast.unparse(ast.Module(body=[helper],type_ignores=[])).encode()
    rewritten = ast.parse(source)
    rewrite_asserts(rewritten,source)
    namespace = {'private_session':'PRIVATE_SESSION_CANARY','private_csrf':'PRIVATE_CSRF_CANARY',
                 'private_connection':{'password':'PRIVATE_DB_CANARY'}}
    exec(compile(rewritten,'<support-privacy-check>','exec'),namespace)  # noqa: S102
    try:
        namespace['check'](False)
    except AssertionError:
        diagnostic = str(pytest.ExceptionInfo.from_current().getrepr(style='short',showlocals=False))
    else:
        pytest.fail('Synthetic support check must fail')
    private = not any(canary in diagnostic for canary in ('PRIVATE_SESSION_CANARY','PRIVATE_CSRF_CANARY','PRIVATE_DB_CANARY'))
    assert private, 'Negative support diagnostic exposed synthetic credentials'
    retained = 'Structured support contract failed' in diagnostic and 'assert False' in diagnostic
    assert retained, 'Negative support diagnostic must retain static assertion proof'


def test_failed_disposable_database_start_hides_subprocess_credentials(monkeypatch):
    import subprocess

    import test_oidc_login

    monkeypatch.setenv('RUN_POSTGRES_INTEGRATION','1')
    monkeypatch.setattr(test_oidc_login.shutil,'which',lambda name:'synthetic-docker')
    monkeypatch.setattr(test_oidc_login.secrets,'token_urlsafe',lambda size:'PRIVATE_DB_START_CANARY')
    def failed(command,**options):
        if options.get('check'):
            raise subprocess.CalledProcessError(1,command,stderr='PRIVATE_PROCESS_STDERR_CANARY')
        return subprocess.CompletedProcess(command,1,stdout='',stderr='PRIVATE_PROCESS_STDERR_CANARY')
    monkeypatch.setattr(test_oidc_login.subprocess,'run',failed)
    try:
        next(test_oidc_login.database.__wrapped__())
    except (subprocess.CalledProcessError, pytest.fail.Exception):
        diagnostic = str(pytest.ExceptionInfo.from_current().getrepr(style='short',showlocals=False))
    else:
        pytest.fail('Synthetic disposable database start must fail')
    private = not any(canary in diagnostic for canary in ('PRIVATE_DB_START_CANARY','PRIVATE_PROCESS_STDERR_CANARY'))
    assert private, 'Disposable database startup diagnostic exposed synthetic process credentials'
    fixed = 'disposable PostgreSQL could not start' in diagnostic
    assert fixed, 'Disposable database startup failure must retain a static useful diagnosis'
