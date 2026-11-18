"""Dedicated same-origin patient portal, direct TLS only; no legacy routes."""

import argparse
import ipaddress
import logging
import sys
import warnings
from pathlib import Path

import psycopg
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from loguru import logger
from starlette.staticfiles import StaticFiles

# Authlib currently emits a known httpx migration notice at import, including
# a local package path. Suppress only that notice at the standalone launcher
# boundary, not runtime warnings, exceptions or arbitrary dependency messages.
with warnings.catch_warnings():
    # Authlib adds an always-filter when importing this category; load it
    # before installing the more-specific launcher-only filter.
    from authlib.deprecate import AuthlibDeprecationWarning

    warnings.filterwarnings(
        "ignore",
        message=r"The httpx module is deprecated; please use httpx2 instead\.",
        category=AuthlibDeprecationWarning,
    )
    from .oidc_login import OIDCLogin
from .logging_setup import configure_logging, flush_logging, silence_logging
from .patient_api import create_patient_app
from .security_config import PatientConfiguration, StartupError, load_configuration

# Every required column from migrations 1-7 is resolved and SELECT-checked.
_COLUMNS = {
    "tenants": "tenant_id active session_version",
    "identity_accounts": "tenant_id account_id issuer subject active session_version",
    "identity_roles": "tenant_id account_id role",
    "identity_bindings": "tenant_id account_id patient_id verified",
    "identity_sessions": "token_hash csrf_hash tenant_id account_id mfa_verified account_version tenant_version created_at last_seen_at expires_at revoked authenticated_at provider_sid_hash",
    "oidc_login_transactions": "tenant_id state_hash browser_hash tenant_version encrypted_context created_at expires_at",
    "patient_subjects": "tenant_id patient_id age_classification identity_verified",
    "patient_consent_events": "event_id tenant_id patient_id grantor_account_id purpose notice_version granted recorded_at expires_at event_order",
    "conversations": "tenant_id patient_id conversation_id title created_at dialogue_state dialogue_revision context_version",
    "conversation_turns": "tenant_id patient_id conversation_id turn_id content role citations ordinal created_at answer_type",
    "security_audit_events": "event_id tenant_id operator_id action resource_type outcome reason_code recorded_at event_order request_id",
    "provider_logout_events": "event_id tenant_id issuer jti_hash subject_hash sid_hash issued_at expires_at recorded_at",
    "support_cases": "tenant_id case_id requester_account_id category status assigned_account_id resolution_code created_at updated_at",
}


def _connect(config: PatientConfiguration):
    # Explicit options override DSN values; ignore ambient PG service defaults
    # for required identity/host fields by requiring them in preflight below.
    info = psycopg.conninfo.conninfo_to_dict(config.database_dsn)
    if (
        not all(info.get(key) for key in ("host", "dbname", "user", "password"))
        or "service" in info
    ):
        raise StartupError()
    host = info["host"]
    if "," in host or "hostaddr" in info or host.startswith(("/", "\\")):
        raise StartupError()
    try:
        address = ipaddress.ip_address(host)
        loopback = address.is_loopback
        # In particular PGHOSTADDR must not redirect a plaintext loopback
        # development connection to a remote endpoint.
        info["hostaddr"] = str(address)
    except ValueError:
        loopback = False
    if not loopback and info.get("sslmode") != "verify-full":
        raise StartupError()
    # Explicit endpoint/auth/options prevent libpq PGHOST/PGUSER/PGPASSWORD/
    # PGPORT/PGOPTIONS from silently selecting a different authority.
    info.setdefault("port", "5432")
    info.setdefault("sslmode", "disable" if loopback else "verify-full")
    info["gssencmode"] = "disable"
    info.update(
        connect_timeout="3",
        options="-c search_path=pg_catalog -c lock_timeout=3000 -c statement_timeout=5000",
    )
    return psycopg.connect(**info, autocommit=False)


def preflight(config: PatientConfiguration) -> None:
    """Partial live privilege/schema/primary check, NOT a full security audit."""
    try:
        with _connect(config) as db:
            if db.execute(
                "SELECT medical_rag.runtime_role_basic_check() AND NOT pg_catalog.pg_is_in_recovery()"
            ).fetchone() != (True,):
                raise StartupError()
            for table, columns in _COLUMNS.items():
                query = psycopg.sql.SQL("SELECT {} FROM medical_rag.{} LIMIT 0").format(
                    psycopg.sql.SQL(",").join(
                        map(psycopg.sql.Identifier, columns.split())
                    ),
                    psycopg.sql.Identifier(table),
                )
                db.execute(query)
                # Catch grants via membership/PUBLIC, including column grants
                # which has_table_privilege alone does not detect.
                forbidden = (
                    ["INSERT", "UPDATE", "DELETE"]
                    if table
                    in {
                        "tenants",
                        "identity_accounts",
                        "identity_roles",
                        "identity_bindings",
                        "patient_subjects",
                    }
                    else ["UPDATE", "DELETE"]
                    if table
                    in {"patient_consent_events", "conversations", "conversation_turns", "security_audit_events", "provider_logout_events"}
                    else []
                )
                if table == "support_cases":
                    forbidden += ["DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]
                    for privilege, allowed in {
                        "INSERT": {"tenant_id", "case_id", "requester_account_id", "category"},
                        "UPDATE": {"status", "assigned_account_id", "resolution_code"},
                    }.items():
                        for column in columns.split():
                            if column not in allowed and db.execute(
                                "SELECT has_column_privilege(current_user,%s,%s,%s)",
                                ("medical_rag." + table, column, privilege),
                            ).fetchone() == (True,):
                                raise StartupError()
                if table in {"security_audit_events", "provider_logout_events"}:
                    forbidden += ["TRUNCATE", "REFERENCES", "TRIGGER"]
                    # Defaults are exclusively server-owned, not insertable by
                    # runtime even through inherited/PUBLIC column grants.
                    for column in (("event_id", "recorded_at", "event_order") if table == "security_audit_events"
                                   else ("event_id", "recorded_at")):
                        if db.execute(
                            "SELECT has_column_privilege(current_user,%s,%s,'INSERT')",
                            ("medical_rag." + table, column),
                        ).fetchone() == (True,):
                            raise StartupError()
                for privilege in forbidden:
                    if privilege == "UPDATE" and table == "patient_subjects":
                        bad = db.execute(
                            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped AND attname<>'patient_id' AND has_column_privilege(current_user,attrelid,attname,'UPDATE'))",
                            ("medical_rag." + table,),
                        ).fetchone()[0]
                    elif privilege == "UPDATE" and table == "conversations":
                        # 0009 makes the dialogue columns the only mutable part of
                        # the row, so the check scans columns instead of dropping it:
                        # any other column UPDATE (or a table-level grant) fails.
                        bad = db.execute(
                            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute "
                            "WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped "
                            "AND attname<>ALL(%s) AND has_column_privilege(current_user,attrelid,attname,'UPDATE'))",
                            ("medical_rag.conversations",
                             ["dialogue_state", "dialogue_revision", "context_version"]),
                        ).fetchone()[0]
                    elif privilege in {"INSERT", "UPDATE", "REFERENCES"}:
                        bad = db.execute(
                            "SELECT has_any_column_privilege(current_user,%s,%s)",
                            ("medical_rag." + table, privilege),
                        ).fetchone()[0]
                    else:
                        bad = db.execute(
                            "SELECT has_table_privilege(current_user,%s,%s)",
                            ("medical_rag." + table, privilege),
                        ).fetchone()[0]
                    if bad:
                        raise StartupError()
            required = {
                "support_cases": {
                    "INSERT": "tenant_id case_id requester_account_id category",
                    "UPDATE": "status assigned_account_id resolution_code",
                },
                "identity_sessions": {
                    "INSERT": "token_hash csrf_hash tenant_id account_id mfa_verified account_version tenant_version expires_at authenticated_at provider_sid_hash",
                    "UPDATE": "last_seen_at revoked",
                },
                "oidc_login_transactions": {
                    "INSERT": "tenant_id state_hash browser_hash tenant_version encrypted_context expires_at",
                    "UPDATE": "state_hash",
                },
                "patient_subjects": {"UPDATE": "patient_id"},
                "patient_consent_events": {
                    "INSERT": "event_id tenant_id patient_id grantor_account_id purpose notice_version granted expires_at"
                },
                "conversations": {
                    "INSERT": "tenant_id patient_id conversation_id title",
                    "UPDATE": "dialogue_state dialogue_revision context_version",
                },
                "conversation_turns": {
                    "INSERT": "tenant_id patient_id conversation_id turn_id content role citations answer_type"
                },
                "security_audit_events": {
                    "INSERT": "tenant_id operator_id action resource_type outcome reason_code request_id"
                },
                "provider_logout_events": {
                    "INSERT": "tenant_id issuer jti_hash subject_hash sid_hash issued_at expires_at"
                },
            }
            for table, permissions in required.items():
                for privilege, columns in permissions.items():
                    for column in columns.split():
                        if db.execute(
                            "SELECT has_column_privilege(current_user,%s,%s,%s)",
                            ("medical_rag." + table, column, privilege),
                        ).fetchone() != (True,):
                            raise StartupError()
            if db.execute(
                "SELECT has_table_privilege(current_user,'medical_rag.oidc_login_transactions','DELETE')"
            ).fetchone() != (True,):
                raise StartupError()
            for table in (
                "patient_subjects",
                "patient_consent_events",
                "conversations",
                "conversation_turns",
                "security_audit_events",
                "provider_logout_events",
                "support_cases",
            ):
                if db.execute(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_catalog.pg_class WHERE oid=%s::regclass",
                    ("medical_rag." + table,),
                ).fetchone() != (True,):
                    raise StartupError()
            if db.execute(
                "SELECT active FROM medical_rag.tenants WHERE tenant_id=%s",
                (config.tenant_id,),
            ).fetchone() != (True,):
                raise StartupError()
    except Exception:  # noqa: BLE001 -- SQL/DSN errors must never cross startup boundary
        raise StartupError() from None


def _application(config: PatientConfiguration) -> FastAPI:
    try:
        # Opt-in and additive only: a disabled [logging] section installs nothing,
        # so the portal keeps its silent stderr and writes no file.
        configure_logging("patient", patient=config.logging_config)
        html = config.frontend_dist / "patient.html"
        assets = config.frontend_dist / "assets"
        if not html.is_file() or not assets.is_dir() or not any(assets.iterdir()):
            raise StartupError()
        preflight(config)
        login = OIDCLogin(
            config.login,
            lambda: _connect(config),
            tenant_id=config.tenant_id,
            transaction_encryption_key=config.transaction_key,
        )
        app = create_patient_app(login, config.notice, patient_html=True, chat=config.chat,
                                 dialogue_state_enabled=config.dialogue_state_enabled,
                                 chat_streaming=config.chat_streaming)

        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(html, media_type="text/html")

        app.mount(
            "/assets",
            StaticFiles(directory=assets, html=False, follow_symlink=False),
            name="patient-assets",
        )
        return app
    except Exception:  # noqa: BLE001 -- only static startup errors may be public
        raise StartupError() from None


def load_patient_application(config_path: Path) -> FastAPI:
    """Load only trusted file configuration, preflight, and dedicated build."""
    return _application(load_configuration(config_path))


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise StartupError()


def main(argv: list[str] | None = None) -> int:
    """Run direct TLS without request logging or proxy-header trust."""
    try:
        parser = _Parser(description="机构患者门户（直接 TLS）")
        parser.add_argument("--config", required=True, type=Path)
        args = parser.parse_args(argv)
        # The portal stays silent unless the operator opted in: drop the default
        # stderr sink before any record can be emitted.
        silence_logging()
        config = load_configuration(args.config)
        configure_logging("patient", patient=config.logging_config)
        app = _application(config)
        if not ipaddress.ip_address(config.listen_host).is_loopback:
            logger.warning("患者服务绑定非回环地址，机构须另行完成网络边界审查")
            print(
                "警告：患者服务绑定非回环地址；机构须另行完成网络边界审查。",
                file=sys.stderr,
            )
        # Avoid upstream libraries printing startup paths or ASGI traces. The
        # application converts request failures to static responses as well.
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            upstream = logging.getLogger(name)
            upstream.handlers = [logging.NullHandler()]
            upstream.propagate = False
        logger.info("患者服务已启动")
        uvicorn.run(
            app,
            host=config.listen_host,
            port=config.listen_port,
            ssl_certfile=str(config.tls_cert_file),
            ssl_keyfile=str(config.tls_key_file),
            access_log=False,
            proxy_headers=False,
            server_header=False,
            log_config=None,
            log_level="critical",
            limit_concurrency=100,
            timeout_keep_alive=5,
        )
        return 0
    except SystemExit as error:
        if error.code == 0:
            return 0
        logger.error("患者服务启动失败")
        print(str(StartupError()), file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 -- prevent server/path/secret tracebacks
        logger.error("患者服务启动失败")
        print(str(StartupError()), file=sys.stderr)
        return 1
    finally:
        flush_logging()


if __name__ == "__main__":
    raise SystemExit(main())
