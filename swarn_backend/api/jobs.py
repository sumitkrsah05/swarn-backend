"""Jobs — the contract of agent2's `POST/GET /api/jobs`, persisted and owned."""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from swarn_backend.auth import deps
from swarn_backend.db import repo
from swarn_backend.db.engine import get_db
from swarn_backend.db.models import Job, User
from swarn_backend.jobs.supervisor import get_supervisor
from swarn_backend.settings import Settings, get_settings
from swarn_backend.util import parse_uuid

router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(deps.current_user)])

VALID_METHODS = ("react", "aide", "team")
_UPLOAD_DIR = re.compile(r"(?:^|/)uploads/([0-9a-fA-F-]{36})/?$")


class JobRequest(BaseModel):
    task: str
    method: str = "react"
    data_dir: str | None = None
    steps: int = 10
    model: str = ""
    resume_session_ids: list[str] = Field(default_factory=list)
    workspace_id: uuid.UUID | None = None


class ApprovalAnswer(BaseModel):
    request_id: str
    answer: str


def _batch_id_from_data_dir(value: str | None) -> uuid.UUID | None:
    """Accept `uploads/<id>`, a bare id, or an absolute path ending in
    `uploads/<id>` (what an older frontend echoes back)."""
    if not value:
        return None
    m = _UPLOAD_DIR.search(value.strip())
    if m:
        return parse_uuid(m.group(1))
    return parse_uuid(value.strip())


async def summary(db: AsyncSession, job: Job) -> dict:
    return repo.job_summary(job, await repo.pending_approvals(db, job.id))


@router.post("")
async def submit(body: JobRequest, request: Request, user: User = Depends(deps.current_user),
                 db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    if body.method not in VALID_METHODS:
        raise HTTPException(422, f"method must be one of {VALID_METHODS}")
    if not body.task.strip():
        raise HTTPException(422, "task is required")
    ws = await repo.resolve_workspace(db, user, body.workspace_id)
    if ws is None:
        raise HTTPException(404, "no such workspace")

    batch_id = _batch_id_from_data_dir(body.data_dir)
    if body.data_dir and batch_id is None:
        raise HTTPException(422, f"data_dir does not exist: {body.data_dir}")
    batch = await repo.get_upload_batch(db, user, batch_id) if batch_id else None
    if body.data_dir and batch is None:
        raise HTTPException(422, f"data_dir does not exist: {body.data_dir}")
    if body.method == "aide" and batch is None:
        raise HTTPException(422, "AIDE needs a data directory — upload files first "
                                 "and pass the returned data_dir")

    resume = [s.strip() for s in body.resume_session_ids if s and s.strip()]
    if resume:
        if body.method != "react":
            raise HTTPException(422, "resume_session_ids only applies to method 'react' — aide "
                                     "and team runs don't take conversation context")
        missing = []
        for sid in resume:
            row = await repo.get_user_session(db, user, parse_uuid(sid)) if parse_uuid(sid) else None
            if row is None or row.ended_at is None or row.workspace_id != ws.id or not row.trace_key:
                missing.append(sid)
        if missing:
            raise HTTPException(422, f"resume session(s) not found: {', '.join(missing)} — a "
                                     "session's trace exists once its run has completed; check "
                                     "GET /api/sessions")

    if await repo.running_job_in_workspace(db, ws.id):
        raise HTTPException(409, "a job is already running in this workspace")
    if await repo.count_running_for_user(db, user.id) >= settings.max_running_per_user:
        raise HTTPException(429, f"you already have {settings.max_running_for_user} jobs running")

    job = await repo.create_job(db, user_id=user.id, workspace_id=ws.id, task=body.task,
                                method=body.method, status="queued", data_batch_id=batch.id if batch else None,
                                data_dir=f"uploads/{batch.id}" if batch else None,
                                steps=max(1, int(body.steps or 10)), model=body.model or "",
                                resume_session_ids=resume)
    await repo.append_job_event(db, job, {"type": "status", "status": "queued"})
    await repo.audit(db, user, "job.submit", "job", job.id,
                     details={"method": job.method, "workspace_id": str(ws.id)},
                     ip=deps.client_ip(request))
    await db.commit()
    await get_supervisor().enqueue(job.id)
    return await summary(db, job)


@router.get("")
async def list_jobs(workspace_id: uuid.UUID | None = None, limit: int = 100,
                    user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db)):
    jobs = await repo.list_jobs(db, user, workspace_id, limit=max(1, min(limit, 500)))
    return {"jobs": [await summary(db, j) for j in jobs]}


@router.get("/{job_id}")
async def job_detail(job_id: str, since: int = 0, user: User = Depends(deps.current_user),
                     db: AsyncSession = Depends(get_db)):
    job = await repo.get_user_job(db, user, job_id)
    if job is None:
        raise HTTPException(404, f"no job '{job_id}'")
    d = await summary(db, job)
    d["result"] = job.result
    d["error"] = job.error
    d["events"] = await repo.job_events_since(db, job.id, since)
    d["events_from"] = max(0, since)
    return d


@router.post("/{job_id}/approve")
async def approve(job_id: str, body: ApprovalAnswer, request: Request,
                  user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db)):
    job = await repo.get_user_job(db, user, job_id)
    if job is None:
        raise HTTPException(404, f"no job '{job_id}'")
    if job.user_id != user.id:
        raise HTTPException(403, "only the job's owner can answer its questions")
    pending = {a.id for a in await repo.pending_approvals(db, job.id)}
    if body.request_id not in pending or job.status != "running":
        raise HTTPException(409, f"request '{body.request_id}' is not waiting "
                                 "(already answered, timed out, or unknown)")
    delivered = await get_supervisor().approve(job.id, body.request_id, body.answer)
    if delivered is False:
        raise HTTPException(409, f"request '{body.request_id}' is not waiting "
                                 "(already answered, timed out, or unknown)")
    await repo.resolve_approval(db, body.request_id, answer=body.answer, via="responder",
                                answered_by=user.id)
    await repo.audit(db, user, "approval.answer", "approval", body.request_id,
                     details={"job_id": job.id}, ip=deps.client_ip(request))
    await db.commit()
    return await summary(db, job)


@router.post("/{job_id}/cancel")
async def cancel(job_id: str, request: Request, user: User = Depends(deps.current_user),
                 db: AsyncSession = Depends(get_db)):
    job = await repo.get_user_job(db, user, job_id)
    if job is None:
        raise HTTPException(404, f"no job '{job_id}'")
    if job.status in ("queued", "running") and not job.cancel_requested:
        job.cancel_requested = True
        await repo.append_job_event(db, job, {"type": "cancel_requested"})
        await repo.audit(db, user, "job.cancel", "job", job.id, ip=deps.client_ip(request))
        await db.commit()
        await get_supervisor().cancel(job.id)
    out = await summary(db, job)
    if job.method == "team" and job.status == "running":
        out["note"] = ("team runs can't be interrupted between roles — cancel recorded; the worker "
                       "is stopped after the grace period if it does not finish")
    return out
