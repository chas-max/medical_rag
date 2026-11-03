"""Fresh verified TLS contexts from bounded operator-owned provider trust."""

import re
import ssl
from pathlib import Path

import certifi

_CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----"
)


def provider_ssl_context(ca_file: Path | None) -> ssl.SSLContext:
    """Use only fixed operator PEM trust; never environment or request overrides.

    Re-read explicit trust on every transport construction, so changed/missing
    trust fails closed. No global trust store is modified.
    """
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if ca_file is None:
            # Same public CA bundle as HTTPX verify=True/trust_env=False;
            # OpenSSL default paths would honor SSL_CERT_FILE/SSL_CERT_DIR.
            context.load_verify_locations(cafile=certifi.where())
        else:
            if not isinstance(ca_file, Path):
                raise ValueError()
            with ca_file.open("rb") as stream:
                pem = stream.read(65537)
            if (
                not pem
                or len(pem) > 65536
                or not _CERTIFICATE.search(pem)
                or _CERTIFICATE.sub(b"", pem).strip()
            ):
                raise ValueError()
            context.load_verify_locations(cadata=pem.decode("ascii"))
        return context
    except Exception:  # noqa: BLE001 -- file/crypto errors can contain private paths
        raise ValueError("Invalid trusted provider TLS configuration") from None
