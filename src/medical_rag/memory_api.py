"""Small local-user memory management API, independent of retrieval services."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from .memory import MemoryStore


class MemorySettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(strict=True)


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=500, strict=True)
    category: Literal["background", "preferences", "interests", "health"]
    sensitive: bool = Field(default=False, strict=True)


def memory_router(store: MemoryStore) -> APIRouter:
    """Expose no user selector; every caller is the shared local user."""
    router = APIRouter(prefix="/memory")

    def success(value: object) -> dict:
        return {"success": True, "data": value, "error": None}

    @router.get("")
    def state():
        return success(store.state())

    @router.put("/settings")
    def settings(payload: MemorySettingsRequest):
        store.set_enabled(payload.enabled)
        return success(store.state())

    @router.put("/profiles/{key}")
    def save_profile(key: str, payload: ProfileRequest):
        store.save_profile(key, payload.value, payload.category, payload.sensitive)
        return success(store.state())

    @router.delete("/profiles/{key}")
    def delete_profile(key: str):
        store.delete_profile(key)
        return success(store.state())

    @router.get("/episodes/{episode_id}")
    def episode(episode_id: str):
        return success(store.read_episode(episode_id))

    @router.delete("/episodes/{episode_id}")
    def delete_episode(episode_id: str):
        store.delete_episode(episode_id)
        return success(store.state())

    @router.delete("")
    def clear():
        store.clear()
        return success(store.state())

    @router.get("/jobs/{run_id}")
    def job(run_id: str):
        return success(store.job_status(run_id))

    return router
