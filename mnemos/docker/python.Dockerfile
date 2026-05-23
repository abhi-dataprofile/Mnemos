# Python orchestrator image.
#
# Multi-stage:
#   1. builder installs the project into a virtualenv. Build tools stay in
#      this stage and never reach the runtime image.
#   2. runtime copies just the venv + application source and runs as an
#      unprivileged user.
#
# Compose runs both the API and the RQ worker off this same image — only the
# CMD differs. See docker/docker-compose.yml.

# ---------- builder ---------------------------------------------------------
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
 && apt-get install --no-install-recommends -y build-essential git \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV"

WORKDIR /src

# Copy only pyproject + lockfile first so dependency installs stay cacheable
# across application-code edits.
COPY python/pyproject.toml ./
COPY python/README.md ./README.md
# `poetry.lock` / `uv.lock` optional — pip handles their absence.

# Install the package itself last, after deps are warm.
COPY python/ ./
RUN pip install --upgrade pip \
 && pip install . \
 && python -m compileall -q /opt/venv


# ---------- runtime ---------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PORT=8000

# libpq is the only system dep asyncpg/sqlalchemy need at runtime. Adding
# `curl` so the compose healthcheck doesn't have to install it.
RUN apt-get update \
 && apt-get install --no-install-recommends -y libpq5 curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --shell /usr/sbin/nologin --uid 10001 mnemos

COPY --from=builder --chown=mnemos:mnemos /opt/venv /opt/venv
COPY --from=builder --chown=mnemos:mnemos /src /app

WORKDIR /app
USER mnemos

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Default command runs the API. The RQ worker overrides CMD in compose.
CMD ["uvicorn", "codereview.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
