"""Memory-only fictional MFA enrollment composing the same public fragment."""

import base64
import json
import secrets
import time
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.twofactor.totp import TOTP

ACR = "urn:medical-rag:reference:otp"
POLICY = Path(__file__).resolve().parents[1] / "deploy/identity/keycloak-mfa-policy.json"


def compose_mfa(imported: dict, users: dict) -> None:
    """Inject public policy and process-private fictional credentials pre-import."""
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    imported["roles"]["realm"].extend(policy.pop("roles")["realm"])
    imported.update(policy)
    for name in ("staff", "staff_norole", "staff_unenrolled"):
        user = {"id": str(uuid4()), "password": secrets.token_urlsafe(24)}
        credentials = [{"type": "password", "value": user["password"], "temporary": False}]
        if name != "staff_unenrolled":
            seed = secrets.token_bytes(20)
            user["otp_seed"] = seed
            credentials.append({
                "type": "otp", "userLabel": "fictional-reference-otp",
                "secretData": json.dumps({"value": base64.b32encode(seed).decode().rstrip("=")}),
                "credentialData": json.dumps({"subType": "totp", "digits": 6, "counter": 0,
                                              "period": 30, "algorithm": "HmacSHA1", "secretEncoding": "BASE32"}),
            })
        users[name] = user
        imported["users"].append({
            "id": user["id"], "username": name, "enabled": True,
            "firstName": "Fictional", "lastName": "Staff", "email": name + "@invalid.example",
            "emailVerified": True, "requiredActions": [], "realmRoles": ["medical-staff-mfa"],
            "credentials": credentials,
        })


def otp_code(user: dict) -> str:
    """Use cryptography, not a custom OTP implementation; bounded rollover wait."""
    now = time.time()
    remaining = 30 - now % 30
    if remaining < 3 or user.get("_last_otp_step") == int(now // 30):
        time.sleep(remaining + 0.05)
        now = time.time()
    if user.get("_last_otp_step", -1) >= int(now // 30):
        raise RuntimeError("Fixture OTP clock did not advance")
    user["_last_otp_step"] = int(now // 30)
    return TOTP(user["otp_seed"], 6, hashes.SHA1(), 30).generate(now).decode("ascii")


def wrong_otp_code(user: dict) -> str:
    """Six digits guaranteed different from adjacent default-window candidates."""
    now = time.time()
    totp = TOTP(user["otp_seed"], 6, hashes.SHA1(), 30)
    candidates = {totp.generate(now + offset * 30).decode("ascii") for offset in range(-2, 3)}
    return next(f"{candidate:06d}" for candidate in range(6) if f"{candidate:06d}" not in candidates)


