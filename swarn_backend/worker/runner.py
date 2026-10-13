"""Run one job inside the worker process. Mirrors agent2's
agent/web/jobs.py `_run_react` / `_run_aide` / `_run_team`; the only
difference is where the output goes (the pipe) and where files live (the
staging directory the environment already points at).

Imports of `agent.*` happen inside functions: the environment must be set
before the first one (see __main__).
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any

from swarn_backend.worker.io import CommandReader, Emitter
from swarn_backend.worker.registry import RegistrySync

USAGE_EVENT_INTERVAL = 0.5


class JobCancelled(Exception):
    pass


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


class JobRunner:
    def __init__(self, spec: dict, emit: Emitter):
        self.spec = spec
        self.emit = emit
        self.stop = threading.Event()
        self.data_root = Path(spec["data_root"])
        self.broker = None
        self.sync: RegistrySync | None = None
        self._seq: dict[str, int] = defaultdict(int)
        self._sessions_seen: set[str] = set()
        self.run_id: str | None = None

    # ── wiring ──────────────────────────────────────────────────────────

    def _make_broker(self):
        from agent.core.hitl import ApprovalBroker

        emit = self.emit

        class EmittingBroker(ApprovalBroker):
            """Announces when a parked question is resolved, however it was."""

            def _park(self, req, timeout):
                super()._park(req, timeout)
                emit("approval_resolved", id=req.id, answer=req.answer, via=req.answered_via)

        broker = EmittingBroker()
        broker.set_responder(lambda req: emit("approval_request", **req))
        return broker

    def _on_approve(self, request_id: str, answer: str) -> None:
        if self.broker is None or not self.broker.answer(request_id, answer):
            self.emit("approval_nack", id=request_id)

    def _on_cancel(self) -> None:
        if not self.stop.is_set():
            self.stop.set()
            self.emit("log", level="info", message="cancel requested")
        if self.broker is not None:
            self.broker.release("cancelled")

    def _usage_tracker(self):
        from agent.llm.usage_tracker import UsageTracker

        tracker = UsageTracker(label=self.spec["job_id"])
        last = [0.0]

        def on_usage(t, final: bool) -> None:
            now = time.time()
            if not final and now - last[0] < USAGE_EVENT_INTERVAL:
                return
            last[0] = now
            self.emit("usage", **t.snapshot())

        tracker.add_listener(on_usage)
        return tracker

    def _subscribe_sessions(self) -> None:
        from agent.memory.memory import StepKind, get_session_store

        def on_step(session, step) -> None:
            sid = session.id
            seq = self._seq[sid]
            self._seq[sid] = seq + 1
            if sid not in self._sessions_seen:
                self._sessions_seen.add(sid)
                self.emit("session", session_id=sid, task=session.task, model=session.model)
            kind = step.kind.value if isinstance(step.kind, StepKind) else str(step.kind)
            self.emit("session_step", session_id=sid, task=session.task[:120], seq=seq,
                      kind=kind, time=step.timestamp, data=_jsonable(step.data))
            if kind != "tool_result":
                return
            arts = (step.data or {}).get("artifacts") or {}
            step_no = (step.data or {}).get("step")
            files = arts.get("files") or []
            if files:
                self.emit("artifact_files", session_id=sid, step=step_no, files=_jsonable(files))
            if self.sync is not None:
                for d in arts.get("datasets") or []:
                    name = d.get("name") if isinstance(d, dict) else None
                    if not name:
                        continue
                    info = self.sync.dump(name, force=True)
                    if info:
                        self.emit("dataset_dumped", **info)

        get_session_store().subscribe_to_all_sessions(on_step)

    # ── engines ─────────────────────────────────────────────────────────

    def _data_dir_abs(self) -> str | None:
        from agent.paths import WORKSPACE_DIR
        rel = self.spec.get("data_dir")
        if not rel:
            return None
        path = os.path.join(WORKSPACE_DIR, rel)
        return path if os.path.isdir(path) else None

    def run_react(self) -> dict:
        from agent.core.agent_loop import AgentLoop
        from agent.core.hitl import set_thread_broker
        from agent.core.self_correction import SelfCorrectionPolicy
        from agent.llm import DEFAULT_MODEL
        from agent.observability.observability import GuardrailPolicy
        from agent.web.jobs import attach_data_note, seed_history

        resume = list(self.spec.get("resume_session_ids") or [])
        agent = AgentLoop(model=self.spec.get("model") or DEFAULT_MODEL,
                          correction_policy=SelfCorrectionPolicy(),
                          guardrail_policy=GuardrailPolicy(),
                          keep_history=bool(resume))
        if resume:
            agent.history = seed_history(resume)
        set_thread_broker(self.broker)
        try:
            return agent.run(attach_data_note(self.spec["task"], self._data_dir_abs()),
                             stop_event=self.stop)
        finally:
            set_thread_broker(None)

    def run_team(self) -> dict:
        from agent.core.hitl import set_thread_broker
        from agent.core.orchestrator import Orchestrator
        from agent.llm import DEFAULT_MODEL
        from agent.observability.observability import GuardrailPolicy
        from agent.web.jobs import attach_data_note

        orchestrator = Orchestrator(model=self.spec.get("model") or DEFAULT_MODEL,
                                    guardrail_policy=GuardrailPolicy())
        set_thread_broker(self.broker)
        try:
            result = orchestrator.run(attach_data_note(self.spec["task"], self._data_dir_abs()))
        finally:
            set_thread_broker(None)
        return _jsonable(result)

    def run_aide(self) -> dict:
        from agent.ml.search import SearchConfig, run_search

        def on_step(node, journal) -> None:
            if self.stop.is_set():
                raise JobCancelled()
            best = journal.best_node()
            self.emit("node",
                      run_id=self.run_id,
                      step=node.step, stage=node.stage, is_buggy=node.is_buggy,
                      metric=node.metric, best_metric=best.metric if best else None,
                      suspicious=bool(getattr(node, "suspicious", False)),
                      note=((node.analysis or node.plan) or "")[:300],
                      node_id=node.id, parent_id=node.parent_id,
                      lower_is_better=getattr(node, "lower_is_better", None),
                      exec_time=getattr(node, "exec_time", None),
                      exit_code=getattr(node, "exit_code", None),
                      timed_out=getattr(node, "timed_out", None),
                      ctime=getattr(node, "ctime", None),
                      analysis=(node.analysis or None), plan=(node.plan or None),
                      next_action=getattr(node, "next_action", None),
                      nodes=len(journal))

        data_dir = self._data_dir_abs()
        if not data_dir:
            raise ValueError("aide needs a data directory (upload files first)")
        cfg = SearchConfig(steps=int(self.spec.get("steps") or 10),
                           knowledge_dir=str(self.data_root / "knowledge"))
        # the run id is minted inside run_search; watch the runs dir for it
        runs_dir = Path(cfg.runs_dir)
        before = {p.name for p in runs_dir.iterdir()} if runs_dir.is_dir() else set()

        def on_step_with_id(node, journal):
            if self.run_id is None and runs_dir.is_dir():
                new = {p.name for p in runs_dir.iterdir()} - before
                if new:
                    self.run_id = sorted(new)[-1]
                    self.emit("log", level="info", message=f"run {self.run_id}")
            on_step(node, journal)

        result = run_search(self.spec["task"], data_dir=data_dir, config=cfg,
                            on_step=on_step_with_id, broker=self.broker)
        self.run_id = result.run_id
        return {
            "method": "aide",
            "run_id": result.run_id,
            "steps_done": result.steps_done,
            "best_metric": result.best.metric if result.best else None,
            "lower_is_better": getattr(result.best, "lower_is_better", None) if result.best else None,
            "nodes": len(result.journal) if result.journal is not None else None,
            "holdout_metric": result.holdout_metric,
            "holdout_note": result.holdout_note,
            "stopped_because": result.stopped_because,
            "solution_path": (result.solution_path
                              if result.best and result.exported else None),
            "report_path": result.report_path,
            "exported": bool(result.exported),
        }

    def run_eval(self) -> dict:
        """Phase 2 placeholder: the eval engine runs as a job the same way;
        wiring the config/resume/harness options is the next step."""
        raise NotImplementedError("eval jobs are not wired in this backend yet")

    # ── main ────────────────────────────────────────────────────────────

    def _emit_closed_sessions(self, staged: set[str]) -> None:
        sdir = self.data_root / "sessions"
        if not sdir.is_dir():
            return
        for entry in sdir.iterdir():
            trace = entry / "trace.json"
            if not entry.is_dir() or entry.name in staged or not trace.is_file():
                continue
            try:
                data = json.loads(trace.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            steps = data.pop("steps", []) or []
            self.emit("session_closed", session_id=str(data.get("id") or entry.name),
                      meta=_jsonable(data), n_steps=len(steps))

    def run(self) -> int:
        from agent.llm.usage_tracker import track

        spec = self.spec
        staged = set(spec.get("resume_session_ids") or [])
        status, result, error = "failed", None, None
        self.broker = self._make_broker()
        CommandReader(__import__("sys").stdin, self._on_approve, self._on_cancel).start()
        tracker = self._usage_tracker()
        try:
            if spec["method"] in ("react", "team"):
                from agent.data.pipeline import get_data_pipeline
                self.sync = RegistrySync(get_data_pipeline(), self.data_root / ".registry",
                                         int(spec.get("registry_max_mb") or 2048),
                                         self.emit.log)
                n = self.sync.hydrate()
                if n:
                    self.emit("log", level="info", message=f"registry: {n} dataset(s) restored")
            self._subscribe_sessions()
            with track(tracker):
                if spec["method"] == "aide":
                    result = self.run_aide()
                elif spec["method"] == "team":
                    result = self.run_team()
                elif spec["method"] == "eval":
                    result = self.run_eval()
                else:
                    result = self.run_react()
            if self.stop.is_set() and (result or {}).get("outcome") == "cancelled":
                status = "cancelled"
            else:
                status = "complete"
        except JobCancelled:
            status = "cancelled"
        except Exception as exc:  # noqa: BLE001 — the job must record any failure
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            self.emit("log", level="error", message=traceback.format_exc()[-4000:])
        finally:
            try:
                if self.sync is not None:
                    for info in self.sync.dump_changed():
                        self.emit("dataset_dumped", **info)
            except Exception as exc:  # noqa: BLE001
                self.emit("log", level="warning", message=f"registry dump failed: {exc}")
            self._emit_closed_sessions(staged)
            try:
                from agent.runtime.execution import close_backend
                close_backend()
            except Exception as exc:  # noqa: BLE001
                self.emit("log", level="warning", message=f"sandbox close failed: {exc}")
            self.emit("done", status=status, result=_jsonable(result), error=error,
                      usage=tracker.snapshot(), run_id=self.run_id)
        return 0 if status != "failed" else 1
