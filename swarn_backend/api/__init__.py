"""HTTP routers, one per resource. Everything but auth and /healthz requires
a signed-in user (the dependency is attached per router)."""

from __future__ import annotations

from fastapi import FastAPI

from swarn_backend.api import (admin, auth, data, health, jobs, playbook, runs, sessions, uploads,
                               workspace_files, workspaces, ws)


def include_all(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(workspaces.router)
    app.include_router(jobs.router)
    app.include_router(uploads.router)
    app.include_router(sessions.router)
    app.include_router(runs.router)
    app.include_router(workspace_files.router)
    app.include_router(data.router)
    app.include_router(playbook.router)
    app.include_router(admin.router)
    app.include_router(ws.router)
