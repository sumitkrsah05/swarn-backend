"""The supervisor: queue → slot → staging pull → worker subprocess → events →
push → final status. One instance per API process (section 10).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from swarn_backend.db import repo
from swarn_backend.db.models import Job, Workspace
from swarn_backend.jobs import staging
from swarn_backend.jobs.protocol import JOB_EVENT_NAMES, WorkerSpec, approve_command, cancel_command
from swarn_backend.settings import Settings
from swarn_backend.storage import keys
from swarn_backend.storage.s3 import ObjectStore
from swarn_backend.util import from_epoch, now_utc, parse_uuid
from swarn_backend.ws.manager import LiveManager

log = logging.getLogger("swarn_backend.jobs")

STDOUT_LIMIT = 64 * 1024 * 1024


@dataclass
class Child:
    job_id: str
    proc: asyncio.subprocess.Process
    workdir: Path
    manifest: staging.Manifest
    user_id: str
    stderr_tail: list[str] = field(default_factory=list)
    done: dict | None = None
    approval_waiters: dict[str, asyncio.Future] = field(default_factory=dict)
    cancel_at: float | None = None

    async def send(self, cmd: dict) -> bool:
        if self.proc.stdin is None or self.proc.stdin.is_closing():
            return False
        try:
            self.proc.stdin.write((json.dumps(cmd) + "\n").encode("utf-8"))
            await self.proc.stdin.drain()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False


class Supervisor:
    def __init__(self, settings: Settings, factory: async_sessionmaker[AsyncSession],
                 store: ObjectStore, live: LiveManager):
        self.settings = settings
        self.factory = factory
        self.store = store
        self.live = live
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._slots = asyncio.Semaphore(max(1, settings.job_concurrency))
        self._children: dict[str, Child] = {}
        self._tasks: set[asyncio.Task] = set()
        self._dispatch_task: asyncio.Task | None = None

    # ── lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._recover_stale()
        self.settings.work_path.mkdir(parents=True, exist_ok=True)
        self._dispatch_task = asyncio.create_task(self._dispatch(), name="job-dispatch")

    async def stop(self) -> None:
        if self._dispatch_task:
            self._dispatch_task.cancel()
        for child in list(self._children.values()):
            await child.send(cancel_command())
        for child in list(self._children.values()):
            try:
                await asyncio.wait_for(child.proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                child.proc.kill()
        for t in list(self._tasks):
            t.cancel()

    async def _recover_stale(self) -> None:
        async with self.factory() as db:
            for job in await repo.stale_jobs(db):
                job.status = "failed"
                job.error = "server restarted"
                job.finished_at = now_utc()
                await repo.append_job_event(db, job, {"type": "status", "status": "failed",
                                                      "result": None, "error": job.error,
                                                      "usage": job.usage or {}})
                log.warning("job %s marked failed: server restarted", job.id)
            await db.commit()
        # staging directories older than a day are leftovers
        root = self.settings.work_path
        if root.is_dir():
            cutoff = time.time() - 86400
            for d in root.iterdir():
                try:
                    if d.is_dir() and d.stat().st_mtime < cutoff:
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass

    async def enqueue(self, job_id: str) -> None:
        await self._queue.put(job_id)

    def running(self) -> int:
        return len(self._children)

    def is_running(self, job_id: str) -> bool:
        return job_id in self._children

    async def _dispatch(self) -> None:
        while True:
            job_id = await self._queue.get()
            await self._slots.acquire()
            task = asyncio.create_task(self._run(job_id), name=f"job-{job_id}")
            self._tasks.add(task)

            def _done(t: asyncio.Task, _jid=job_id) -> None:
                self._tasks.discard(t)
                self._slots.release()
                if t.cancelled():
                    return
                exc = t.exception()
                if exc:
                    log.exception("job %s: supervisor task failed", _jid, exc_info=exc)

            task.add_done_callback(_done)

    # ── helpers ─────────────────────────────────────────────────────────

    async def _emit(self, db: AsyncSession, job: Job, event: dict) -> None:
        stored = await repo.append_job_event(db, job, event)
        await self.live.send_to_user(str(job.user_id), {
            "channel": "job", "job_id": job.id, "method": job.method, "status": job.status,
            "task": job.task[:120], "event": stored,
        })

    async def _fail_early(self, job_id: str, error: str) -> None:
        async with self.factory() as db:
            job = await repo.get_job(db, job_id)
            if job is None:
                return
            job.status = "failed"
            job.error = error
            job.finished_at = now_utc()
            await self._emit(db, job, {"type": "status", "status": "failed", "result": None,
                                       "error": error, "usage": job.usage or {}})
            await db.commit()

    # ── the run ─────────────────────────────────────────────────────────

    async def _run(self, job_id: str) -> None:
        async with self.factory() as db:
            job = await repo.get_job(db, job_id)
            if job is None or job.status != "queued":
                return
            if job.cancel_requested:
                job.status = "cancelled"
                job.finished_at = now_utc()
                await self._emit(db, job, {"type": "status", "status": "cancelled", "result": None,
                                           "error": None, "usage": {}})
                await db.commit()
                return
            ws = await db.get(Workspace, job.workspace_id)
            if ws is None:
                await db.commit()
                await self._fail_early(job_id, "workspace no longer exists")
                return
            workdir = staging.new_workdir(self.settings, job.id)
            try:
                manifest = await staging.pull(db, self.store, self.settings, job, ws, workdir)
            except Exception as exc:  # noqa: BLE001
                log.exception("job %s: staging failed", job.id)
                await db.commit()
                await self._fail_early(job_id, f"staging failed: {type(exc).__name__}: {exc}")
                shutil.rmtree(workdir, ignore_errors=True)
                return
            spec = WorkerSpec(
                job_id=job.id, method=job.method, task=job.task, model=job.model or "",
                steps=job.steps, data_dir=(f"uploads/{job.data_batch_id}" if job.data_batch_id else None),
                resume_session_ids=list(job.resume_session_ids or []),
                workspace_id=str(ws.id), user_id=str(job.user_id), data_root=str(workdir),
                env=self.settings.swarn_env(), eval_config=job.eval_config,
                eval_options=job.eval_options or {},
                registry_max_mb=self.settings.hydrate_max_mb,
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.settings.worker_executable, "-m", "swarn_backend.worker",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd=str(workdir), limit=STDOUT_LIMIT,
                    start_new_session=True,
                )
            except OSError as exc:
                await db.commit()
                await self._fail_early(job_id, f"could not start worker: {exc}")
                return
            child = Child(job_id=job.id, proc=proc, workdir=workdir, manifest=manifest,
                          user_id=str(job.user_id))
            self._children[job.id] = child
            job.status = "running"
            job.started_at = now_utc()
            job.worker_pid = proc.pid
            await self._emit(db, job, {"type": "status", "status": "running"})
            await db.commit()
            user_id, ws_id = job.user_id, ws.id

        await child.send(spec.model_dump())
        stderr_task = asyncio.create_task(self._drain_stderr(child))
        try:
            assert proc.stdout is not None
            while True:
                try:
                    line = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    log.warning("job %s: oversized line from worker dropped", job_id)
                    continue
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                try:
                    await self._handle(job_id, user_id, ws_id, child, ev)
                except Exception:  # noqa: BLE001 — one bad event must not end the stream
                    log.exception("job %s: event handling failed: %s", job_id, str(ev)[:200])
            rc = await proc.wait()
        finally:
            stderr_task.cancel()
            self._children.pop(job_id, None)
        await self._finalize(job_id, child, rc)

    async def _drain_stderr(self, child: Child) -> None:
        assert child.proc.stderr is not None
        try:
            while True:
                line = await child.proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                child.stderr_tail.append(text)
                if len(child.stderr_tail) > 200:
                    del child.stderr_tail[0]
                log.debug("job %s [worker] %s", child.job_id, text)
        except asyncio.CancelledError:
            return

    # ── events ──────────────────────────────────────────────────────────

    async def _handle(self, job_id: str, user_id: uuid.UUID, ws_id: uuid.UUID, child: Child,
                      ev: dict) -> None:
        t = ev.get("t")
        if t == "log":
            log.log(logging.getLevelName(str(ev.get("level", "info")).upper()) if isinstance(
                logging.getLevelName(str(ev.get("level", "info")).upper()), int) else logging.INFO,
                "job %s [worker] %s", job_id, ev.get("message"))
            return
        if t == "done":
            child.done = ev
            return
        if t == "approval_nack":
            fut = child.approval_waiters.pop(str(ev.get("id")), None)
            if fut and not fut.done():
                fut.set_result(False)
            return

        async with self.factory() as db:
            job = await repo.get_job(db, job_id)
            if job is None:
                return
            ws = await db.get(Workspace, ws_id)
            if t == "session":
                sid = parse_uuid(ev.get("session_id"))
                if sid is None:
                    return
                if job.session_id is None:
                    job.session_id = sid
                await repo.ensure_agent_session(db, sid, user_id=user_id, workspace_id=ws_id,
                                                job_id=job.id, task=ev.get("task"),
                                                model=ev.get("model"))
                await self._emit(db, job, {"type": "session", "session_id": str(sid)})
            elif t == "session_step":
                sid = parse_uuid(ev.get("session_id"))
                if sid is None:
                    return
                await repo.ensure_agent_session(db, sid, user_id=user_id, workspace_id=ws_id,
                                                job_id=job.id, task=ev.get("task"), model=None)
                await repo.add_session_step(db, sid, int(ev["seq"]), str(ev["kind"]),
                                            float(ev["time"]), ev.get("data") or {})
                await self.live.send_to_user(str(user_id), {
                    "channel": "session", "session_id": str(sid), "task": ev.get("task", ""),
                    "kind": ev["kind"], "timestamp": ev["time"], "data": ev.get("data") or {},
                })
            elif t == "session_closed":
                sid = parse_uuid(ev.get("session_id"))
                if sid is not None:
                    row = await repo.ensure_agent_session(db, sid, user_id=user_id, workspace_id=ws_id,
                                                          job_id=job.id, task=None, model=None)
                    self._apply_session_meta(row, ev.get("meta") or {})
                    row.trace_key = keys.session_key(str(sid))
            elif t == "usage":
                snap = {k: v for k, v in ev.items() if k not in ("t", "ts")}
                job.usage = snap
                await self._emit(db, job, {"type": "usage", **snap})
            elif t == "node":
                run_id = ev.get("run_id")
                if run_id:
                    if job.run_id != run_id:
                        job.run_id = run_id
                    await repo.upsert_search_run(db, run_id, user_id=user_id, workspace_id=ws_id,
                                                 job_id=job.id, task=job.task, status="running",
                                                 nodes=int(ev.get("nodes") or 0),
                                                 best_metric=ev.get("best_metric"),
                                                 lower_is_better=ev.get("lower_is_better"),
                                                 run_prefix=keys.run_prefix(run_id))
                    if ev.get("node_id"):
                        await repo.upsert_search_node(db, run_id, {
                            "id": ev["node_id"], "parent_id": ev.get("parent_id"),
                            "step": ev.get("step"), "stage": ev.get("stage"),
                            "is_buggy": ev.get("is_buggy"), "suspicious": ev.get("suspicious"),
                            "metric": ev.get("metric"), "exec_time": ev.get("exec_time"),
                            "exit_code": ev.get("exit_code"), "timed_out": ev.get("timed_out"),
                            "ctime": ev.get("ctime"), "analysis": ev.get("analysis"),
                            "plan": ev.get("plan"), "next_action": ev.get("next_action")})
                await self._emit(db, job, {"type": "node", "step": ev.get("step"),
                                           "stage": ev.get("stage"), "is_buggy": ev.get("is_buggy"),
                                           "metric": ev.get("metric"),
                                           "best_metric": ev.get("best_metric"),
                                           "suspicious": ev.get("suspicious"),
                                           "note": ev.get("note")})
            elif t == "approval_request":
                req = {k: ev.get(k) for k in ("id", "checkpoint", "question", "options", "default",
                                              "created")}
                await repo.upsert_approval(db, job.id, req)
                await self._emit(db, job, {"type": "approval_request", **req,
                                           "request_id": req["id"]})
            elif t == "approval_resolved":
                rid = str(ev.get("id"))
                await repo.resolve_approval(db, rid, answer=ev.get("answer"),
                                            via=str(ev.get("via") or "default"))
                await self._emit(db, job, {"type": "approval_answered", "request_id": rid,
                                           "answer": str(ev.get("answer") or "")[:500],
                                           "via": ev.get("via")})
                fut = child.approval_waiters.pop(rid, None)
                if fut and not fut.done():
                    fut.set_result(True)
            elif t == "progress":
                await self._emit(db, job, {"type": "progress", "message": ev.get("message")})
            elif t == "artifact_files" and ws is not None:
                for f in ev.get("files") or []:
                    if not isinstance(f, dict) or not f.get("path"):
                        continue
                    await staging.upload_workspace_file(
                        db, self.store, ws, child.workdir, str(f["path"]), job_id=job.id,
                        session_id=ev.get("session_id"), step=ev.get("step"), kind=f.get("kind"),
                        dataset=f.get("dataset"), spec=f.get("spec"))
            elif t == "dataset_dumped" and ws is not None:
                await staging.upload_dataset(db, self.store, ws, child.workdir, ev, job.id)
            elif t == "status":
                pass    # the supervisor owns status transitions
            await db.commit()

    @staticmethod
    def _apply_session_meta(row, meta: dict) -> None:
        row.task = meta.get("task") or row.task
        row.model = meta.get("model") or row.model
        row.started_at = meta.get("started_at") or row.started_at
        row.ended_at = meta.get("ended_at")
        row.outcome = meta.get("outcome")
        row.summary = meta.get("summary")
        row.corrections = int(meta.get("corrections") or 0)
        row.duration_s = meta.get("duration_s")
        row.tool_counts = meta.get("tool_counts") or {}
        row.usage = meta.get("usage") or {}

    # ── finish ──────────────────────────────────────────────────────────

    async def _finalize(self, job_id: str, child: Child, rc: int) -> None:
        done = child.done or {}
        async with self.factory() as db:
            job = await repo.get_job(db, job_id)
            if job is None:
                return
            ws = await db.get(Workspace, job.workspace_id)
            status = done.get("status")
            error = done.get("error")
            if status not in ("complete", "cancelled", "failed"):
                status = "cancelled" if job.cancel_requested else "failed"
                if status == "failed" and not error:
                    tail = "\n".join(child.stderr_tail[-5:])
                    error = f"worker exited with code {rc}" + (f": {tail[-500:]}" if tail else "")
            push = None
            if ws is not None:
                try:
                    push = await staging.push(db, self.store, self.settings, job, ws,
                                              child.workdir, child.manifest)
                except Exception as exc:  # noqa: BLE001 — outputs lost is an error, not a crash
                    log.exception("job %s: push failed", job_id)
                    if status == "complete":
                        error = f"outputs could not be saved: {type(exc).__name__}: {exc}"
            if push is not None:
                for cs in push.sessions:
                    sid = parse_uuid(cs.session_id)
                    if sid is None:
                        continue
                    row = await repo.ensure_agent_session(db, sid, user_id=job.user_id,
                                                          workspace_id=job.workspace_id,
                                                          job_id=job.id, task=cs.meta.get("task"),
                                                          model=cs.meta.get("model"))
                    self._apply_session_meta(row, cs.meta)
                    row.trace_key = cs.trace_key
                    have = await repo.session_step_count(db, sid)
                    if have < len(cs.steps):
                        for seq, step in enumerate(cs.steps):
                            await repo.add_session_step(db, sid, seq, str(step.get("kind")),
                                                        float(step.get("time") or 0),
                                                        step.get("data") or {})
                    row.n_steps = max(row.n_steps, len(cs.steps))
                if job.method == "aide" and (done.get("run_id") or job.run_id):
                    run_id = done.get("run_id") or job.run_id
                    job.run_id = run_id
                    result = done.get("result") or {}
                    prefix = keys.run_prefix(run_id)
                    await repo.upsert_search_run(
                        db, run_id, user_id=job.user_id, workspace_id=job.workspace_id,
                        job_id=job.id, task=job.task, status=status,
                        steps_done=int(result.get("steps_done") or 0),
                        nodes=int(result.get("nodes") or 0), best_metric=result.get("best_metric"),
                        lower_is_better=result.get("lower_is_better"),
                        holdout_metric=result.get("holdout_metric"),
                        holdout_note=result.get("holdout_note"),
                        stopped_because=result.get("stopped_because"),
                        exported=bool(result.get("exported")), run_prefix=prefix,
                        report_key=prefix + "report.md",
                        solution_key=(prefix + "best_solution.py") if result.get("exported") else None,
                        finished_at=now_utc())
            job.status = status
            job.result = done.get("result")
            job.error = error
            job.usage = done.get("usage") or job.usage or {}
            job.exit_code = rc
            job.finished_at = now_utc()
            usage = job.usage or {}
            if usage.get("calls"):
                await repo.add_usage_event(db, user_id=job.user_id, job_id=job.id,
                                           workspace_id=job.workspace_id, model=job.model or None,
                                           input_tokens=int(usage.get("input_tokens") or 0),
                                           output_tokens=int(usage.get("output_tokens") or 0),
                                           cache_read_tokens=int(usage.get("cache_read_tokens") or 0),
                                           calls=int(usage.get("calls") or 0),
                                           cost=usage.get("cost"), currency=usage.get("currency"))
            await self._emit(db, job, {"type": "status", "status": status, "result": job.result,
                                       "error": error, "usage": job.usage})
            await db.commit()
        for fut in child.approval_waiters.values():
            if not fut.done():
                fut.set_result(False)
        if status == "failed" and self.settings.keep_failed_workdirs:
            log.info("job %s: keeping %s for inspection", job_id, child.workdir)
        else:
            shutil.rmtree(child.workdir, ignore_errors=True)

    # ── commands from the API ───────────────────────────────────────────

    async def approve(self, job_id: str, request_id: str, answer: str) -> bool | None:
        """True: delivered and acknowledged. False: the worker says it was not
        waiting. None: delivered, no acknowledgement within 2 s."""
        child = self._children.get(job_id)
        if child is None:
            return False
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        child.approval_waiters[request_id] = fut
        if not await child.send(approve_command(request_id, answer)):
            child.approval_waiters.pop(request_id, None)
            return False
        try:
            return await asyncio.wait_for(fut, timeout=2.0)
        except asyncio.TimeoutError:
            return None

    async def cancel(self, job_id: str) -> None:
        child = self._children.get(job_id)
        if child is None:
            return
        child.cancel_at = time.time()
        await child.send(cancel_command())
        asyncio.create_task(self._enforce_cancel(child))

    async def _enforce_cancel(self, child: Child) -> None:
        try:
            await asyncio.wait_for(child.proc.wait(), timeout=self.settings.cancel_grace_s)
            return
        except asyncio.TimeoutError:
            pass
        log.warning("job %s: worker ignored cancel for %ss, sending SIGTERM", child.job_id,
                    self.settings.cancel_grace_s)
        try:
            child.proc.send_signal(signal.SIGTERM)
            await asyncio.wait_for(child.proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            child.proc.kill()
        except ProcessLookupError:
            pass


_supervisor: Supervisor | None = None


def get_supervisor() -> Supervisor:
    if _supervisor is None:
        raise RuntimeError("supervisor not started")
    return _supervisor


def set_supervisor(s: Supervisor | None) -> None:
    global _supervisor
    _supervisor = s


_unused = (from_epoch,)
