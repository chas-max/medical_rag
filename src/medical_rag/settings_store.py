"""Settings the UI writes at runtime, applied immediately, kept apart from `.env`."""

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from loguru import logger
from pydantic import BaseModel, ValidationError

from .config import Settings
from .types import ApiError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_SECTION = "model"
MODEL_FIELDS = ("model_name", "base_url", "api_key")


def resolve_path(value: str | Path) -> Path:
    """Relative settings paths hang off the project root, not the launch directory."""
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def mask_secret(value: str) -> str:
    """Let the UI recognise a stored secret without ever handing it back."""
    if not value:
        return ""
    return f"••••{value[-4:]}" if len(value) > 8 else "••••"


def _require_http_url(base_url: str) -> None:
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ApiError("MODEL_CONFIG_INVALID_URL",
                       "Base URL 需要是以 http:// 或 https:// 开头的完整地址，请重新填写", 422)


class SettingsStore:
    """One JSON file holding every settings section, replaced atomically."""

    def __init__(self, path: str | Path) -> None:
        self.path = resolve_path(path)

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("设置文件无法读取，按未配置处理")
            return {}
        return data if isinstance(data, dict) else {}

    def section(self, name: str) -> dict:
        value = self.read().get(name)
        return value if isinstance(value, dict) else {}

    def write_section(self, name: str, value: dict) -> None:
        data = {**self.read(), "version": 1, name: value}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


class ModelProfile(BaseModel):
    """One confirmed model endpoint the operator can switch back to."""

    id: str
    model_name: str
    base_url: str
    api_key: str
    verified_at: str = ""

    def view(self, active: bool) -> dict:
        return {"id": self.id, "model_name": self.model_name, "base_url": self.base_url,
                "api_key_masked": mask_secret(self.api_key),
                "verified_at": self.verified_at or None, "active": active}


class ModelSettings:
    """The generation models the app can run on: confirmed once, then switched in place."""

    def __init__(self, settings: Settings, store: SettingsStore | None = None) -> None:
        self.settings = settings
        self.store = store or SettingsStore(settings.settings_file)
        # Captured before any saved profile is applied, so removing the last profile can
        # hand control back to `.env`.
        self.env_defaults = {"model_name": settings.qwen_model, "base_url": settings.qwen_base_url,
                             "api_key": settings.qwen_api_key}
        self.profiles: list[ModelProfile] = []
        self.active_id: str | None = None

    @property
    def active(self) -> ModelProfile | None:
        return self._find(self.active_id)

    def current(self) -> dict:
        active = self.active
        api_key = self.settings.qwen_api_key
        return {
            "model_name": self.settings.qwen_model,
            "base_url": self.settings.qwen_base_url,
            "api_key_masked": mask_secret(api_key),
            "configured": bool(api_key),
            "source": "settings" if active else ("env" if self.env_defaults["api_key"] else "none"),
            "verified_at": active.verified_at or None if active else None,
            "active_id": self.active_id,
            "profiles": [item.view(item.id == self.active_id) for item in self.profiles],
            "fallback": {"model_name": self.env_defaults["model_name"],
                         "base_url": self.env_defaults["base_url"],
                         "configured": bool(self.env_defaults["api_key"])},
        }

    def apply_saved(self) -> bool:
        """Restore the last confirmed selection before the first request needs it."""
        saved = self.store.section(MODEL_SECTION)
        profiles = []
        for raw in saved.get("profiles", []):
            if not isinstance(raw, dict):
                continue
            try:
                profile = ModelProfile(**{key: raw.get(key, "") for key in ModelProfile.model_fields})
            except ValidationError:
                logger.warning("跳过无法解析的模型配置")
                continue
            if not all([profile.model_name, profile.base_url, profile.api_key]):
                logger.warning("跳过字段不完整的模型配置")
                continue
            profiles.append(profile)
        self.profiles = profiles
        active_id = saved.get("active")
        active = self._find(active_id if isinstance(active_id, str) else None)
        if active is None:
            return False
        self._apply(**self._fields(active))
        self.active_id = active.id
        return True

    def confirm(self, model_name: str, base_url: str, api_key: str) -> dict:
        """Add or refresh a profile, then make it live; a failed probe changes nothing."""
        from .generation import verify_connection

        values = dict(zip(MODEL_FIELDS, (model_name.strip(), base_url.strip(), api_key.strip())))
        if not all(values.values()):
            raise ApiError("MODEL_CONFIG_INCOMPLETE",
                           "模型名称、Base URL 和 API Key 都需要填写，请补齐后重试", 422)
        _require_http_url(values["base_url"])
        verify_connection(values["model_name"], values["base_url"], values["api_key"])
        # Same model at the same address refreshes the existing entry, so trying a new key
        # does not pile up rows the operator has to guess apart.
        existing = next((item for item in self.profiles if item.model_name == values["model_name"]
                         and item.base_url == values["base_url"]), None)
        verified_at = datetime.now(UTC).isoformat(timespec="seconds")
        if existing is None:
            created = ModelProfile(id=self._new_id(values["model_name"]),
                                   verified_at=verified_at, **values)
            self.profiles.append(created)
            chosen = created
        else:
            existing.api_key = values["api_key"]
            existing.verified_at = verified_at
            chosen = existing
        self.active_id = chosen.id
        self._persist()
        self._apply(**self._fields(chosen))
        return self.current()

    def select(self, profile_id: str) -> dict:
        """Switch to a stored profile; the endpoint was proven when it was confirmed."""
        profile = self._find(profile_id)
        if profile is None:
            raise ApiError("MODEL_PROFILE_NOT_FOUND", "该模型配置不存在，请刷新后重试", 404)
        self.active_id = profile.id
        self._persist()
        self._apply(**self._fields(profile))
        return self.current()

    def remove(self, profile_id: str) -> dict:
        """Drop a profile; removing the running one hands control back to `.env`."""
        profile = self._find(profile_id)
        if profile is None:
            raise ApiError("MODEL_PROFILE_NOT_FOUND", "该模型配置不存在，请刷新后重试", 404)
        self.profiles = [item for item in self.profiles if item.id != profile.id]
        was_active = self.active_id == profile.id
        self.active_id = None
        self._persist()
        if was_active:
            self._apply(**self.env_defaults)
        return self.current()

    def _find(self, profile_id: str | None) -> ModelProfile | None:
        return next((item for item in self.profiles if item.id == profile_id), None)

    def _new_id(self, model_name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", model_name.lower()).strip("-")[:40] or "model"
        taken = {item.id for item in self.profiles}
        candidate, suffix = slug, 2
        while candidate in taken:
            candidate = f"{slug}-{suffix}"
            suffix += 1
        return candidate

    def _persist(self) -> None:
        # Written before the live swap so a read-only disk cannot report a selection the next
        # start would forget.
        self.store.write_section(MODEL_SECTION, {"active": self.active_id,
                                                "profiles": [item.model_dump() for item in self.profiles]})

    def _apply(self, model_name: str, base_url: str, api_key: str) -> None:
        # The transport reads these fields per request, so writing them here is what makes a
        # selection take effect without restarting the service.
        self.settings.qwen_model = model_name
        self.settings.qwen_base_url = base_url
        self.settings.qwen_api_key = api_key

    @staticmethod
    def _fields(profile: ModelProfile) -> dict:
        return {name: getattr(profile, name) for name in MODEL_FIELDS}
