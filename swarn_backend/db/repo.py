"""Every query the API and the supervisor make. Route code never writes SQL.

Every read of a user-owned table takes the acting `User` and filters on
`user_id`; a row another user owns comes back as None (a 404 upstream, never
a 403). Admins pass `scope_user=None` through the explicit admin helpers.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, delete, desc, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from swarn_backend.db.models import (AgentSession, Approval, AuditLog, AuthSession, CliLoginCode,
                                     Dataset, File, Job, JobEvent, OAuthState, SearchNode,
                                     SearchRun, SessionStep, UploadBatch, UploadFile, UsageEvent,
                                     User, Workspace)
from swarn_backend.util import epoch, now_utc

ACTIVE_JOB = ("queued", "running")
TERMINAL_JOB = ("complete", "failed", "cancelled")


# ─── users ────────────────────────────────────────────────────────────────────


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.get(User, user_id)


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    return (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()


async def upsert_google_user(db: AsyncSession, *, sub: str, email: str, name: str | None,
                             picture: str | None, make_admin: bool) -> User:
    email = email.lower()
    user = (await db.execute(select(User).where(User.google_sub == sub))).scalar_one_or_none()
    if user is None:
        user = await get_user_by_email(db, email)
        if user is not None and user.google_sub is None:
            user.google_sub = sub
    if user is None:
        user = User(email=email, google_sub=sub, display_name=name, avatar_url=picture,
                    role="admin" if make_admin else "member")
        db.add(user)
    else:
        user.email = email
        user.display_name = name or user.display_name
        user.avatar_url = picture or user.avatar_url
        if make_admin:
            user.role = "admin"
    user.last_login_at = now_utc()
    await db.flush()
    return user


async def list_users(db: AsyncSession) -> list[User]:
    return list((await db.execute(select(User).order_by(User.created_at))).scalars())


async def count_active_admins(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(User)
                             .where(User.role == "admin", User.active.is_(True)))).scalar_one()


# ─── auth sessions ────────────────────────────────────────────────────────────


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


async def create_auth_session(db: AsyncSession, user: User, *, kind: str, max_days: int,
                              user_agent: str | None, ip: str | None,
                              label: str | None = None) -> tuple[AuthSession, str]:
    token = secrets.token_urlsafe(32)
    row = AuthSession(user_id=user.id, token_hash=hash_token(token), kind=kind, label=label,
                      expires_at=now_utc() + timedelta(days=max_days), user_agent=user_agent,
                      ip=ip)
    db.add(row)
    await db.flush()
    return row, token


async def load_auth_session(db: AsyncSession, token: str) -> tuple[AuthSession, User] | None:
    stmt = (select(AuthSession, User).join(User, User.id == AuthSession.user_id)
            .where(AuthSession.token_hash == hash_token(token)))
    row = (await db.execute(stmt)).first()
    return (row[0], row[1]) if row else None


async def touch_auth_session(db: AsyncSession, session: AuthSession) -> None:
    session.last_seen_at = now_utc()


async def delete_auth_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(delete(AuthSession).where(AuthSession.id == session_id))


async def delete_user_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
    await db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))


async def list_auth_sessions(db: AsyncSession, user_id: uuid.UUID) -> list[AuthSession]:
    return list((await db.execute(select(AuthSession).where(AuthSession.user_id == user_id)
                                  .order_by(desc(AuthSession.created_at)))).scalars())


# ─── oauth state and CLI login codes ──────────────────────────────────────────


async def save_oauth_state(db: AsyncSession, state: str, verifier: str, redirect_to: str | None,
                           cli_code: str | None = None) -> None:
    db.add(OAuthState(state=state, code_verifier=verifier, redirect_to=redirect_to,
                      cli_code=cli_code))
    await db.flush()


async def pop_oauth_state(db: AsyncSession, state: str, max_age_s: int = 600) -> OAuthState | None:
    row = await db.get(OAuthState, state)
    if row is None:
        return None
    await db.delete(row)
    if (now_utc() - row.created_at).total_seconds() > max_age_s:
        return None
    return row


async def create_cli_code(db: AsyncSession, label: str | None, ttl_s: int = 600) -> CliLoginCode:
    row = CliLoginCode(code=secrets.token_urlsafe(24), label=label,
                       expires_at=now_utc() + timedelta(seconds=ttl_s))
    db.add(row)
    await db.flush()
    return row


async def get_cli_code(db: AsyncSession, code: str) -> CliLoginCode | None:
    row = await db.get(CliLoginCode, code)
    if row is None:
        return None
    if row.status == "pending" and row.expires_at < now_utc():
        row.status = "expired"
    return row


# ─── workspaces ───────────────────────────────────────────────────────────────


async def list_workspaces(db: AsyncSession, user: User) -> list[Workspace]:
    stmt = (select(Workspace).where(Workspace.user_id == user.id, Workspace.deleted_at.is_(None))
            .order_by(desc(Workspace.updated_at)))
    return list((await db.execute(stmt)).scalars())


async def get_workspace(db: AsyncSession, user: User, workspace_id: uuid.UUID) -> Workspace | None:
    ws = await db.get(Workspace, workspace_id)
    if ws is None or ws.deleted_at is not None:
        return None
    if ws.user_id != user.id and user.role != "admin":
        return None
    return ws


async def create_workspace(db: AsyncSession, user: User, name: str) -> Workspace:
    ws = Workspace(user_id=user.id, name=name[:200] or "Workspace")
    db.add(ws)
    await db.flush()
    return ws


async def default_workspace(db: AsyncSession, user: User) -> Workspace:
    """The user's `Scratch` workspace, created on first use — what a client
    that does not send workspace_id gets."""
    stmt = (select(Workspace).where(Workspace.user_id == user.id, Workspace.name == "Scratch",
                                    Workspace.deleted_at.is_(None)).limit(1))
    ws = (await db.execute(stmt)).scalar_one_or_none()
    return ws or await create_workspace(db, user, "Scratch")


async def latest_workspace(db: AsyncSession, user: User) -> Workspace | None:
    """The workspace with the most recent job, else the most recently updated."""
    stmt = (select(Workspace).join(Job, Job.workspace_id == Workspace.id)
            .where(Workspace.user_id == user.id, Workspace.deleted_at.is_(None))
            .order_by(desc(Job.created_at)).limit(1))
    ws = (await db.execute(stmt)).scalar_one_or_none()
    if ws is not None:
        return ws
    rows = await list_workspaces(db, user)
    return rows[0] if rows else None


async def resolve_workspace(db: AsyncSession, user: User, workspace_id: uuid.UUID | None,
                            create_default: bool = True) -> Workspace | None:
    if workspace_id is not None:
        return await get_workspace(db, user, workspace_id)
    ws = await latest_workspace(db, user)
    if ws is None and create_default:
        ws = await default_workspace(db, user)
    return ws


async def running_job_in_workspace(db: AsyncSession, workspace_id: uuid.UUID) -> Job | None:
    stmt = (select(Job).where(Job.workspace_id == workspace_id, Job.status.in_(ACTIVE_JOB))
            .order_by(desc(Job.created_at)).limit(1))
    return (await db.execute(stmt)).scalar_one_or_none()


# ─── uploads ──────────────────────────────────────────────────────────────────


async def create_upload_batch(db: AsyncSession, user: User, label: str | None) -> UploadBatch:
    batch = UploadBatch(user_id=user.id, label=label)
    db.add(batch)
    await db.flush()
    return batch


async def add_upload_file(db: AsyncSession, batch: UploadBatch, *, name: str, size: int,
                          sha256: bytes | None, content_type: str | None, key: str) -> UploadFile:
    row = UploadFile(batch_id=batch.id, name=name, size=size, sha256=sha256,
                     content_type=content_type, object_key=key)
    db.add(row)
    await db.flush()
    return row


async def list_upload_batches(db: AsyncSession, user: User) -> list[tuple[UploadBatch, list[UploadFile]]]:
    stmt = (select(UploadBatch).where(UploadBatch.user_id == user.id, UploadBatch.deleted_at.is_(None))
            .order_by(desc(UploadBatch.created_at)))
    batches = list((await db.execute(stmt)).scalars())
    if not batches:
        return []
    ids = [b.id for b in batches]
    files = list((await db.execute(select(UploadFile).where(UploadFile.batch_id.in_(ids))
                                   .order_by(UploadFile.name))).scalars())
    by_batch: dict[uuid.UUID, list[UploadFile]] = {i: [] for i in ids}
    for f in files:
        by_batch[f.batch_id].append(f)
    return [(b, by_batch[b.id]) for b in batches]


async def get_upload_batch(db: AsyncSession, user: User, batch_id: uuid.UUID) -> UploadBatch | None:
    batch = await db.get(UploadBatch, batch_id)
    if batch is None or batch.deleted_at is not None:
        return None
    if batch.user_id != user.id and user.role != "admin":
        return None
    return batch


async def upload_files_of(db: AsyncSession, batch_id: uuid.UUID) -> list[UploadFile]:
    return list((await db.execute(select(UploadFile).where(UploadFile.batch_id == batch_id)
                                  .order_by(UploadFile.name))).scalars())


async def batches_used_by_workspace(db: AsyncSession, workspace_id: uuid.UUID) -> list[uuid.UUID]:
    stmt = (select(Job.data_batch_id).where(Job.workspace_id == workspace_id,
                                            Job.data_batch_id.is_not(None)).distinct())
    return [r for (r,) in (await db.execute(stmt)).all()]


async def batch_in_use(db: AsyncSession, batch_id: uuid.UUID) -> bool:
    stmt = select(func.count()).select_from(Job).where(Job.data_batch_id == batch_id,
                                                        Job.status.in_(ACTIVE_JOB))
    return (await db.execute(stmt)).scalar_one() > 0


# ─── jobs ─────────────────────────────────────────────────────────────────────


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


async def create_job(db: AsyncSession, **fields: Any) -> Job:
    job = Job(id=new_job_id(), **fields)
    db.add(job)
    await db.flush()
    return job


async def get_job(db: AsyncSession, job_id: str) -> Job | None:
    return await db.get(Job, job_id)


async def get_user_job(db: AsyncSession, user: User, job_id: str) -> Job | None:
    job = await db.get(Job, job_id)
    if job is None:
        return None
    if job.user_id != user.id and user.role != "admin":
        return None
    return job


async def list_jobs(db: AsyncSession, user: User | None, workspace_id: uuid.UUID | None = None,
                    status: str | None = None, limit: int = 100) -> list[Job]:
    stmt = select(Job)
    if user is not None:
        stmt = stmt.where(Job.user_id == user.id)
    if workspace_id is not None:
        stmt = stmt.where(Job.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(Job.status == status)
    stmt = stmt.order_by(desc(Job.created_at)).limit(limit)
    return list((await db.execute(stmt)).scalars())


async def count_running_for_user(db: AsyncSession, user_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(Job).where(Job.user_id == user_id,
                                                        Job.status.in_(ACTIVE_JOB))
    return (await db.execute(stmt)).scalar_one()


async def stale_jobs(db: AsyncSession) -> list[Job]:
    return list((await db.execute(select(Job).where(Job.status.in_(ACTIVE_JOB)))).scalars())


async def append_job_event(db: AsyncSession, job: Job, event: dict) -> dict:
    """Insert the next event (seq = n_events) and cache it on the job."""
    ts = float(event.get("ts") or now_utc().timestamp())
    etype = str(event.get("type") or "event")
    payload = {k: v for k, v in event.items() if k not in ("ts", "type")}
    seq = job.n_events
    db.add(JobEvent(job_id=job.id, seq=seq, ts=ts, type=etype, payload=payload))
    stored = {"ts": ts, "type": etype, **payload}
    job.n_events = seq + 1
    job.last_event = stored
    await db.flush()
    return stored


async def job_events_since(db: AsyncSession, job_id: str, since: int) -> list[dict]:
    stmt = (select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.seq >= max(0, since))
            .order_by(JobEvent.seq))
    return [{"ts": e.ts, "type": e.type, **e.payload} for e in (await db.execute(stmt)).scalars()]


async def pending_approvals(db: AsyncSession, job_id: str) -> list[Approval]:
    stmt = (select(Approval).where(Approval.job_id == job_id, Approval.status == "pending")
            .order_by(Approval.requested_at))
    return list((await db.execute(stmt)).scalars())


async def upsert_approval(db: AsyncSession, job_id: str, req: dict) -> Approval:
    row = await db.get(Approval, req["id"])
    if row is None:
        row = Approval(id=req["id"], job_id=job_id)
        db.add(row)
    row.checkpoint = req.get("checkpoint")
    row.question = req.get("question")
    row.options = req.get("options") or []
    row.default_answer = req.get("default")
    row.requested_at = req.get("created")
    row.status = "pending"
    await db.flush()
    return row


async def resolve_approval(db: AsyncSession, request_id: str, *, answer: str | None, via: str,
                           answered_by: uuid.UUID | None = None) -> Approval | None:
    row = await db.get(Approval, request_id)
    if row is None:
        return None
    row.answer = answer
    row.via = via
    row.status = ("answered" if via == "responder" else
                  "timed_out" if via == "default" else "released")
    row.resolved_at = now_utc()
    if answered_by is not None:
        row.answered_by = answered_by
    await db.flush()
    return row


def job_summary(job: Job, pending: list[Approval]) -> dict:
    return {
        "id": job.id,
        "task": job.task,
        "method": job.method,
        "status": job.status,
        "created": epoch(job.created_at),
        "started": epoch(job.started_at),
        "finished": epoch(job.finished_at),
        "cancel_requested": job.cancel_requested,
        "resume_session_ids": list(job.resume_session_ids or []),
        "session_id": str(job.session_id) if job.session_id else None,
        "run_id": job.run_id,
        "n_events": job.n_events,
        "last_event": job.last_event,
        "pending_approvals": [
            {"id": a.id, "request_id": a.id, "checkpoint": a.checkpoint, "question": a.question,
             "options": a.options or [], "default": a.default_answer, "created": a.requested_at}
            for a in pending],
        "usage": job.usage or {},
        "workspace_id": str(job.workspace_id),
        "data_dir": job.data_dir,
    }


# ─── agent sessions and steps ─────────────────────────────────────────────────


async def ensure_agent_session(db: AsyncSession, session_id: uuid.UUID, *, user_id: uuid.UUID,
                               workspace_id: uuid.UUID, job_id: str | None, task: str | None,
                               model: str | None) -> AgentSession:
    row = await db.get(AgentSession, session_id)
    if row is None:
        row = AgentSession(id=session_id, user_id=user_id, workspace_id=workspace_id, job_id=job_id,
                           task=task, model=model, started_at=now_utc().timestamp())
        db.add(row)
        await db.flush()
    return row


async def add_session_step(db: AsyncSession, session_id: uuid.UUID, seq: int, kind: str,
                           ts: float, data: dict) -> None:
    stmt = (pg_insert(SessionStep).values(session_id=session_id, seq=seq, kind=kind, ts=ts,
                                          data=data)
            .on_conflict_do_nothing(constraint="uq_session_steps_seq"))
    await db.execute(stmt)
    await db.execute(update(AgentSession).where(AgentSession.id == session_id,
                                                AgentSession.n_steps <= seq)
                     .values(n_steps=seq + 1))


async def session_step_count(db: AsyncSession, session_id: uuid.UUID) -> int:
    return (await db.execute(select(func.count()).select_from(SessionStep)
                             .where(SessionStep.session_id == session_id))).scalar_one()


async def list_session_steps(db: AsyncSession, session_id: uuid.UUID) -> list[SessionStep]:
    return list((await db.execute(select(SessionStep).where(SessionStep.session_id == session_id)
                                  .order_by(SessionStep.seq))).scalars())


async def get_user_session(db: AsyncSession, user: User, session_id: uuid.UUID) -> AgentSession | None:
    row = await db.get(AgentSession, session_id)
    if row is None:
        return None
    if row.user_id != user.id and user.role != "admin":
        return None
    return row


async def list_sessions(db: AsyncSession, user: User, workspace_id: uuid.UUID | None,
                        limit: int) -> list[AgentSession]:
    stmt = select(AgentSession).where(AgentSession.user_id == user.id)
    if workspace_id is not None:
        stmt = stmt.where(AgentSession.workspace_id == workspace_id)
    stmt = stmt.order_by(desc(AgentSession.started_at)).limit(limit)
    return list((await db.execute(stmt)).scalars())


def session_index_row(s: AgentSession) -> dict:
    usage = s.usage or {}
    tokens = None
    if usage.get("input_tokens") is not None or usage.get("output_tokens") is not None:
        tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    tool_calls = sum(int(v) for v in (s.tool_counts or {}).values())
    return {"id": str(s.id), "task": (s.task or "")[:80], "model": s.model, "outcome": s.outcome,
            "duration_s": s.duration_s, "tool_calls": tool_calls, "corrections": s.corrections,
            "started_at": s.started_at, "tokens": tokens}


# ─── search runs ──────────────────────────────────────────────────────────────


async def upsert_search_run(db: AsyncSession, run_id: str, **fields: Any) -> SearchRun:
    row = await db.get(SearchRun, run_id)
    if row is None:
        row = SearchRun(id=run_id, **fields)
        db.add(row)
    else:
        for k, v in fields.items():
            setattr(row, k, v)
    await db.flush()
    return row


async def upsert_search_node(db: AsyncSession, run_id: str, node: dict) -> None:
    values = {k: node.get(k) for k in ("parent_id", "step", "stage", "is_buggy", "suspicious",
                                        "metric", "exec_time", "exit_code", "timed_out", "ctime",
                                        "analysis", "plan", "next_action")}
    stmt = pg_insert(SearchNode).values(run_id=run_id, id=node["id"], **values)
    stmt = stmt.on_conflict_do_update(index_elements=["run_id", "id"], set_=values)
    await db.execute(stmt)


async def get_user_run(db: AsyncSession, user: User, run_id: str) -> SearchRun | None:
    row = await db.get(SearchRun, run_id)
    if row is None:
        return None
    if row.user_id != user.id and user.role != "admin":
        return None
    return row


async def list_runs(db: AsyncSession, user: User, workspace_id: uuid.UUID | None,
                    limit: int) -> list[SearchRun]:
    stmt = select(SearchRun).where(SearchRun.user_id == user.id)
    if workspace_id is not None:
        stmt = stmt.where(SearchRun.workspace_id == workspace_id)
    stmt = stmt.order_by(desc(SearchRun.created_at)).limit(limit)
    return list((await db.execute(stmt)).scalars())


# ─── datasets ─────────────────────────────────────────────────────────────────


async def list_datasets(db: AsyncSession, workspace_id: uuid.UUID) -> list[Dataset]:
    stmt = (select(Dataset).where(Dataset.workspace_id == workspace_id,
                                  Dataset.superseded_at.is_(None)))
    return list((await db.execute(stmt)).scalars())


async def get_dataset(db: AsyncSession, workspace_id: uuid.UUID, name: str) -> Dataset | None:
    stmt = (select(Dataset).where(Dataset.workspace_id == workspace_id, Dataset.name == name,
                                  Dataset.superseded_at.is_(None)).limit(1))
    return (await db.execute(stmt)).scalar_one_or_none()


async def upsert_dataset(db: AsyncSession, workspace_id: uuid.UUID, name: str, **fields: Any) -> Dataset:
    """Supersede the active row of that name (its object stays until GC) and
    insert the new one."""
    await db.execute(update(Dataset).where(Dataset.workspace_id == workspace_id,
                                           Dataset.name == name,
                                           Dataset.superseded_at.is_(None))
                     .values(superseded_at=now_utc()))
    row = Dataset(workspace_id=workspace_id, name=name, **fields)
    db.add(row)
    await db.flush()
    return row


# ─── files ────────────────────────────────────────────────────────────────────


async def upsert_file(db: AsyncSession, workspace_id: uuid.UUID, path: str, **fields: Any) -> File:
    stmt = (select(File).where(File.workspace_id == workspace_id, File.path == path,
                               File.deleted_at.is_(None)).limit(1))
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = File(workspace_id=workspace_id, path=path, **fields)
        db.add(row)
    else:
        for k, v in fields.items():
            if v is not None or k in ("job_id", "session_id", "step"):
                setattr(row, k, v)
    await db.flush()
    return row


async def list_files(db: AsyncSession, workspace_id: uuid.UUID, prefix: str = "") -> list[File]:
    stmt = select(File).where(File.workspace_id == workspace_id, File.deleted_at.is_(None))
    if prefix:
        stmt = stmt.where(File.path.like(prefix.replace("%", r"\%").replace("_", r"\_") + "%"))
    return list((await db.execute(stmt.order_by(File.path))).scalars())


async def get_file(db: AsyncSession, workspace_id: uuid.UUID, path: str) -> File | None:
    stmt = (select(File).where(File.workspace_id == workspace_id, File.path == path,
                               File.deleted_at.is_(None)).limit(1))
    return (await db.execute(stmt)).scalar_one_or_none()


# ─── ledgers ──────────────────────────────────────────────────────────────────


async def add_usage_event(db: AsyncSession, **fields: Any) -> None:
    db.add(UsageEvent(**fields))


async def audit(db: AsyncSession, actor: User | None, action: str, target_type: str | None = None,
                target_id: str | None = None, details: dict | None = None,
                ip: str | None = None) -> None:
    db.add(AuditLog(actor_user_id=actor.id if actor else None, action=action,
                    target_type=target_type, target_id=str(target_id) if target_id else None,
                    details=details, ip=ip))


async def list_audit(db: AsyncSession, actor: uuid.UUID | None, limit: int = 200) -> list[AuditLog]:
    stmt = select(AuditLog)
    if actor is not None:
        stmt = stmt.where(AuditLog.actor_user_id == actor)
    return list((await db.execute(stmt.order_by(desc(AuditLog.id)).limit(limit))).scalars())


async def usage_totals(db: AsyncSession, user_id: uuid.UUID | None) -> dict:
    stmt = select(func.coalesce(func.sum(UsageEvent.input_tokens), 0),
                  func.coalesce(func.sum(UsageEvent.output_tokens), 0),
                  func.coalesce(func.sum(UsageEvent.calls), 0),
                  func.coalesce(func.sum(UsageEvent.cost), 0),
                  func.count())
    if user_id is not None:
        stmt = stmt.where(UsageEvent.user_id == user_id)
    i, o, c, cost, n = (await db.execute(stmt)).one()
    return {"input_tokens": int(i), "output_tokens": int(o), "calls": int(c),
            "cost": float(cost) if cost is not None else None, "jobs": int(n)}


__all__ = [n for n in dir() if not n.startswith("_")]
_unused = (and_,)
