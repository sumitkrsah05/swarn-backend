# swarn-backend

The hosted backend for the swarn agent: a FastAPI API over **PostgreSQL**
(users, sessions, jobs, every index) and **MinIO** (every file), with
**Google sign-in**. It runs the agent engines from the sibling `agent2`
checkout, one worker subprocess per job, and serves the existing
`swarn-frontend` with its API contract unchanged.

Design and rationale: `../agent2/docs/Backend_Build_Instructions.md`.

## Layout

```
swarn_backend/
  settings.py        BACKEND_* variables (pydantic-settings); SWARN_* pass through to workers
  db/                SQLAlchemy models (models.py), engine, repo.py (every query, user-scoped)
  storage/           MinIO client (s3.py), key layout + file kinds (keys.py)
  auth/              Google OIDC (google.py), tokens, dependencies (cookie or bearer)
  api/               one router per resource: auth, workspaces, jobs, uploads, sessions,
                     runs, workspace_files, data, playbook, health, admin, ws
  jobs/              supervisor.py (queue → worker → events → push), staging.py, protocol.py
  worker/            the subprocess: __main__ sets env, runner.py wires agent2's engines
  data/              parquet-backed /api/data/* (kinds.py, frames.py)
  ws/                per-user websocket fan-out
migrations/          Alembic (async)
deploy/              docker-compose, Caddyfile
```

## Run locally

Prerequisites: Python 3.12 via `uv`, a PostgreSQL and a MinIO reachable from
this machine, Docker if you want the sandbox (`SWARN_SANDBOX=docker`), and
the `agent2` checkout as a sibling directory (it is installed editable).

```bash
cd swarn-backend
cp .env.example .env            # fill in database, MinIO, model endpoint
uv sync --extra dev             # installs agent2 too (torch included — several GB)
uv run swarn-backend migrate    # alembic upgrade head
uv run swarn-backend serve      # http://localhost:8000
```

Without Google credentials, set `BACKEND_DEV_LOGIN=1` and open
`http://localhost:8000/api/auth/dev-login?email=you@example.com` once; the
cookie then works for the frontend on `:3000` (keep
`BACKEND_CORS_ORIGINS=http://localhost:3000`).

Frontend: run `swarn-frontend` with `NEXT_PUBLIC_SWARN_API=http://localhost:8000`
and the small changes listed below.

## Google sign-in

1. Google Cloud console → APIs & Services → Credentials → OAuth client ID,
   type **Web application**. Authorised redirect URI:
   `<BACKEND_PUBLIC_URL>/api/auth/google/callback`.
2. Put the client id and secret in `.env`. Restrict who may sign in with
   `BACKEND_ALLOWED_EMAIL_DOMAINS` (or `BACKEND_ALLOWED_EMAILS`); list the
   first admins in `BACKEND_ADMIN_EMAILS`, or run `swarn-backend make-admin
   <email>` after they sign in.
3. The browser is sent to `GET /api/auth/google/login?redirect_to=/`; the
   callback sets an HttpOnly `swarn_session` cookie and redirects to the
   frontend. `GET /api/auth/me`, `POST /api/auth/logout`.

CLI / MCP (browser-mediated): `POST /api/auth/cli/start` → open `verify_url`
in a browser → poll `POST /api/auth/cli/poll {code}` → store the returned
token → send `Authorization: Bearer <token>`.

## API

Everything in `agent2/agent/web/API.md` is served at the same paths with the
same shapes, scoped to the signed-in user:

- `POST /api/jobs` additionally accepts `workspace_id`; without it the user's
  `Scratch` workspace is used. `data_dir` is `uploads/<batch_id>` (also
  accepted as a bare id or an absolute path ending that way).
- `GET /api/data/*`, `GET /api/workspace/files|file`, `GET /api/sessions`,
  `GET /api/runs` take an optional `workspace_id`; without it the user's most
  recently active workspace is used.
- New: `/api/auth/*`, `/api/workspaces` (CRUD + `GET/PUT /{id}/state` for
  the UI's thread tree), `/api/admin/*`, `GET /healthz`, `GET /readyz`.
- `GET /api/sessions/{id}` returns steps **while the run is still going**.
- `WS /ws/live` authenticates with the cookie (same origin) or
  `?token=` from `POST /api/auth/ws-token`, and only carries the user's own
  jobs. Admins may pass `?all=1`.
- Evaluation routes (`/api/eval/*`) are phase 2 and not served yet.

Errors are `{"detail": "..."}`. State-changing requests must carry an
`Origin` (or `Referer`) matching `BACKEND_PUBLIC_URL` / `BACKEND_FRONTEND_URL`
/ `BACKEND_CORS_ORIGINS`.

## Frontend changes needed

1. `lib/api.ts` (`apiFetch`, `apiFetchText`, `uploadFiles`) and
   `api/client.ts` (`doFetch`): add `credentials: "include"`
   (`xhr.withCredentials = true`).
2. On `ApiError(401)` redirect to `/login`; a `/login` page with a button to
   `${API_BASE}/api/auth/google/login?redirect_to=/`; an account menu from
   `GET /api/auth/me` with Logout (`POST /api/auth/logout`).
3. Send `workspace_id` on job submit and on the dataset / workspace-file /
   session / run reads (one workspace in the UI = one `POST /api/workspaces`).
4. Cross-origin websocket: append `?token=` from `POST /api/auth/ws-token`.

## How a job runs

```
POST /api/jobs ─▶ jobs(queued) ─▶ supervisor: pull workspace from MinIO to work/<job_id>/
   ─▶ python -m swarn_backend.worker (SWARN_DATA_ROOT=work/<job_id>) ─▶ JSON lines on stdout
   ─▶ rows in job_events / session_steps / approvals / datasets / files + websocket frames
   ─▶ exit ─▶ push outputs, traces, run dir to MinIO ─▶ jobs(complete|failed|cancelled)
```

Approve and cancel are lines on the worker's stdin. An API restart marks
running jobs failed (`server restarted`). The sandbox mounts the job's
staging workspace, so with Docker the work directory must be a host path
visible at the same path inside the API container (see `deploy/`).

## Operations

- `swarn-backend migrate` / `swarn-backend revision -m "..."` (autogenerate).
- `swarn-backend backfill --source ../agent2 --owner you@example.com` imports
  an agent2 checkout's `sessions/` and AIDE runs into a workspace.
- Backups: `pg_dump` + `mc mirror` of the bucket. The API host keeps nothing
  after a job finishes (unless `BACKEND_KEEP_FAILED_WORKDIRS=1`).
- Tests: `uv run pytest`.
