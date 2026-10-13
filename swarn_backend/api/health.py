"""Probes. /healthz is public and says nothing; /readyz is admin-only."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from swarn_backend.auth import deps
from swarn_backend.db.engine import get_db
from swarn_backend.db.models import User
from swarn_backend.jobs.supervisor import get_supervisor
from swarn_backend.storage.s3 import ObjectStore, get_store
from swarn_backend.ws.manager import get_live

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(_: User = Depends(deps.require_admin), db: AsyncSession = Depends(get_db),
                 store: ObjectStore = Depends(get_store)):
    out = {"database": False, "object_store": False, "jobs_running": 0, "ws_connections": 0}
    try:
        await db.execute(text("select 1"))
        out["database"] = True
    except Exception as e:  # noqa: BLE001
        out["database_error"] = str(e)[:200]
    try:
        await store.ensure_bucket()
        out["object_store"] = True
    except Exception as e:  # noqa: BLE001
        out["object_store_error"] = str(e)[:200]
    try:
        out["jobs_running"] = get_supervisor().running()
    except RuntimeError:
        out["supervisor"] = "not started"
    out["ws_connections"] = get_live().connections()
    return out
