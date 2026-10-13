"""Staging: the per-job directory on local disk that the worker (and its
Docker sandbox) uses, pulled from MinIO before the run and pushed back
after. Section 8 and 10.3 of the build document.

Layout of <work_dir>/<job_id>/  (= the worker's SWARN_DATA_ROOT):
  workspace/   sessions/   runs/   knowledge/   artifacts/   .chroma/   .registry/   eval_runs/
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from swarn_backend.db import repo
from swarn_backend.db.models import Job, Workspace
from swarn_backend.settings import Settings
from swarn_backend.storage import keys
from swarn_backend.storage.s3 import ObjectStore
from swarn_backend.util import parse_uuid

log = logging.getLogger("swarn_backend.staging")

SUBDIRS = ("workspace", "sessions", "runs", "knowledge", "artifacts", ".chroma", ".registry",
           "eval_runs")


@dataclass
class Manifest:
    pulled: dict[str, tuple[int, float]] = field(default_factory=dict)   # workspace rel → (size, mtime)
    resume_ids: set[str] = field(default_factory=set)
    bytes: int = 0


@dataclass
class ClosedSession:
    session_id: str
    meta: dict
    steps: list[dict]
    trace_key: str


@dataclass
class PushResult:
    files: int = 0
    sessions: list[ClosedSession] = field(default_factory=list)
    run_uploaded: bool = False


def _stat(path: str) -> tuple[int, float]:
    st = os.stat(path)
    return st.st_size, st.st_mtime


def _registry_filename(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "_-." else "_" for c in name)
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{safe[:80]}-{digest}.parquet"


def _skip_workspace_file(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in keys.SKIP_DIRS for p in parts[:-1]):
        return True
    if parts[-1].startswith(keys.SKIP_FILE_PREFIXES):
        return True
    return False


# ─── pull ─────────────────────────────────────────────────────────────────────


async def pull(db: AsyncSession, store: ObjectStore, settings: Settings, job: Job, ws: Workspace,
               workdir: Path) -> Manifest:
    cap = int(settings.stage_max_gb * (1 << 30))
    man = Manifest()
    for sub in SUBDIRS:
        (workdir / sub).mkdir(parents=True, exist_ok=True)
    wsdir = workdir / "workspace"

    def _count(n: int) -> None:
        man.bytes += n
        if man.bytes > cap:
            raise RuntimeError(f"staging exceeds BACKEND_STAGE_MAX_GB ({settings.stage_max_gb} GB)")

    # 1. uploads: this job's batch and every batch an earlier job of the
    #    workspace read, so the agent can re-open files it saw before
    batch_ids = set(await repo.batches_used_by_workspace(db, ws.id))
    if job.data_batch_id:
        batch_ids.add(job.data_batch_id)
    for bid in batch_ids:
        for f in await repo.upload_files_of(db, bid):
            dest = wsdir / "uploads" / str(bid) / f.name
            _count(f.size)
            await store.download(f.object_key, str(dest))

    # 2. the workspace's own files
    for f in await repo.list_files(db, ws.id):
        dest = wsdir / f.path
        _count(f.size)
        await store.download(f.object_key, str(dest))
        man.pulled[f.path] = _stat(str(dest))

    # 3. datasets → .registry/ + manifest.json
    reg = workdir / ".registry"
    entries = []
    for d in await repo.list_datasets(db, ws.id):
        fname = _registry_filename(d.name)
        _count(d.bytes)
        await store.download(d.object_key, str(reg / fname))
        entries.append({"name": d.name, "file": fname, "parents": list(d.parents or []),
                        "tool": d.tool, "session_id": str(d.session_id) if d.session_id else None,
                        "step": d.step, "created_at": d.created_at, "bytes": d.bytes,
                        "columns": d.columns})
    (reg / "manifest.json").write_text(json.dumps(entries), encoding="utf-8")

    # 4. resume: ancestors' traces + an index.json so SessionStore finds them
    index_rows = []
    for sid in job.resume_session_ids or []:
        row = await repo.get_user_session(db, await repo.get_user(db, job.user_id), parse_uuid(sid))
        if row is None or not row.trace_key:
            continue
        dest = workdir / "sessions" / sid / "trace.json"
        await store.download(row.trace_key, str(dest))
        man.resume_ids.add(sid)
        index_rows.append(repo.session_index_row(row))
    if index_rows:
        (workdir / "sessions" / "index.json").write_text(json.dumps(index_rows), encoding="utf-8")

    # 5. shared knowledge (AIDE reads the playbook and run archive)
    if job.method == "aide":
        _count(await store.download_prefix(keys.KNOWLEDGE_PREFIX, str(workdir / "knowledge"), cap))

    # 6. the workspace's RAG index and document store, when they exist
    for sub in (".chroma", "artifacts"):
        _count(await store.download_prefix(keys.workspace_prefix(ws.id, sub), str(workdir / sub), cap))
    return man


# ─── push ─────────────────────────────────────────────────────────────────────


async def upload_workspace_file(db: AsyncSession, store: ObjectStore, ws: Workspace, workdir: Path,
                                rel: str, *, job_id: str | None, session_id: str | None = None,
                                step: int | None = None, kind: str | None = None,
                                dataset: str | None = None, spec: str | None = None) -> bool:
    """Upload one workspace file and upsert its row. False when it is missing."""
    full = workdir / "workspace" / rel
    if not full.is_file():
        return False
    key = keys.workspace_file_key(ws.id, rel)
    ctype = keys.content_type_for(rel)
    size = await store.put_file(key, str(full), ctype)
    await repo.upsert_file(db, ws.id, rel, kind=kind or keys.file_kind(rel), size=size,
                           content_type=ctype, object_key=key, job_id=job_id,
                           session_id=parse_uuid(session_id), step=step, dataset=dataset,
                           spec_path=spec)
    return True


async def upload_dataset(db: AsyncSession, store: ObjectStore, ws: Workspace, workdir: Path,
                         ev: dict, job_id: str) -> None:
    """A `dataset_dumped` event: upload the parquet and supersede the row."""
    path = workdir / ev["path"]
    if not path.is_file():
        return
    key = keys.workspace_dataset_key(ws.id, _registry_filename(ev["name"])[:-8])
    size = await store.put_file(key, str(path), keys.content_type_for("x.parquet"))
    await repo.upsert_dataset(db, ws.id, ev["name"], rows=int(ev.get("rows") or 0),
                              cols=int(ev.get("cols") or 0), columns=ev.get("columns") or [],
                              parents=[p for p in ev.get("parents") or [] if isinstance(p, str)],
                              tool=ev.get("tool"), session_id=parse_uuid(ev.get("session_id")),
                              job_id=job_id, step=ev.get("step"), created_at=ev.get("created_at"),
                              object_key=key, bytes=size)


async def push(db: AsyncSession, store: ObjectStore, settings: Settings, job: Job, ws: Workspace,
               workdir: Path, man: Manifest) -> PushResult:
    out = PushResult()
    wsdir = workdir / "workspace"

    # 1. workspace files that are new or changed since the pull
    if wsdir.is_dir():
        for root, dirs, files in os.walk(wsdir):
            dirs[:] = [d for d in dirs if d not in keys.SKIP_DIRS]
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, wsdir).replace(os.sep, "/")
                if _skip_workspace_file(rel):
                    continue
                try:
                    st = _stat(full)
                except OSError:
                    continue
                if man.pulled.get(rel) == st:
                    continue
                if await upload_workspace_file(db, store, ws, workdir, rel, job_id=job.id):
                    out.files += 1

    # 2. sessions closed in this job (every trace that was not staged for resume)
    sdir = workdir / "sessions"
    if sdir.is_dir():
        for entry in sorted(sdir.iterdir()):
            trace = entry / "trace.json"
            if not entry.is_dir() or entry.name in man.resume_ids or not trace.is_file():
                continue
            try:
                data = json.loads(trace.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.warning("job %s: unreadable trace %s: %s", job.id, trace, e)
                continue
            sid = str(data.get("id") or entry.name)
            tkey = keys.session_key(sid, "trace.json")
            await store.put_file(tkey, str(trace), "application/json")
            summary = entry / "summary.md"
            if summary.is_file():
                await store.put_file(keys.session_key(sid, "summary.md"), str(summary),
                                     "text/markdown; charset=utf-8")
            steps = data.pop("steps", []) or []
            out.sessions.append(ClosedSession(session_id=sid, meta=data, steps=steps, trace_key=tkey))

    # 3. AIDE run directory
    if job.method == "aide" and job.run_id:
        rdir = workdir / "runs" / job.run_id
        if rdir.is_dir():
            await store.upload_dir(str(rdir), keys.run_prefix(job.run_id),
                                   skip=lambda rel: "/__pycache__/" in f"/{rel}")
            out.run_uploaded = True
        kdir = workdir / "knowledge"
        if kdir.is_dir() and any(kdir.iterdir()):
            await store.upload_dir(str(kdir), keys.KNOWLEDGE_PREFIX)

    # 4. per-workspace RAG index and document store
    for sub in (".chroma", "artifacts"):
        d = workdir / sub
        if d.is_dir() and any(d.rglob("*")):
            await store.upload_dir(str(d), keys.workspace_prefix(ws.id, sub))
    return out


def new_workdir(settings: Settings, job_id: str) -> Path:
    path = settings.work_path / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def workdir_for(settings: Settings, job_id: str) -> Path:
    return settings.work_path / job_id


_unused = (uuid,)
