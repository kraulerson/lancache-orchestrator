# ── Stage 1: builder ─────────────────────────────────────────────
FROM python:3.12-slim@sha256:520153e2deb359602c9cffd84e491e3431d76e7bf95a3255c9ce9433b76ab99a AS builder

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
FROM python:3.12-slim@sha256:520153e2deb359602c9cffd84e491e3431d76e7bf95a3255c9ce9433b76ab99a AS runtime

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:${PATH}"

RUN groupadd -r -g 1000 orchestrator \
    && useradd -r -u 1000 -g orchestrator -s /usr/sbin/nologin orchestrator \
    && mkdir -p /var/lib/orchestrator \
    && chown orchestrator:orchestrator /var/lib/orchestrator

COPY --from=builder /build/.venv /app/.venv
# pip console scripts hardcode the build-stage shebang
# (#!/build/.venv/bin/python), which doesn't exist in the runtime image — so
# uvicorn AND the bundled `orchestrator-cli` console script fail with ENOENT.
# Rewrite the shebang of every text script to the final venv path. (Restricting
# to grep-matched files avoids touching the `python` symlinks / binaries.)
RUN grep -rlI '^#!/build/\.venv/bin/python' /app/.venv/bin/ \
    | xargs -r sed -i '1s|/build/\.venv/bin/python|/app/.venv/bin/python|'
COPY --from=builder /build/src /app/src
# Migrations ship as package data inside /app/src/orchestrator/db/migrations/ and
# are loaded at runtime via importlib.resources. No separate COPY required.

# The only persistent mount point. Declared as a VOLUME so operators running
# with `--read-only` (recommended — PROJECT_BIBLE threat model) get a clean
# error if they mount the filesystem read-only without also mounting this
# path. Lets us safely add `--read-only` to the compose bundle later.
# Addresses UAT-1 adversarial F7.
VOLUME ["/var/lib/orchestrator"]

WORKDIR /app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import httpx; httpx.get('http://127.0.0.1:8765/api/v1/health').raise_for_status()"]

USER orchestrator

EXPOSE 8765

# Bind ORCH_API_HOST, defaulting to loopback (matches Settings.api_host) so the
# trigger endpoints are NOT exposed to the LAN by default and the non-loopback
# boot warning only fires when an operator deliberately opts in (UAT-11 F-INT-3).
# For LAN access, run with host networking or set ORCH_API_HOST=0.0.0.0.
# `python -m uvicorn` (not the `uvicorn` console script) so the entrypoint is
# independent of the console-script shebang; binds ORCH_API_HOST, default
# loopback (UAT-11 F-INT-3).
ENTRYPOINT ["sh", "-c", "exec python -m uvicorn orchestrator.api.main:app --host \"${ORCH_API_HOST:-127.0.0.1}\" --port \"${ORCH_API_PORT:-8765}\""]
