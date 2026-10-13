"""The cross-run playbook, read from MinIO with a short cache."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from swarn_backend.auth import deps
from swarn_backend.settings import Settings, get_settings
from swarn_backend.storage import keys
from swarn_backend.storage.s3 import ObjectStore, get_store

router = APIRouter(prefix="/api", tags=["knowledge"], dependencies=[Depends(deps.current_user)])

_cached: tuple[float, str] = (0.0, "")


@router.get("/playbook")
async def playbook(store: ObjectStore = Depends(get_store), settings: Settings = Depends(get_settings)):
    global _cached
    at, text = _cached
    if time.time() - at > settings.playbook_cache_s:
        data = await store.get_bytes(keys.KNOWLEDGE_PREFIX + "playbook.md")
        text = data.decode("utf-8", "replace") if data else ""
        _cached = (time.time(), text)
    return {"playbook": text}
