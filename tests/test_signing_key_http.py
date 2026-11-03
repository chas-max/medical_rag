"""Ring mode HTTP boundaries with actual least-privilege PostgreSQL storage."""

# ruff: noqa: F401,F811 -- imported pytest fixtures
import asyncio
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import httpx
import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from test_patient_vertical import (
    APP_ORIGIN,
    ISSUER,
    TENANT,
    auth_system,
    database,
    grant,
    logged_in,
    oidc_database,
    signing_key,
    start,
)
from test_patient_vertical import system as base_system
from test_provider_logout import claims, post, signed
from test_signing_keys import keys

from medical_rag.oidc_login import OIDCLogin
from medical_rag.patient_api import create_patient_app
from medical_rag.patient_service import HealthNotice

pytestmark = pytest.mark.integration


@pytest.fixture(params=["old", "new"])
def system(base_system, database, keys, request):
    _, original, _, admin = base_system
    runtime, _ = database
    state = {"kid": request.param, "nonce": None, "auth_time": int(time.time()),
             "entered": None, "release": None}
    ring = dict(zip(("old", "new"), (key.public_key() for key in keys), strict=True))
    signing = dict(zip(("old", "new"), keys, strict=True))

    async def response(request):
        if state["entered"] is not None:
            state["entered"].set()
            assert await asyncio.to_thread(state["release"].wait, 2)
        now = int(time.time())
        encoded = jwt.encode({"iss": ISSUER, "aud": "medical-rag", "sub": "subject-1",
                              "sid": "ring-pending", "iat": now, "exp": now + 120,
                              "auth_time": state["auth_time"], "nonce": state["nonce"],
                              "acr": "urn:hospital:mfa"}, signing[state["kid"]],
                             algorithm="RS256", headers={"kid": state["kid"]})
        return httpx.Response(200, json={"access_token": "fictional", "token_type": "Bearer", "id_token": encoded})

    login = OIDCLogin(replace(original.config, public_key=None, verification_keys=ring),
                      lambda: psycopg.connect(**runtime), tenant_id=TENANT,
                      transaction_encryption_key=secrets.token_bytes(32), transport=httpx.MockTransport(response))
    notice = HealthNotice("fictional-v1", "Fictional ring HTTP test notice.", 3600)
    with TestClient(create_patient_app(login, notice), base_url=APP_ORIGIN, headers={}) as client:
        yield client, login, state, admin, signing


def login_http(system):
    client, login, state, admin, _ = system
    old_shape = (client, login, state, admin)
    return logged_in(old_shape)


def logout_token(system, **changes):
    _, _, state, _, signing = system
    return signed(signing[state["kid"]], claims(**changes), kid=state["kid"])


def test_both_keys_http_login_logout_and_replay(system):
    client, login, state, _, _ = system
    headers = login_http(system)
    grant(client, headers)
    assert client.get("/conversations").status_code == 200
    encoded = logout_token(system)
    issued_at = login.provider_logout_verifier.verify(encoded).issued_at
    assert post(client, encoded).status_code == 200
    assert client.get("/me").status_code == 401
    while int(time.time()) <= issued_at:
        time.sleep(0.05)
    state["auth_time"] = int(time.time())
    client.cookies.clear()
    login_http(system)
    assert post(client, encoded).status_code == 200
    assert client.get("/me").status_code == 200


def test_both_keys_http_pending_exchange_fence_and_replay(system):
    client, login, state, admin, _ = system
    state["entered"], state["release"] = Event(), Event()
    _, query = start((client, login, state, admin))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.get, "/auth/callback",
                                 params={"state": query["state"][0], "code": "fictional"}, follow_redirects=False)
        assert state["entered"].wait(1)
        try:
            encoded = logout_token(system, sid="ring-pending")
            issued_at = login.provider_logout_verifier.verify(encoded).issued_at
            assert post(client, encoded).status_code == 200
        finally:
            state["release"].set()
        assert future.result(timeout=3).status_code == 401
    assert client.get("/me").status_code == 401
    state["entered"] = None
    while int(time.time()) <= issued_at:
        time.sleep(0.05)
    state["auth_time"] = int(time.time())
    login_http(system)
    assert post(client, encoded).status_code == 200
    assert client.get("/me").status_code == 200
