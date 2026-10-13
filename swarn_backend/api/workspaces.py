"""Workspaces (conversations): the ownership unit for datasets, files, sessions."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from swarn_backend.auth import deps
from swarn_backend.db import repo
from swarn_backend.db.engine import get_db
from swarn_backend.db.models import Job, User, Workspace
from swarn_backend.util import epoch, now_utc, parse_uuid

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"],
                   dependencies=[Depends(deps.current_user)])


async def _row(db: AsyncSession, ws: Workspace) -> dict:
    last = (await db.execute(select(func.max(Job.created_at)).where(Job.workspace_id == ws.id))).scalar()
    running = await repo.running_job_in_workspace(db, ws.id)
    return {"id": str(ws.id), "name": ws.name, "created_at": epoch(ws.created_at),
            "updated_at": epoch(ws.updated_at), "last_job_at": epoch(last),
            "running_job_id": running.id if running else None}


class WorkspaceBody(BaseModel):
    name: str


@router.get("")
async def list_workspaces(user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db)):
    return {"workspaces": [await _row(db, ws) for ws in await repo.list_workspaces(db, user)]}


@router.post("", status_code=201)
async def create_workspace(body: WorkspaceBody, user: User = Depends(deps.current_user),
                           db: AsyncSession = Depends(get_db)):
    ws = await repo.create_workspace(db, user, body.name.strip())
    await repo.audit(db, user, "workspace.create", "workspace", str(ws.id))
    await db.commit()
    return await _row(db, ws)


def _ws_or_404(ws: Workspace | None) -> Workspace:
    if ws is None:
        raise HTTPException(404, "no such workspace")
    return ws


@router.get("/{workspace_id}")
async def get_workspace(workspace_id: uuid.UUID, user: User = Depends(deps.current_user),
                        db: AsyncSession = Depends(get_db)):
    return await _row(db, _ws_or_404(await repo.get_workspace(db, user, workspace_id)))


@router.patch("/{workspace_id}")
async def rename_workspace(workspace_id: uuid.UUID, body: WorkspaceBody,
                           user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db)):
    ws = _ws_or_404(await repo.get_workspace(db, user, workspace_id))
    ws.name = body.name.strip()[:200] or ws.name
    await db.commit()
    return await _row(db, ws)


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(workspace_id: uuid.UUID, request: Request,
                           user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db)):
    ws = _ws_or_404(await repo.get_workspace(db, user, workspace_id))
    if await repo.running_job_in_workspace(db, ws.id):
        raise HTTPException(409, "a job is still running in this workspace")
    ws.deleted_at = now_utc()
    await repo.audit(db, user, "workspace.delete", "workspace", str(ws.id), ip=deps.client_ip(request))
    await db.commit()
    # objects are left for a later garbage collection pass; rows are what authorise access


@router.get("/{workspace_id}/state")
async def get_state(workspace_id: uuid.UUID, user: User = Depends(deps.current_user),
                    db: AsyncSession = Depends(get_db)):
    ws = _ws_or_404(await repo.get_workspace(db, user, workspace_id))
    return {"workspace_id": str(ws.id), "state": ws.client_state}


@router.put("/{workspace_id}/state")
async def put_state(workspace_id: uuid.UUID, request: Request,
                    user: User = Depends(deps.current_user), db: AsyncSession = Depends(get_db)):
    ws = _ws_or_404(await repo.get_workspace(db, user, workspace_id))
    body = await request.body()
    if len(body) > 2 * 1024 * 1024:
        raise HTTPException(413, "state larger than 2 MB")
    import json
    try:
        ws.client_state = json.loads(body or b"null")
    except ValueError:
        raise HTTPException(422, "state must be JSON")
    await db.commit()
    return {"workspace_id": str(ws.id), "saved": True}


_unused = (desc, parse_uuid)
