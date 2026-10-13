"""Workspace files: listing from the `files` index, downloads streamed from
MinIO. Same shapes as agent2's `/api/workspace/files|file`."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from swarn_backend.auth import deps
from swarn_backend.db import repo
from swarn_backend.db.engine import get_db
from swarn_backend.db.models import User
from swarn_backend.storage import keys
from swarn_backend.storage.s3 import ObjectStore, get_store
from swarn_backend.util import clean_relative_path

router = APIRouter(prefix="/api/workspace", tags=["workspace"],
                   dependencies=[Depends(deps.current_user)])


async def _workspace(db, user, workspace_id):
    ws = await repo.resolve_workspace(db, user, workspace_id, create_default=False)
    if ws is None:
        raise HTTPException(404, "no workspace yet")
    return ws


@router.get("/files")
async def list_files(path: str = "", workspace_id: uuid.UUID | None = None,
                     user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db)):
    ws = await _workspace(db, user, workspace_id)
    rel = clean_relative_path(path)
    if rel is None:
        raise HTTPException(403, "path escapes the allowed directory")
    prefix = rel + "/" if rel else ""
    rows = await repo.list_files(db, ws.id, prefix)
    # uploads live in the user's batches, not in `files`; surface them too
    entries: dict[str, dict] = {}
    for f in rows:
        rest = f.path[len(prefix):]
        if not rest:
            continue
        head, _, tail = rest.partition("/")
        if head.startswith(".") and not rel.startswith("."):
            continue
        if tail:
            entries.setdefault(head, {"name": head, "is_dir": True, "size": None})
        else:
            entries[head] = {"name": head, "is_dir": False, "size": f.size}
    if not rel:
        batches = await repo.list_upload_batches(db, user)
        if batches:
            entries.setdefault("uploads", {"name": "uploads", "is_dir": True, "size": None})
    elif rel == "uploads":
        for batch, _files in await repo.list_upload_batches(db, user):
            entries[str(batch.id)] = {"name": str(batch.id), "is_dir": True, "size": None}
    elif rel.startswith("uploads/"):
        bid = rel.split("/", 2)[1] if rel.count("/") == 1 else None
        try:
            batch = await repo.get_upload_batch(db, user, uuid.UUID(bid)) if bid else None
        except ValueError:
            batch = None
        if batch is not None:
            for f in await repo.upload_files_of(db, batch.id):
                entries[f.name] = {"name": f.name, "is_dir": False, "size": f.size}
    if not entries and rel and not rows:
        raise HTTPException(404, f"no workspace directory '{path}'")
    return {"path": rel, "entries": sorted(entries.values(), key=lambda e: e["name"])}


@router.get("/file")
async def get_file(path: str, workspace_id: uuid.UUID | None = None,
                   user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db),
                   store: ObjectStore = Depends(get_store)):
    ws = await _workspace(db, user, workspace_id)
    rel = clean_relative_path(path)
    if not rel:
        raise HTTPException(403, "path escapes the allowed directory")
    key: str | None = None
    size: int | None = None
    ctype: str | None = None
    if rel.startswith("uploads/"):
        parts = rel.split("/", 2)
        if len(parts) == 3:
            try:
                batch = await repo.get_upload_batch(db, user, uuid.UUID(parts[1]))
            except ValueError:
                batch = None
            if batch is not None:
                for f in await repo.upload_files_of(db, batch.id):
                    if f.name == parts[2]:
                        key, size, ctype = f.object_key, f.size, f.content_type
    else:
        row = await repo.get_file(db, ws.id, rel)
        if row is not None:
            key, size, ctype = row.object_key, row.size, row.content_type
    if key is None:
        raise HTTPException(404, f"no workspace file '{path}'")
    ctype = ctype or keys.content_type_for(rel)
    disposition = "inline" if keys.is_inline(ctype) else "attachment"
    headers = {"Content-Disposition": f'{disposition}; filename="{rel.rsplit("/", 1)[-1]}"'}
    if size:
        headers["Content-Length"] = str(size)
    return StreamingResponse(store.iter_object(key), media_type=ctype, headers=headers)
