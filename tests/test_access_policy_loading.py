"""Policy component must exist before business endpoints can use it."""

import importlib.util


def test_access_policy_component_exists():
    assert importlib.util.find_spec("medical_rag.access_policy") is not None
