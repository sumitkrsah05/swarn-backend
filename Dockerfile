# syntax=docker/dockerfile:1
# swarn-backend API image. The worker is the same image (python -m swarn_backend.worker).
#
# Build from the parent folder so ../agent2 is in the context:
#   docker build -f swarn-backend/Dockerfile -t swarn-backend .
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    MPLBACKEND=Agg

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY agent2 /app/agent2
COPY swarn-backend /app/swarn-backend
WORKDIR /app/swarn-backend
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev || uv sync --no-dev

RUN useradd --create-home --uid 1000 app && mkdir -p /var/lib/swarn/work \
 && chown -R app:app /app /var/lib/swarn
USER app
ENV PATH="/app/swarn-backend/.venv/bin:$PATH" BACKEND_WORK_DIR=/var/lib/swarn/work

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz >/dev/null || exit 1
CMD ["sh", "-c", "swarn-backend migrate && swarn-backend serve --port 8000"]
