"""End-to-end smoke test against a running backend (dev-login mode).

    uv run python scripts/smoke.py --base http://localhost:8000 --email you@example.com \
        --task "Load the CSV and describe it" [--csv path.csv] [--method react]

Signs in, uploads a CSV, submits a job, follows events until it ends, then
reads the session, datasets and workspace files.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8000")
    p.add_argument("--email", default="dev@example.com")
    p.add_argument("--task", default="Load the uploaded CSV, print its shape and column names, "
                                     "and finish with a one-line summary. Do not ask questions.")
    p.add_argument("--csv")
    p.add_argument("--method", default="react")
    p.add_argument("--timeout", type=int, default=900)
    args = p.parse_args()

    c = httpx.Client(base_url=args.base, timeout=60, follow_redirects=False,
                     headers={"Origin": args.base})
    r = c.get("/api/auth/dev-login", params={"email": args.email, "redirect_to": "/"})
    assert r.status_code == 302, r.text
    me = c.get("/api/auth/me").json()
    print("signed in as", me["email"], "role", me["role"])

    csv = args.csv
    if csv:
        with open(csv, "rb") as fh:
            up = c.post("/api/upload", files=[("files", (csv.rsplit("/", 1)[-1], fh, "text/csv"))])
    else:
        body = "region,revenue,units\nnorth,10.5,3\nsouth,20,5\neast,7.25,1\nwest,1,9\n"
        up = c.post("/api/upload", files=[("files", ("sales.csv", body, "text/csv"))])
    assert up.status_code == 200, up.text
    print("uploaded", up.json())

    job = c.post("/api/jobs", json={"task": args.task, "method": args.method,
                                    "data_dir": up.json()["data_dir"]})
    assert job.status_code == 200, job.text
    jid = job.json()["id"]
    print("job", jid, "workspace", job.json()["workspace_id"])

    since, t0 = 0, time.time()
    while time.time() - t0 < args.timeout:
        d = c.get(f"/api/jobs/{jid}", params={"since": since}).json()
        for ev in d["events"]:
            keep = {k: v for k, v in ev.items() if k not in ("ts",)}
            print(f"  [{ev['type']}] {json.dumps(keep, default=str)[:300]}")
            if ev["type"] == "approval_request":
                ans = ev.get("default") or (ev.get("options") or ["yes"])[0]
                print("  -> answering", ans)
                c.post(f"/api/jobs/{jid}/approve", json={"request_id": ev["request_id"], "answer": ans})
        since = d["n_events"]
        if d["status"] in ("complete", "failed", "cancelled"):
            print("final:", d["status"], "error:", d.get("error"))
            print("result:", json.dumps(d.get("result"), default=str)[:800])
            break
        time.sleep(2)
    else:
        print("timed out; cancelling")
        c.post(f"/api/jobs/{jid}/cancel")
        return 1

    d = c.get(f"/api/jobs/{jid}").json()
    if d.get("session_id"):
        s = c.get(f"/api/sessions/{d['session_id']}").json()
        print("session steps:", len(s.get("steps", [])), "outcome:", s.get("outcome"))
    print("datasets:", [x["name"] for x in c.get("/api/data/datasets").json()["datasets"]])
    print("files:", c.get("/api/workspace/files").json())
    print("sessions:", len(c.get("/api/sessions").json()["sessions"]))
    return 0 if d["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
