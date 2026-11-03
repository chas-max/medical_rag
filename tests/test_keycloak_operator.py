"""Fixture rotation administrative boundaries and diagnostic privacy."""

from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_operator_rejects_unowned_realm_before_network():
    from keycloak_operator import Operator

    with pytest.raises(RuntimeError, match="^Disposable provider operator failed$"):
        Operator(SimpleNamespace(issuer="https://real.example.org/realms/master"))


def test_operator_rejects_preexisting_component_even_if_metadata_looks_owned():
    from keycloak_operator import Operator

    operator = Operator(SimpleNamespace(issuer="https://localhost:8443/realms/fictional-123456abcdef"))
    component = {"id": str(uuid4()), "name": "fixture-rotation-" + uuid4().hex,
                 "parentId": operator.realm, "providerType": "org.keycloak.keys.KeyProvider",
                 "providerId": "rsa-generated"}
    operator._request = lambda *args, **kwargs: component
    with pytest.raises(RuntimeError, match="^Disposable provider operator failed$"):
        operator._owned(component)
