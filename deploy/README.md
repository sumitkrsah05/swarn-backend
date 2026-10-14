# Deploying swarn on one VM

One VM, one origin. Caddy terminates HTTPS and routes `/api`, `/ws`, `/docs`
to the API container and everything else to the Next.js container. Postgres
and MinIO run as containers with named volumes. Agent jobs run as
subprocesses of the API container, or in throw-away Docker sandboxes through
the mounted Docker socket.

```
https://<host>/            → web   (swarn-frontend, :3000)
https://<host>/api/*, /ws  → api   (swarn-backend, :8000) → postgres:5432, minio:9000
```

## 0. What you need before you start

| Need | Why |
| --- | --- |
| Ubuntu 22.04/24.04 VM, 4 vCPU, 8 GB RAM, 60 GB disk | the API image is ~3 GB; jobs and sandboxes need RAM |
| A hostname under a real domain, e.g. `swarn.esds.co.in`, resolving to the VM (internal DNS is fine) | Google OAuth rejects raw IPs and non-public TLDs; the session cookie is `Secure`, so the site must be HTTPS |
| Ports 80 and 443 open to your users | Caddy serves both |
| A Google OAuth **Web application** client | sign-in; dev-login is disabled off localhost |
| The model endpoint reachable from the VM (`SWARN_DEPLOYED_*`) | the workers call it |
| Outbound internet from the VM during build and first job | image pulls, `pip` inside sandboxes |

## 1. Prepare the VM

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
docker compose version            # v2.24 or newer

sudo mkdir -p /opt/swarn /var/lib/swarn/work
sudo chown $USER /opt/swarn
sudo chown 1000:1000 /var/lib/swarn/work     # uid 1000 = the `app` user inside the api image
sudo ufw allow 80/tcp && sudo ufw allow 443/tcp   # if ufw is active
```

## 2. Put the three repos side by side

The compose file builds from the parent folder, so the layout is fixed:

```
/opt/swarn/
├── agent2/            # agent engines (the api image copies it in)
├── swarn-backend/     # this repo; deploy/ is here
└── swarn-frontend/    # the Next.js app
```

```bash
cd /opt/swarn
git clone https://github.com/hypernullvoid-spec/agent2.git agent2
git clone https://github.com/sumitkrsah05/swarn-backend.git swarn-backend
git clone https://github.com/sumitkrsah05/Swarn-Frontend.git swarn-frontend

# keep the build context small (the parent folder is the context)
cp swarn-backend/deploy/root.dockerignore /opt/swarn/.dockerignore
```

Copying from your laptop instead of cloning also works; exclude `.venv`,
`node_modules`, `.next`, `runs` and `work`, which the `.dockerignore` above
would otherwise have to skip on every build.

## 3. Configure

Two env files. Compose reads the first; the API container reads the second.

```bash
cd /opt/swarn/swarn-backend/deploy
cp .env.example .env            # SITE_ADDRESS, POSTGRES_PASSWORD, MINIO_ROOT_USER, MINIO_ROOT_PASSWORD
cp ../.env.example ../.env      # BACKEND_* and SWARN_* (the API's own settings)
```

`deploy/.env`:

```
SITE_ADDRESS=swarn.esds.co.in        # the hostname users type; Caddy gets a certificate for it
POSTGRES_PASSWORD=<long random>
MINIO_ROOT_USER=swarn
MINIO_ROOT_PASSWORD=<long random>
```

`swarn-backend/.env`, the values that differ from local development
(compose overrides the database URL, the S3 endpoint and keys, the work dir,
the cookie flag and the sandbox mode, so leave those lines alone):

```
BACKEND_PUBLIC_URL=https://swarn.esds.co.in
BACKEND_FRONTEND_URL=https://swarn.esds.co.in
BACKEND_GOOGLE_CLIENT_ID=<from the Google console>
BACKEND_GOOGLE_CLIENT_SECRET=<from the Google console>
BACKEND_ALLOWED_EMAIL_DOMAINS=esds.co.in
BACKEND_ADMIN_EMAILS=you@esds.co.in
BACKEND_SECRET_KEY=<python3 -c "import secrets; print(secrets.token_urlsafe(48))">
BACKEND_DEV_LOGIN=0
BACKEND_CORS_ORIGINS=              # empty: same origin behind Caddy
BACKEND_EXPOSE_DOCS=0              # 1 to serve /docs
SWARN_DEPLOYED_MODEL=...
SWARN_DEPLOYED_BASE_URL=...
SWARN_DEPLOYED_API_KEY=...
```

In the Google Cloud console add the authorised redirect URI
`https://swarn.esds.co.in/api/auth/google/callback` and the authorised
JavaScript origin `https://swarn.esds.co.in`.

### TLS

Caddy obtains a Let's Encrypt certificate automatically when the hostname
is publicly resolvable and ports 80/443 are reachable from the internet.
For an intranet-only VM add one line to `Caddyfile` right under `encode gzip`:

```
tls internal
```

Caddy then signs with its own CA. Export it once with
`docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt .` and
install it on users' machines, or the browser shows a certificate warning
(sign-in still works after accepting it).

### Sandbox: subprocess or Docker

`docker-compose.yml` sets `SWARN_SANDBOX: docker`, which runs each job's
generated code in a `python:3.11-slim` container through the mounted Docker
socket. The `app` user inside the api image (uid 1000) must be allowed to use
that socket, so add the host's docker group id to the api service:

```bash
stat -c %g /var/run/docker.sock      # e.g. 988
```

```yaml
# deploy/docker-compose.override.yml
services:
  api:
    group_add: ["988"]               # the number from stat
```

To start without sandboxes (simplest first run; code executes inside the api
container as `app`), use this override instead:

```yaml
services:
  api:
    environment:
      SWARN_SANDBOX: subprocess
```

## 4. Build and start

```bash
cd /opt/swarn/swarn-backend/deploy
docker compose up -d --build         # first build 10–20 minutes (torch wheels)
docker compose ps                    # api: healthy, postgres: healthy
docker compose logs -f api           # "swarn-backend 0.1.0 ready on https://swarn.esds.co.in"
```

The api container runs `swarn-backend migrate` before `serve` on every
start, so the schema is created and kept at head without a manual step.
`ensure_bucket` creates the `swarn` bucket on the first request that needs it.

## 5. Verify

```bash
curl -s https://swarn.esds.co.in/healthz          # {"status":"ok"}
curl -sI https://swarn.esds.co.in/ | head -1      # HTTP/2 200
```

Open `https://swarn.esds.co.in`, sign in with a Google account in the allowed
domain, then from the VM:

```bash
docker compose exec api swarn-backend make-admin you@esds.co.in   # if not listed in BACKEND_ADMIN_EMAILS
```

As an admin, `https://swarn.esds.co.in/readyz` must show
`"database": true, "object_store": true`. Submit a small job from the UI and
watch `docker compose logs -f api`.

## 6. Bring your laptop data along (optional)

Run on the laptop, then on the VM. Rows and objects must come from the same
moment, since `object_key` columns reference the bucket.

```bash
# laptop
docker exec swarn-postgres pg_dump -U swarn -d swarn_backend --format=custom > swarn.dump
docker exec aistor-server mc mirror --quiet local/swarn /tmp/b && docker cp aistor-server:/tmp/b ./bucket
scp -r swarn.dump bucket user@VM:/opt/swarn/

# VM, from swarn-backend/deploy
docker compose exec -T postgres pg_restore -U swarn -d swarn_backend --clean --if-exists < /opt/swarn/swarn.dump
docker compose cp /opt/swarn/bucket minio:/tmp/bucket
docker compose exec minio sh -c 'mc alias set local http://127.0.0.1:9000 $MINIO_ROOT_USER $MINIO_ROOT_PASSWORD && mc mb -p local/swarn && mc cp --recursive /tmp/bucket/ local/swarn/'
docker compose exec api python scripts/storage_check.py      # 0 missing
```

## 7. Day-to-day

| Task | Command (from `swarn-backend/deploy`) |
| --- | --- |
| Update to the latest code | `git -C /opt/swarn/agent2 pull && git -C /opt/swarn/swarn-backend pull && git -C /opt/swarn/swarn-frontend pull && docker compose up -d --build` |
| Logs | `docker compose logs -f api` (or `web`, `caddy`, `postgres`, `minio`) |
| Postgres shell | `docker compose exec postgres psql -U swarn -d swarn_backend` |
| MinIO CLI | `docker compose exec minio mc alias set local http://127.0.0.1:9000 $MINIO_ROOT_USER $MINIO_ROOT_PASSWORD` then `mc ls local/swarn` |
| Backup | `docker compose exec -T postgres pg_dump -U swarn -d swarn_backend --format=custom > backup.dump`; `mc mirror` of the bucket as in step 6 |
| Rows vs objects check | `docker compose exec api python scripts/storage_check.py` |
| Restart one service | `docker compose restart api` |
| Stop everything, keep data | `docker compose down` (volumes `pgdata`, `minio-data`, `caddy-data` stay) |

Changing `SITE_ADDRESS`, `BACKEND_PUBLIC_URL` or the Google client requires
only `docker compose up -d` (api and caddy re-read their env); nothing is
baked into the web image because its API base is the relative `/`.
