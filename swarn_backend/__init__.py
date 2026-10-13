"""swarn-backend: the hosted API for the swarn agent.

Layout
  settings.py   every BACKEND_* variable
  db/           SQLAlchemy models, engine, repository (all queries live here)
  storage/      MinIO/S3 client, object key layout
  auth/         Google OIDC sign-in, cookie + bearer sessions, dependencies
  api/          one router per resource
  jobs/         the supervisor that runs one worker subprocess per job
  worker/       the subprocess: imports agent2, runs one job, speaks JSON lines
  data/         dataset (parquet) serving without importing agent2
  ws/           websocket fan-out per user
"""

__version__ = "0.1.0"
