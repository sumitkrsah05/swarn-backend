"""Uploads: multipart straight to MinIO, rows in Postgres. Response shapes
match agent2's dashboard; `data_dir` is now `uploads/<batch_id>`."""

from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from swarn_backend.auth import deps
from swarn_backend.db import repo
from swarn_backend.db.engine import get_db
from swarn_backend.db.models import User
from swarn_backend.settings import Settings, get_settings
from swarn_backend.storage import keys
from swarn_backend.storage.s3 import ObjectStore, get_store
from swarn_backend.util import epoch, now_utc, sanitize_upload_name

router = APIRouter(prefix="/api", tags=["uploads"], dependencies=[Depends(deps.current_user)])


class _HashingReader:
    """Wraps the spooled upload so boto3 streams it while we hash it."""

    def __init__(self, f, limit: int):
        self._f, self._limit, self.size, self.sha = f, limit, 0, hashlib.sha256()

    def read(self, n: int = -1) -> bytes:
        data = self._f.read(n)
        self.size += len(data)
        if self.size > self._limit:
            raise ValueError("too large")
        self.sha.update(data)
        return data


@router.post("/upload")
async def upload(request: Request, files: list[UploadFile] = File(...),
                 user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db),
                 settings: Settings = Depends(get_settings), store: ObjectStore = Depends(get_store)):
    if not files:
        raise HTTPException(422, "no files")
    names = [sanitize_upload_name(f.filename) for f in files]
    batch = await repo.create_upload_batch(db, user, ", ".join(names)[:500])
    limit = settings.max_upload_mb << 20
    saved: list[str] = []
    seen: set[str] = set()
    for f, name in zip(files, names, strict=True):
        if name in seen:                       # two parts with the same name: keep both
            stem, dot, ext = name.rpartition(".")
            name = f"{stem or ext}_{len(seen)}{dot}{ext if stem else ''}"
        seen.add(name)
        key = keys.upload_key(user.id, batch.id, name)
        reader = _HashingReader(f.file, limit)
        try:
            await store.put_fileobj(key, reader, f.content_type or keys.content_type_for(name))
        except ValueError:
            await store.delete_key(key)
            raise HTTPException(413, f"{name} is larger than {settings.max_upload_mb} MB")
        await repo.add_upload_file(db, batch, name=name, size=reader.size, sha256=reader.sha.digest(),
                                   content_type=f.content_type, key=key)
        saved.append(name)
    await repo.audit(db, user, "upload.create", "upload_batch", str(batch.id),
                     details={"files": saved}, ip=deps.client_ip(request))
    await db.commit()
    rel = f"uploads/{batch.id}"
    return {"data_dir": rel, "relative_dir": rel, "batch": str(batch.id), "files": saved}


@router.get("/uploads")
async def list_uploads(user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db)):
    out = []
    for batch, files in await repo.list_upload_batches(db, user):
        rel = f"uploads/{batch.id}"
        out.append({"batch": str(batch.id), "data_dir": rel, "relative_dir": rel,
                    "created": epoch(batch.created_at),
                    "files": [{"name": f.name, "size": f.size} for f in files]})
    return {"uploads": out}


@router.delete("/uploads/{batch}")
async def delete_upload(batch: str, request: Request, user: User = Depends(deps.current_user),
                        db: AsyncSession = Depends(get_db), store: ObjectStore = Depends(get_store)):
    try:
        bid = uuid.UUID(batch)
    except ValueError:
        raise HTTPException(422, "invalid batch name")
    row = await repo.get_upload_batch(db, user, bid)
    if row is None:
        raise HTTPException(404, f"no upload batch '{batch}'")
    if await repo.batch_in_use(db, bid):
        raise HTTPException(409, "a running job is using this upload")
    row.deleted_at = now_utc()
    await store.delete_prefix(f"users/{row.user_id}/uploads/{bid}/")
    await repo.audit(db, user, "upload.delete", "upload_batch", batch, ip=deps.client_ip(request))
    await db.commit()
    return {"deleted": batch}
