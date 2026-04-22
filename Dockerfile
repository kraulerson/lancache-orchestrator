# ── Stage 1: builder ─────────────────────────────────────────────
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .
RUN python -m venv /build/.venv \
    && /build/.venv/bin/pip install --no-cache-dir --require-hashes \
       -r requirements.txt

COPY pyproject.toml .
COPY src/ src/
RUN /build/.venv/bin/pip install --no-cache-dir --no-deps .

# ── Stage 2: runtime ────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:${PATH}"

RUN groupadd -r -g 1000 orchestrator \
    && useradd -r -u 1000 -g orchestrator -s /usr/sbin/nologin orchestrator \
    && mkdir -p /var/lib/orchestrator \
    && chown orchestrator:orchestrator /var/lib/orchestrator

COPY --from=builder /build/.venv /app/.venv
COPY --from=builder /build/src /app/src
# Migrations ship as package data inside /app/src/orchestrator/db/migrations/ and
# are loaded at runtime via importlib.resources. No separate COPY required.

WORKDIR /app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import httpx; httpx.get('http://127.0.0.1:8765/api/v1/health').raise_for_status()"]

USER orchestrator

EXPOSE 8765

ENTRYPOINT ["uvicorn", "orchestrator.api.main:app", "--host", "0.0.0.0", "--port", "8765"]
