"""Admin: users, every job, the audit log, usage totals."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from swarn_backend.auth import deps
from swarn_backend.db import repo
from swarn_backend.db.engine import get_db
from swarn_backend.db.models import User
from swarn_backend.util import epoch

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(deps.require_admin)])


def _user(u: User) -> dict:
    return {"id": str(u.id), "email": u.email, "display_name": u.display_name, "role": u.role,
            "active": u.active, "created_at": epoch(u.created_at),
            "last_login_at": epoch(u.last_login_at)}


@router.get("/users")
async def users(db: AsyncSession = Depends(get_db)):
    return {"users": [_user(u) for u in await repo.list_users(db)]}


class UserPatch(BaseModel):
    role: str | None = None
    active: bool | None = None


@router.patch("/users/{user_id}")
async def patch_user(user_id: uuid.UUID, body: UserPatch, request: Request,
                     admin: User = Depends(deps.require_admin), db: AsyncSession = Depends(get_db)):
    target = await repo.get_user(db, user_id)
    if target is None:
        raise HTTPException(404, "no such user")
    if target.id == admin.id and (body.role not in (None, "admin") or body.active is False):
        raise HTTPException(409, "you cannot demote or deactivate yourself")
    if body.role is not None:
        if body.role not in ("admin", "member"):
            raise HTTPException(422, "role must be admin or member")
        if target.role == "admin" and body.role != "admin" and await repo.count_active_admins(db) <= 1:
            raise HTTPException(409, "cannot demote the last active admin")
        target.role = body.role
    if body.active is not None:
        if target.active and not body.active and target.role == "admin" \
                and await repo.count_active_admins(db) <= 1:
            raise HTTPException(409, "cannot deactivate the last active admin")
        target.active = body.active
        if not body.active:
            await repo.delete_user_sessions(db, target.id)
    await repo.audit(db, admin, "user.update", "user", str(target.id),
                     details=body.model_dump(exclude_none=True), ip=deps.client_ip(request))
    await db.commit()
    return _user(target)


@router.get("/jobs")
async def all_jobs(status: str | None = None, user_id: uuid.UUID | None = None, limit: int = 200,
                   db: AsyncSession = Depends(get_db)):
    owner = await repo.get_user(db, user_id) if user_id else None
    jobs = await repo.list_jobs(db, owner, None, status, limit=max(1, min(limit, 1000)))
    return {"jobs": [{**repo.job_summary(j, await repo.pending_approvals(db, j.id)),
                      "user_id": str(j.user_id)} for j in jobs]}


@router.get("/audit")
async def audit_log(actor: uuid.UUID | None = None, limit: int = 200,
                    db: AsyncSession = Depends(get_db)):
    rows = await repo.list_audit(db, actor, max(1, min(limit, 2000)))
    return {"events": [{"id": r.id, "actor_user_id": str(r.actor_user_id) if r.actor_user_id else None,
                        "action": r.action, "target_type": r.target_type, "target_id": r.target_id,
                        "details": r.details, "ip": str(r.ip) if r.ip else None,
                        "occurred_at": epoch(r.occurred_at)} for r in rows]}


@router.get("/usage")
async def usage(user_id: uuid.UUID | None = None, db: AsyncSession = Depends(get_db)):
    return await repo.usage_totals(db, user_id)
