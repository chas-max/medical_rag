"""Dedicated verified TLS boundary; actual application proof is not OIDC login."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VALID = '''public_origin = "https://patient.example.org:9443"
upstream_url = "https://patient-app:8443"
upstream_tls_name = "patient.internal.example.org"
tls_certificate = "/run/tls/gateway.pem"
tls_key = "/run/tls/gateway.key"
upstream_ca = "/run/tls/upstream-ca.pem"
'''


def gateway_module():
    from medical_rag import patient_gateway

    return patient_gateway


def test_renderer_verified_tls_port_header_routes_and_bounds():
    module = gateway_module()
    config = module.load_configuration(VALID.encode())
    rendered = module.render_nginx(config)
    for directive in (
        "proxy_ssl_verify on;", "proxy_ssl_server_name on;",
        "proxy_ssl_name patient.internal.example.org;",
        "proxy_ssl_trusted_certificate /run/tls/upstream-ca.pem;",
        "ssl_protocols TLSv1.2 TLSv1.3;", "proxy_ssl_protocols TLSv1.2 TLSv1.3;",
        "proxy_next_upstream off;", "server_tokens off;",
        "fastcgi_temp_path /tmp/fastcgi;", "uwsgi_temp_path /tmp/uwsgi;",
        "scgi_temp_path /tmp/scgi;",
        "proxy_set_header Host patient.example.org:9443;",
        "proxy_pass https://patient-app:8443;", "proxy_pass_request_headers off;",
        "client_max_body_size 100000;", "client_body_buffer_size 128k;",
        "proxy_buffering off;", "proxy_cache off;", "proxy_read_timeout 180s;",
        "proxy_http_version 1.1;", 'proxy_set_header Connection "";',
        "rate=5r/s", "burst=20", "limit_req_status 429;", "access_log off;",
        "proxy_hide_header X-Gateway-Request-ID;", "X-Gateway-Request-ID $request_id always;",
        "proxy_intercept_errors off;", "proxy_redirect off;",
    ):
        assert directive in rendered
    assert "X-Request-ID $request_id" not in rendered
    assert "Authorization $" not in rendered
    assert "X-Forwarded" not in rendered
    assert "set_real_ip_from" not in rendered
    assert "BEGIN " not in rendered and "PRIVATE KEY" not in rendered


@pytest.mark.parametrize("origin", [
    "https://example.org", "https://127.0.0.1:8443", "https://[::1]:8443",
])
def test_canonical_public_origins(origin):
    module = gateway_module()
    data = VALID.replace("https://patient.example.org:9443", origin)
    assert module.load_configuration(data.encode()).public_origin == origin


@pytest.mark.parametrize("key,value", [
    ("public_origin", "http://example.org"), ("public_origin", "https://EXAMPLE.org"),
    ("public_origin", "https://example.org:443"), ("public_origin", "https://example.org:0"),
    ("public_origin", "https://example.org:65536"), ("public_origin", "https://example.org:0443"),
    ("public_origin", "https://a@b"), ("public_origin", "https://example.org/"),
    ("public_origin", "https://example.org?"), ("public_origin", "https://example.org#"),
    ("public_origin", "https://*.example.org"), ("public_origin", "https://é.example"),
    ("public_origin", "https://127.1"), ("public_origin", "https://01.2.3.4"),
    ("public_origin", "https://example.org."), ("public_origin", "https://[fe80::1%25eth0]:4"),
    ("upstream_url", "https://app"), ("upstream_url", "http://app:443"),
    ("upstream_url", "https://app:443/path"), ("upstream_url", "https://$host:443"),
    ("upstream_tls_name", "127.0.0.1"), ("upstream_tls_name", "*.example.org"),
    ("tls_key", "/run/../secret"), ("tls_key", "relative/key"),
    ("tls_key", "/run/key;"), ("tls_key", "/run/key$"), ("tls_key", "/run/key{a}"),
    ("tls_key", "/run/key name"), ("tls_key", "/run//key"),
    ("tls_key", "/run/key\nsecret"), ("tls_key", '/run/key"secret'),
])
def test_rejects_ambiguous_or_injected_values(key, value):
    module = gateway_module()
    import tomllib

    fields = tomllib.loads(VALID)
    fields[key] = value
    data = "\n".join(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in fields.items())
    with pytest.raises(module.GatewayConfigurationError, match="^Patient gateway configuration failed\\.$"):
        module.load_configuration(data.encode())


@pytest.mark.parametrize("data", [
    b"", b"x" * 8193, b"\xff", b"[]", VALID.encode() + b'unknown = "secret"',
    VALID.encode() + b'public_origin = "secret"', VALID.replace('"/run/tls/gateway.key"', "42").encode(),
    VALID.replace('tls_key = "/run/tls/gateway.key"\n', "").encode(),
])
def test_strict_bounded_toml(data):
    module = gateway_module()
    with pytest.raises(module.GatewayConfigurationError):
        module.load_configuration(data)


def test_renderer_revalidates_constructed_configuration():
    module = gateway_module()
    from dataclasses import replace

    config = module.load_configuration(VALID.encode())
    with pytest.raises(module.GatewayConfigurationError):
        module.render_nginx(replace(config, upstream_url="http://unsafe:80"))


def test_cli_exclusive_output_and_static_private_failures(tmp_path, capsys):
    module = gateway_module()
    config, output = tmp_path / "operator-secret.toml", tmp_path / "nginx.conf"
    config.write_text(VALID, encoding="utf-8")
    assert module.main(["--config", str(config), "--output", str(output)]) == 0
    original = output.read_bytes()
    assert module.main(["--config", str(config), "--output", str(output)]) == 1
    assert output.read_bytes() == original
    assert module.main(["--config", "secret-not-found", "--output", str(output)]) == 1
    assert module.main(["--unrecognized-secret"]) == 1
    capture = capsys.readouterr()
    assert capture.out == "Patient gateway configuration written.\n"
    assert capture.err == "Patient gateway configuration failed.\n" * 3
    config.write_bytes(b"private-invalid-value")
    run = subprocess.run([sys.executable, "-m", "medical_rag.patient_gateway", "--config", str(config),
                          "--output", str(tmp_path / "absent")], capture_output=True, text=True, check=False)
    assert run.returncode == 1
    assert run.stdout == "" and run.stderr == "Patient gateway configuration failed.\n"
    assert not (tmp_path / "absent").exists()


@pytest.mark.integration
def test_synthetic_tls_headers_limits_and_streaming(tmp_path):
    if os.environ.get("RUN_PATIENT_GATEWAY_INTEGRATION") != "1":
        pytest.skip("select the disposable patient gateway socket suite explicitly")
    from patient_gateway_runtime import synthetic_proof

    synthetic_proof(tmp_path)


@pytest.mark.integration
def test_dedicated_compose_without_dotenv(tmp_path):
    if os.environ.get("RUN_PATIENT_GATEWAY_INTEGRATION") != "1":
        pytest.skip("select the disposable patient gateway socket suite explicitly")
    from patient_gateway_runtime import compose_proof

    compose_proof(tmp_path)


@pytest.mark.integration
def test_actual_isolated_application_through_verified_tls(tmp_path):
    if os.environ.get("RUN_PATIENT_GATEWAY_INTEGRATION") != "1":
        pytest.skip("select the disposable patient gateway socket suite explicitly")
    assert importlib.util.find_spec("medical_rag.patient_gateway") is not None
    from patient_gateway_runtime import actual_application_proof

    actual_application_proof(tmp_path)


def test_renderer_routes_patient_chat_and_its_stream_only():
    module = gateway_module()
    rendered = module.render_nginx(module.load_configuration(VALID.encode()))
    location = next(line.strip() for line in rendered.splitlines()
                    if line.strip().startswith("location ~ "))

    # Both the synchronous and the streaming patient answer must be reachable;
    # everything the patient portal does not serve stays unroutable.
    assert "|chat(/stream)?|" in location
    for closed in ("/ui", "/api", "/settings", "/documents", "/memory"):
        assert closed not in location
