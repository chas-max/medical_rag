"""One test-local UTC epoch shared by the two logout-verification clocks."""

from datetime import datetime

import jwt.api_jwt

from medical_rag import provider_logout


def freeze_logout_clock(monkeypatch, epoch: int) -> None:
    """Patch only verifier references; the caller owns monkeypatch restoration."""
    class FixedUTC(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(epoch, tz=tz)

    monkeypatch.setattr(jwt.api_jwt, "datetime", FixedUTC)
    monkeypatch.setattr(provider_logout, "time", lambda: epoch)
