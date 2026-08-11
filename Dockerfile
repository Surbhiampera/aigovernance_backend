# ── Stage 1: build dependencies ───────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m spacy download en_core_web_lg


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="AI Governance Platform" \
      org.opencontainers.image.description="FastAPI governance proxy for AI API traffic" \
      org.opencontainers.image.version="3.0.0"

WORKDIR /app

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY app/ ./app/

# Mount point for per-client licensing (see LICENSING_PACKAGING.md). Same
# image ships to every client — only what's mounted here differs. Created
# and chowned up front so a bind-mounted host dir or a fresh named volume
# is writable by appuser without extra setup on the deploying side.
RUN mkdir -p /app/license

RUN adduser --disabled-password --no-create-home appuser \
    && chown appuser:appuser /app/license
USER appuser

# Azure App Service injects PORT at runtime; default to 8000 for local runs
ENV PORT=8000

# Licensed/packaged deployments only: LICENSE_ENFORCEMENT_ENABLED stays
# false (see app/config.py) unless a per-client deployment turns it on, so
# these path defaults are inert for the standard shared-platform image.
ENV LICENSE_FILE_PATH=/app/license/license.lic \
    LICENSE_PUBLIC_KEY_PATH=/app/license/license_public_key.pem

EXPOSE 8000

# Shell form allows $PORT expansion (Azure overrides PORT at runtime); exec
# replaces the shell with uvicorn so it becomes PID 1 and receives signals directly
CMD exec uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 2
