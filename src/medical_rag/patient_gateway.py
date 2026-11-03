"""Strict operator configuration for the separate verified-TLS patient gateway.

This module has no application, dotenv, network or deployment dependencies.
Rendering does not install certificates, deploy the app or grant identity.
"""

import argparse
import ipaddress
import re
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

MAX_CONFIGURATION_BYTES = 8192
_FIELDS = frozenset({"public_origin", "upstream_url", "upstream_tls_name",
                     "tls_certificate", "tls_key", "upstream_ca"})
_DNS = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_PATH = re.compile(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\Z")


class GatewayConfigurationError(Exception):
    """Static diagnostic; never retain received configuration or file paths."""

    def __str__(self) -> str:
        return "Patient gateway configuration failed."


@dataclass(frozen=True, slots=True, repr=False)
class GatewayConfiguration:
    """Explicit public HTTP authority and independent upstream TLS identity."""

    public_origin: str
    upstream_url: str
    upstream_tls_name: str
    tls_certificate: str
    tls_key: str
    upstream_ca: str


def _dns(name: str) -> bool:
    return (len(name) <= 253 and not re.fullmatch(r"[0-9.]+", name)
            and all(_DNS.fullmatch(label) for label in name.split(".")))


def _url(value: str, *, upstream: bool) -> None:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host:
        raise GatewayConfigurationError()
    try:
        address = ipaddress.ip_address(host)
        authority = f"[{address}]" if address.version == 6 else str(address)
    except ValueError:
        if not _dns(host):
            raise GatewayConfigurationError() from None
        authority = host
    port = parsed.port
    if port is not None:
        if not 1 <= port <= 65535 or (not upstream and port == 443):
            raise GatewayConfigurationError()
        authority += f":{port}"
    elif upstream:
        raise GatewayConfigurationError()
    # Exact reconstruction rejects userinfo, Unicode, path, empty ?/#, leading
    # port zeroes, uppercase DNS, alternate IP spellings and scoped IPv6.
    if value != "https://" + authority or "%" in authority:
        raise GatewayConfigurationError()


def _validate(fields: dict) -> GatewayConfiguration:
    if set(fields) != _FIELDS:
        raise GatewayConfigurationError()
    for value in fields.values():
        if (type(value) is not str or not 0 < len(value) <= 1024
                or not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 or c in "\"';{}$\\" for c in value)):
            raise GatewayConfigurationError()
    _url(fields["public_origin"], upstream=False)
    _url(fields["upstream_url"], upstream=True)
    if not _dns(fields["upstream_tls_name"]):
        raise GatewayConfigurationError()
    for key in ("tls_certificate", "tls_key", "upstream_ca"):
        value = fields[key]
        if not _PATH.fullmatch(value) or any(part in {".", ".."} for part in value.split("/")):
            raise GatewayConfigurationError()
    return GatewayConfiguration(**fields)


def load_configuration(data: bytes) -> GatewayConfiguration:
    """Parse bounded TOML bytes, without filesystem or environment access."""
    try:
        if type(data) is not bytes or not 0 < len(data) <= MAX_CONFIGURATION_BYTES:
            raise GatewayConfigurationError()
        return _validate(tomllib.loads(data.decode("utf-8")))
    except (ValueError, TypeError, UnicodeError):
        raise GatewayConfigurationError() from None


def render_nginx(configuration: GatewayConfiguration) -> str:
    """Render fixed directives only, revalidating even directly built objects."""
    try:
        if type(configuration) is not GatewayConfiguration:
            raise GatewayConfigurationError()
        c = _validate(asdict(configuration))
    except (TypeError, ValueError):
        raise GatewayConfigurationError() from None
    authority = c.public_origin.removeprefix("https://")
    return f'''# Generated dedicated patient gateway. Operator review required.
worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/null crit;
events {{ worker_connections 256; }}
http {{
    access_log off;
    server_tokens off;
    default_type application/json;
    client_body_temp_path /tmp/client-body;
    proxy_temp_path /tmp/proxy;
    fastcgi_temp_path /tmp/fastcgi;
    uwsgi_temp_path /tmp/uwsgi;
    scgi_temp_path /tmp/scgi;
    client_max_body_size 100000;
    client_body_buffer_size 128k;
    client_body_timeout 15s;
    client_header_timeout 10s;
    keepalive_timeout 5s;
    send_timeout 30s;
    large_client_header_buffers 2 8k;
    limit_req_zone $binary_remote_addr zone=patient_dev_ip:1m rate=5r/s;
    limit_conn_zone $binary_remote_addr zone=patient_dev_conn:1m;
    server {{
        listen 8443 ssl default_server;
        server_name _;
        ssl_certificate {c.tls_certificate};
        ssl_certificate_key {c.tls_key};
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_session_tickets off;
        if ($http_host != "{authority}") {{ return 400; }}
        limit_req zone=patient_dev_ip burst=20 nodelay;
        limit_req_status 429;
        limit_conn patient_dev_conn 32;
        limit_conn_status 429;
        add_header X-Gateway-Request-ID $request_id always;
        add_header Cache-Control no-store always;
        error_page 400 405 408 414 431 494 495 496 497 = @invalid;
        error_page 404 = @missing;
        error_page 413 = @body_limit;
        error_page 429 = @rate_limit;
        error_page 500 502 503 504 = @unavailable;
        location @invalid {{ return 400 '{{"success":false,"data":null,"error":{{"code":"GATEWAY_INVALID_REQUEST","message":"Invalid request"}}}}'; }}
        location @missing {{ return 404 '{{"success":false,"data":null,"error":{{"code":"GATEWAY_NOT_FOUND","message":"Route unavailable"}}}}'; }}
        location @body_limit {{ return 413 '{{"success":false,"data":null,"error":{{"code":"GATEWAY_BODY_LIMIT","message":"Request too large"}}}}'; }}
        location @rate_limit {{ return 429 '{{"success":false,"data":null,"error":{{"code":"GATEWAY_RATE_LIMIT","message":"Development admission limit"}}}}'; }}
        location @unavailable {{ return 502 '{{"success":false,"data":null,"error":{{"code":"GATEWAY_UNAVAILABLE","message":"Service unavailable"}}}}'; }}
        location ~ ^/(auth/(login|callback|session|logout|backchannel-logout)|me(/consents/health)?|conversations(/[0-9a-fA-F-]+(/turns)?)?|audit|support/(cases(/[0-9a-fA-F-]+)?|queue(/[0-9a-fA-F-]+/(take|resolve))?)|patient\\.html|assets/.+)?$ {{
            proxy_pass {c.upstream_url};
            proxy_ssl_verify on;
            proxy_ssl_verify_depth 3;
            proxy_ssl_trusted_certificate {c.upstream_ca};
            proxy_ssl_server_name on;
            proxy_ssl_name {c.upstream_tls_name};
            proxy_ssl_protocols TLSv1.2 TLSv1.3;
            proxy_next_upstream off;
            proxy_connect_timeout 5s;
            proxy_send_timeout 30s;
            proxy_read_timeout 180s;
            proxy_http_version 1.1;
            proxy_pass_request_headers off;
            proxy_set_header Host {authority};
            proxy_set_header Connection "";
            proxy_set_header Cookie $http_cookie;
            proxy_set_header Origin $http_origin;
            proxy_set_header Content-Type $http_content_type;
            proxy_set_header Accept $http_accept;
            proxy_set_header Accept-Language $http_accept_language;
            proxy_set_header X-CSRF-Token $http_x_csrf_token;
            proxy_hide_header X-Gateway-Request-ID;
            proxy_ignore_headers X-Accel-Redirect X-Accel-Buffering X-Accel-Limit-Rate;
            proxy_buffering off;
            proxy_request_buffering on;
            proxy_cache off;
            proxy_max_temp_file_size 0;
            proxy_intercept_errors off;
            proxy_redirect off;
        }}
        location / {{ return 404; }}
    }}
}}
'''


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise GatewayConfigurationError()


def main(argv: list[str] | None = None) -> int:
    """Read explicit operator file; exclusively create output; never deploy."""
    try:
        parser = _Parser(description="Render the dedicated patient TLS gateway")
        parser.add_argument("--config", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args(argv)
        with args.config.open("rb") as stream:
            rendered = render_nginx(load_configuration(stream.read(MAX_CONFIGURATION_BYTES + 1)))
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
        print("Patient gateway configuration written.")
        return 0
    except Exception:  # noqa: BLE001 -- no input, paths or OS diagnostics cross CLI boundary
        print(str(GatewayConfigurationError()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
