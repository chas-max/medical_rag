"""Actual provider rotation; app boundary TestClient, provider boundary TLS sockets."""

# ruff: noqa: F401,F811 -- imported fixture functions
import time
from dataclasses import replace
from urllib.parse import parse_qs

import jwt
import psycopg
import pytest
from keycloak_runtime import APP_ORIGIN, CLIENT_ID, identity_runtime
from test_keycloak_identity import (
    application,
    authenticate,
    no_provider_url_logging,
)

from medical_rag.oidc_identity import InvalidIdentity
from medical_rag.oidc_login import OIDCLogin


@pytest.fixture
def provider(tmp_path):
    with identity_runtime(tmp_path) as runtime:
        yield runtime


def captured_logout(provider, browser):
    from keycloak_forms import provider_logout

    previous = browser.get(provider.collector_url).content
    provider_logout(browser, provider.issuer, CLIENT_ID, APP_ORIGIN + "/")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = browser.get(provider.collector_url)
        if response.content and response.content != previous:
            return response.content
        time.sleep(0.1)
    pytest.fail("Actual provider rotation logout capture missing")


def test_actual_operator_rotation_overlap_and_retired_token(application, provider):
    from keycloak_operator import Operator, upstream_token

    _client, login, tenant, _ = application
    host_import_absent = not (provider.ca_file.parent / "realm.json").exists()
    assert host_import_absent, "Fixture host import artifact must be absent"
    old_keys = dict(provider.verification_keys)
    old_token, nonce = upstream_token(provider, "patient2")
    old_identity_verified = bool(login._verifier.verify(old_token, expected_nonce=nonce))
    assert old_identity_verified, "Original provider identity must verify"
    old_kid = jwt.get_unverified_header(old_token)["kid"]
    with Operator(provider) as operator, operator.second_signing_key() as component:
        overlap = operator.public_keys()
        overlap_is_approved = len(overlap) == 2 and old_kid in overlap
        assert overlap_is_approved, "Provider overlap must contain the original and second key"
        config = replace(login.config, public_key=None, verification_keys=overlap)
        ring_login = OIDCLogin(
            config, lambda: psycopg.connect(**provider.database),
            tenant_id=tenant, transaction_encryption_key=b"r" * 32,
        )
        # The actual HTTP app is reconstructed with the overlap set.
        from fastapi.testclient import TestClient

        from medical_rag.patient_api import create_patient_app
        from medical_rag.patient_service import HealthNotice

        notice = HealthNotice("fictional-v1", "Fictional overlap verification only.", 3600)
        with TestClient(create_patient_app(ring_login, notice), base_url=APP_ORIGIN, headers={}) as ring_client:
            with provider.browser() as browser:
                old_login_succeeded = authenticate(ring_client, browser, provider, "patient1").status_code == 303
                assert old_login_succeeded, "Original-key provider login must succeed"
                old_body = captured_logout(provider, browser)
            old_logout = parse_qs(old_body.decode())["logout_token"][0]
            old_logout_matches = jwt.get_unverified_header(old_logout)["kid"] == old_kid
            assert old_logout_matches, "Original provider logout must use the original key"
            old_logout_delivered = ring_client.post(
                "/auth/backchannel-logout", content=old_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ).status_code == 200
            assert old_logout_delivered, "Original provider logout must be accepted"
            old_session_revoked = ring_client.get("/me").status_code == 401
            assert old_session_revoked, "Original provider logout must revoke the session"
            operator.activate(component)
            while int(time.time()) <= jwt.decode(old_logout, options={"verify_signature": False})["iat"]:
                time.sleep(0.1)
            with provider.browser() as browser:
                new_login_succeeded = authenticate(ring_client, browser, provider, "patient1").status_code == 303
                assert new_login_succeeded, "Current-key provider login must succeed"
                new_session_authenticated = ring_client.get("/me").status_code == 200
                assert new_session_authenticated, "Current-key session must authenticate"
                new_body = captured_logout(provider, browser)
            new_logout = parse_qs(new_body.decode())["logout_token"][0]
            new_kid = jwt.get_unverified_header(new_logout)["kid"]
            new_logout_is_approved = new_kid != old_kid and new_kid in overlap
            assert new_logout_is_approved, "Current provider logout must use the second approved key"
            new_logout_delivered = ring_client.post(
                "/auth/backchannel-logout", content=new_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ).status_code == 200
            assert new_logout_delivered, "Current provider logout must be accepted"
            new_session_revoked = ring_client.get("/me").status_code == 401
            assert new_session_revoked, "Current provider logout must revoke the session"
            # Same actual unexpired token and correct original nonce passes
            # before removal; this is verifier-level, not a public bearer API.
            overlap_identity_verified = bool(ring_login._verifier.verify(old_token, expected_nonce=nonce))
            assert overlap_identity_verified, "Original identity must verify immediately before key removal"
            current_config = replace(config, verification_keys={new_kid: overlap[new_kid]})
            current_login = OIDCLogin(
                current_config, lambda: psycopg.connect(**provider.database),
                tenant_id=tenant, transaction_encryption_key=b"s" * 32,
            )
            with pytest.raises(InvalidIdentity):
                current_login._verifier.verify(old_token, expected_nonce=nonce)
            with TestClient(create_patient_app(current_login, notice), base_url=APP_ORIGIN, headers={}) as current_client:
                while int(time.time()) <= jwt.decode(new_logout, options={"verify_signature": False})["iat"]:
                    time.sleep(0.1)
                with provider.browser() as browser:
                    current_login_succeeded = authenticate(current_client, browser, provider, "patient1").status_code == 303
                    assert current_login_succeeded, "Current-only provider login must succeed"
                current_session_authenticated = current_client.get("/me").status_code == 200
                assert current_session_authenticated, "Current-only session must authenticate"
            original_keys_match = set(old_keys) == {old_kid}
            assert original_keys_match, "Original provider trust must contain only the original key"
