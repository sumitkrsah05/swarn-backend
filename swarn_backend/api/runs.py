"""AIDE search runs: index from Postgres, journal/report/files from MinIO."""

from __future__ import annotations

import json
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

router = APIRouter(prefix="/api/runs", tags=["runs"], dependencies=[Depends(deps.current_user)])


@router.get("")
async def list_runs(limit: int = 50, workspace_id: uuid.UUID | None = None,
                    user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db)):
    rows = await repo.list_runs(db, user, workspace_id, max(1, min(limit, 500)))
    return {"runs": [{"run_id": r.id, "nodes": r.nodes, "best_metric": r.best_metric,
                      "status": r.status, "task": r.task, "created": r.created_at.timestamp(),
                      "job_id": r.job_id, "workspace_id": str(r.workspace_id)} for r in rows]}


async def _run_or_404(db, user, run_id):
    if "/" in run_id or run_id.startswith("."):
        raise HTTPException(400, "bad run id")
    row = await repo.get_user_run(db, user, run_id)
    if row is None:
        raise HTTPException(404, f"no run '{run_id}'")
    return row


@router.get("/{run_id}")
async def run_detail(run_id: str, user: User = Depends(deps.current_user),
                     db: AsyncSession = Depends(get_db), store: ObjectStore = Depends(get_store)):
    row = await _run_or_404(db, user, run_id)
    out: dict = {"run_id": row.id, "status": row.status, "best_metric": row.best_metric,
                 "nodes": row.nodes, "task": row.task}
    journal = await store.get_bytes(row.run_prefix + "journal.json")
    if journal:
        try:
            out["journal"] = json.loads(journal)
        except ValueError:
            pass
    report = await store.get_bytes(row.run_prefix + "report.md")
    if report:
        out["report_markdown"] = report.decode("utf-8", "replace")
    return out


@router.get("/{run_id}/files")
async def run_files(run_id: str, user: User = Depends(deps.current_user),
                    db: AsyncSession = Depends(get_db), store: ObjectStore = Depends(get_store)):
    row = await _run_or_404(db, user, run_id)
    files = [{"path": k[len(row.run_prefix):], "size": size}
             for k, size in await store.list_keys(row.run_prefix)]
    files.sort(key=lambda f: f["path"])
    if len(files) > 500:
        return {"run_id": run_id, "files": files[:500], "truncated": True}
    return {"run_id": run_id, "files": files}


@router.get("/{run_id}/files/{file_path:path}")
async def run_file(run_id: str, file_path: str, user: User = Depends(deps.current_user),
                   db: AsyncSession = Depends(get_db), store: ObjectStore = Depends(get_store)):
    row = await _run_or_404(db, user, run_id)
    rel = clean_relative_path(file_path)
    if not rel:
        raise HTTPException(403, "path escapes the allowed directory")
    key = row.run_prefix + rel
    head = await store.head(key)
    if head is None:
        raise HTTPException(404, f"no file '{file_path}' in run '{run_id}'")
    ctype = keys.content_type_for(rel)
    disposition = "inline" if keys.is_inline(ctype) else "attachment"
    return StreamingResponse(store.iter_object(key), media_type=ctype, headers={
        "Content-Length": str(head.get("ContentLength", "")),
        "Content-Disposition": f'{disposition}; filename="{rel.rsplit("/", 1)[-1]}"'})
