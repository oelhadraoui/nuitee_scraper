# ===========================================================================
# Dockerfile - Nuitee price competitiveness scraper
# ===========================================================================
# Multi-stage build:
#   builder  - compiles wheels once, so the runtime layer carries no toolchain
#   runtime  - Playwright base + our wheels + source. Slim and cache-friendly.
#
# The Playwright base image version MUST match the pinned playwright package
# in requirements.txt. A mismatch produces the notoriously unhelpful
# "Executable doesn't exist" error at runtime.
# ===========================================================================

ARG PLAYWRIGHT_VERSION=v1.44.0-jammy
ARG UID=1000
ARG GID=1000

# ---------------------------------------------------------------------------
# Stage 1 - builder
# ---------------------------------------------------------------------------
FROM mcr.microsoft.com/playwright/python:${PLAYWRIGHT_VERSION} AS builder

WORKDIR /build

COPY requirements.txt .

# Build wheels for every dependency. Layer is cached until requirements.txt
# changes, so source edits never trigger a dependency rebuild.
RUN pip install --no-cache-dir --upgrade pip wheel \
 && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 - runtime
# ---------------------------------------------------------------------------
FROM mcr.microsoft.com/playwright/python:${PLAYWRIGHT_VERSION} AS runtime

ARG UID
ARG GID

LABEL org.opencontainers.image.title="nuitee-price-scraper" \
      org.opencontainers.image.description="Nuitee vs Booking.com price competitiveness pipeline with full checkout-tunnel extraction" \
      org.opencontainers.image.version="2.0.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Africa/Casablanca

# -- user ------------------------------------------------------------------
# The Playwright image ships `pwuser` on UID 1000. Rename it when the host UID
# collides, otherwise create a dedicated user. This keeps files written to the
# mounted ./output owned by the host user rather than by root.
RUN if id -u ${UID} >/dev/null 2>&1; then \
        EXISTING=$(id -nu ${UID}); \
        usermod -l scraper ${EXISTING} 2>/dev/null || true; \
        groupmod -n scraper $(id -gn ${UID}) 2>/dev/null || true; \
    else \
        groupadd --gid ${GID} scraper 2>/dev/null || true; \
        useradd --uid ${UID} --gid ${GID} --shell /bin/bash --create-home scraper; \
    fi

WORKDIR /app

# -- dependencies from the builder's wheels --------------------------------
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels

# -- Chromium --------------------------------------------------------------
# The base image already carries the browser and its OS libraries; this call
# is a cheap no-op that fails loudly if the versions ever drift apart.
RUN playwright install --with-deps chromium

# -- application source ----------------------------------------------------
COPY --chown=${UID}:${GID} src/     ./src/
COPY --chown=${UID}:${GID} config/  ./config/
COPY --chown=${UID}:${GID} scripts/ ./scripts/
COPY --chown=${UID}:${GID} input.json ./input.json

RUN mkdir -p /app/output && chown -R ${UID}:${GID} /app/output /app

USER scraper

# -- health ----------------------------------------------------------------
# Reports unhealthy when the pipeline's own pre-flight check fails.
HEALTHCHECK --interval=60s --timeout=20s --start-period=30s --retries=3 \
  CMD python -m src.cli doctor >/dev/null 2>&1 || exit 1

ENTRYPOINT ["python", "-u", "-m", "src.cli"]
CMD ["run", "--horizon", "all", "--months", "3"]
