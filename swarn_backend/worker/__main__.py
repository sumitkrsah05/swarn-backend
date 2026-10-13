"""`python -m swarn_backend.worker` — one job, spec on stdin, events on stdout.

Order matters: read the spec, set the environment, THEN import agent2 (its
paths are fixed at import time).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    raw = sys.stdin.readline()
    if not raw.strip():
        print("worker: no spec on stdin", file=sys.stderr)
        return 2
    spec = json.loads(raw)

    from swarn_backend.worker.io import Emitter, take_protocol_stdout
    emit = Emitter(take_protocol_stdout())

    data_root = Path(spec["data_root"]).resolve()
    env = dict(spec.get("env") or {})
    env.update({
        "SWARN_DATA_ROOT": str(data_root),
        "SWARN_ARTIFACTS_DIR": str(data_root / "artifacts"),
        "SWARN_EVAL_DIR": str(data_root / "eval_runs"),
        "SWARN_LEDGER_DIR": str(data_root / "runs" / "ledgers"),
        "SWARN_KNOWLEDGE_DIR": str(data_root / "knowledge"),
        "SWARN_NO_BOOT_ANIM": "1",
        "PYTHONUNBUFFERED": "1",
        "TERM": "dumb",
    })
    env.pop("SWARN_AUTO_APPROVE", None)        # the approval card is the product
    env.setdefault("MPLBACKEND", "Agg")
    os.environ.update(env)
    for sub in ("workspace", "sessions", "runs/ledgers", "knowledge", "artifacts", "eval_runs",
                ".registry"):
        (data_root / sub).mkdir(parents=True, exist_ok=True)
    os.chdir(data_root)

    try:
        from swarn_backend.worker.runner import JobRunner
        return JobRunner(spec, emit).run()
    except Exception as exc:  # noqa: BLE001 — last line of defence: report, then exit non-zero
        import traceback
        emit("done", status="failed", result=None, error=f"{type(exc).__name__}: {exc}",
             usage={}, run_id=None)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
