"""`/api/data/*` — datasets served from parquet objects (section 11)."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from swarn_backend.auth import deps
from swarn_backend.data import frames
from swarn_backend.db import repo
from swarn_backend.db.engine import get_db
from swarn_backend.db.models import Dataset, User
from swarn_backend.settings import get_settings
from swarn_backend.storage.s3 import ObjectStore, get_store

router = APIRouter(prefix="/api/data", tags=["data"], dependencies=[Depends(deps.current_user)])

_cache: frames.FrameCache | None = None


def cache() -> frames.FrameCache:
    global _cache
    if _cache is None:
        _cache = frames.FrameCache(get_settings().dataset_cache_mb << 20)
    return _cache


async def _workspace(db, user, workspace_id):
    ws = await repo.resolve_workspace(db, user, workspace_id, create_default=False)
    if ws is None:
        raise HTTPException(404, "no workspace yet")
    return ws


async def _frame(store: ObjectStore, row: Dataset):
    key = f"{row.id}"
    frame = cache().get(key)
    if frame is None:
        data = await store.get_bytes(row.object_key)
        if data is None:
            raise HTTPException(404, frames.NOT_IN_MEMORY.format(name=row.name))
        frame = await asyncio.to_thread(frames.read_parquet_bytes, data)
        cache().put(key, frame)
    return frame


async def _row_or_404(db, ws, name: str) -> Dataset:
    row = await repo.get_dataset(db, ws.id, name)
    if row is None:
        raise HTTPException(404, frames.NOT_IN_MEMORY.format(name=name))
    return row


@router.get("/datasets")
async def list_datasets(workspace_id: uuid.UUID | None = None,
                        user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db)):
    ws = await repo.resolve_workspace(db, user, workspace_id, create_default=False)
    if ws is None:
        return {"datasets": []}
    rows = await repo.list_datasets(db, ws.id)
    by_name = {r.name: r.columns or [] for r in rows}
    entries = [frames.dataset_entry(r, by_name) for r in rows]
    entries.sort(key=lambda e: (e["created_at"] is None, -(e["created_at"] or 0.0), e["name"]))
    return {"datasets": entries}


@router.get("/datasets/{name}/rows")
async def dataset_rows(name: str, offset: int = 0, limit: int = frames.DEFAULT_LIMIT, sort: str = "",
                       desc: bool = False, q: str = "", filters: str = "",
                       workspace_id: uuid.UUID | None = None,
                       user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db),
                       store: ObjectStore = Depends(get_store)):
    ws = await _workspace(db, user, workspace_id)
    row = await _row_or_404(db, ws, name)
    frame = await _frame(store, row)
    return await asyncio.to_thread(frames.rows_view, frame, name, offset=offset, limit=limit,
                                   sort=sort, desc=desc, q=q, filters=filters)


@router.get("/datasets/{name}/columns/{column}")
async def dataset_column(name: str, column: str, workspace_id: uuid.UUID | None = None,
                         user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db),
                         store: ObjectStore = Depends(get_store)):
    ws = await _workspace(db, user, workspace_id)
    row = await _row_or_404(db, ws, name)
    frame = await _frame(store, row)
    return await asyncio.to_thread(frames.column_view, frame, name, column)


@router.get("/datasets/{name}/export")
async def dataset_export(name: str, format: str = "csv", workspace_id: uuid.UUID | None = None,
                         user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db),
                         store: ObjectStore = Depends(get_store)):
    if format != "csv":
        raise HTTPException(422, "format must be 'csv'")
    ws = await _workspace(db, user, workspace_id)
    row = await _row_or_404(db, ws, name)
    frame = await _frame(store, row)
    filename = f"{frames.safe_filename(name)}.csv"
    return StreamingResponse(frames.iter_csv(frame), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})
