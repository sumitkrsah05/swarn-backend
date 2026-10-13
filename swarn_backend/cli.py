"""`swarn-backend serve | migrate | backfill | make-admin`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def _alembic_config():
    from alembic.config import Config
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    return cfg


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    uvicorn.run("swarn_backend.main:app", host=args.host, port=args.port, workers=1,
                proxy_headers=True, forwarded_allow_ips="*", reload=args.reload,
                log_level=args.log_level)
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from alembic import command
    command.upgrade(_alembic_config(), args.revision)
    return 0


def cmd_revision(args: argparse.Namespace) -> int:
    from alembic import command
    command.revision(_alembic_config(), message=args.message, autogenerate=True)
    return 0


async def _make_admin(email: str) -> int:
    from swarn_backend.db import repo
    from swarn_backend.db.engine import session_factory
    async with session_factory()() as db:
        user = await repo.get_user_by_email(db, email)
        if user is None:
            print(f"no user with email {email} (they must sign in once first)")
            return 1
        user.role = "admin"
        await db.commit()
        print(f"{email} is now an admin")
        return 0


def cmd_make_admin(args: argparse.Namespace) -> int:
    return asyncio.run(_make_admin(args.email))


async def _backfill(source: Path, owner_email: str, workspace_name: str) -> int:
    """Import an agent2 checkout's sessions/ and AIDE runs into one workspace
    owned by `owner_email` (who must have signed in once)."""
    from swarn_backend.db import repo
    from swarn_backend.db.engine import session_factory
    from swarn_backend.storage import keys
    from swarn_backend.storage.s3 import get_store
    from swarn_backend.util import parse_uuid

    store = get_store()
    await store.ensure_bucket()
    async with session_factory()() as db:
        user = await repo.get_user_by_email(db, owner_email)
        if user is None:
            print(f"no user with email {owner_email}")
            return 1
        ws = await repo.create_workspace(db, user, workspace_name)
        n_sessions = n_runs = 0
        sdir = source / "sessions"
        if sdir.is_dir():
            for entry in sorted(sdir.iterdir()):
                trace = entry / "trace.json"
                sid = parse_uuid(entry.name)
                if sid is None or not trace.is_file():
                    continue
                data = json.loads(trace.read_text(encoding="utf-8"))
                steps = data.pop("steps", []) or []
                tkey = keys.session_key(str(sid))
                await store.put_file(tkey, str(trace), "application/json")
                row = await repo.ensure_agent_session(db, sid, user_id=user.id, workspace_id=ws.id,
                                                      job_id=None, task=data.get("task"),
                                                      model=data.get("model"))
                row.started_at = data.get("started_at")
                row.ended_at = data.get("ended_at")
                row.outcome = data.get("outcome")
                row.summary = data.get("summary")
                row.corrections = int(data.get("corrections") or 0)
                row.duration_s = data.get("duration_s")
                row.tool_counts = data.get("tool_counts") or {}
                row.usage = data.get("usage") or {}
                row.trace_key = tkey
                for seq, step in enumerate(steps):
                    await repo.add_session_step(db, sid, seq, str(step.get("kind")),
                                                float(step.get("time") or 0), step.get("data") or {})
                n_sessions += 1
        for rdir_root in (source / "agent" / "runs", source / "runs"):
            if not rdir_root.is_dir():
                continue
            for rdir in sorted(rdir_root.iterdir()):
                if not (rdir / "journal.json").is_file():
                    continue
                prefix = keys.run_prefix(rdir.name)
                await store.upload_dir(str(rdir), prefix)
                try:
                    nodes = json.loads((rdir / "journal.json").read_text(encoding="utf-8")).get("nodes", [])
                except ValueError:
                    nodes = []
                metrics = [n.get("metric") for n in nodes if n.get("metric") is not None
                           and not n.get("is_buggy")]
                await repo.upsert_search_run(db, rdir.name, user_id=user.id, workspace_id=ws.id,
                                             job_id=None, task=None, status="complete",
                                             steps_done=len(nodes), nodes=len(nodes),
                                             best_metric=(max(metrics) if metrics else None),
                                             run_prefix=prefix, report_key=prefix + "report.md",
                                             exported=(rdir / "best_solution.py").is_file())
                for n in nodes:
                    if n.get("id"):
                        await repo.upsert_search_node(db, rdir.name, n)
                n_runs += 1
        await db.commit()
    print(f"imported {n_sessions} sessions and {n_runs} runs into workspace '{workspace_name}'")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    return asyncio.run(_backfill(Path(args.source).resolve(), args.owner, args.workspace))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="swarn-backend")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the API (one process; jobs are subprocesses)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    s.add_argument("--reload", action="store_true")
    s.add_argument("--log-level", default="info")
    s.set_defaults(fn=cmd_serve)
    m = sub.add_parser("migrate", help="alembic upgrade")
    m.add_argument("revision", nargs="?", default="head")
    m.set_defaults(fn=cmd_migrate)
    r = sub.add_parser("revision", help="alembic autogenerate a migration")
    r.add_argument("-m", "--message", required=True)
    r.set_defaults(fn=cmd_revision)
    a = sub.add_parser("make-admin", help="promote a user who has signed in once")
    a.add_argument("email")
    a.set_defaults(fn=cmd_make_admin)
    b = sub.add_parser("backfill", help="import an agent2 checkout's sessions/ and runs")
    b.add_argument("--source", required=True)
    b.add_argument("--owner", required=True, help="email of the user who will own the history")
    b.add_argument("--workspace", default="Imported")
    b.set_defaults(fn=cmd_backfill)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
