"""Session history: index rows and full traces, from Postgres (steps are
available live, not only after completion)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from swarn_backend.auth import deps
from swarn_backend.db import repo
from swarn_backend.db.engine import get_db
from swarn_backend.db.models import User
from swarn_backend.util import parse_uuid

router = APIRouter(prefix="/api/sessions", tags=["sessions"],
                   dependencies=[Depends(deps.current_user)])


@router.get("")
async def list_sessions(limit: int = 20, workspace_id: uuid.UUID | None = None,
                        user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db)):
    rows = await repo.list_sessions(db, user, workspace_id, max(1, min(limit, 500)))
    return {"sessions": [repo.session_index_row(s) for s in rows]}


@router.get("/{session_id}")
async def session_detail(session_id: str, user: User = Depends(deps.current_user),
                         db: AsyncSession = Depends(get_db)):
    sid = parse_uuid(session_id)
    row = await repo.get_user_session(db, user, sid) if sid else None
    if row is None:
        raise HTTPException(404, f"No completed session found matching '{session_id}'.")
    steps = await repo.list_session_steps(db, row.id)
    return {
        "id": str(row.id),
        "task": row.task,
        "model": row.model,
        "started_at": row.started_at,
        "ended_at": row.ended_at,
        "outcome": row.outcome,
        "summary": row.summary,
        "corrections": row.corrections,
        "usage": row.usage or {},
        "duration_s": row.duration_s,
        "tool_counts": row.tool_counts or {},
        "steps": [{"kind": s.kind, "time": s.ts, "data": s.data} for s in steps],
        "workspace_id": str(row.workspace_id),
        "job_id": row.job_id,
    }
