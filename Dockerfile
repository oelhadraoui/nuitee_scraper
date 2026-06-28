# ---------------------------------------------------------------------------
# Hotel Price Scraper — Dockerfile
# ---------------------------------------------------------------------------
ARG UID=1000
ARG GID=1000

FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy AS base

ARG UID
ARG GID

# ── User Setup ─────────────────────────────────────────────────────────────
# Playwright images pre-create a user 'pwuser' on UID/GID 1000.
# This block renames it if it exists, or creates 'scraper' if it doesn't.
RUN if id -u ${UID} >/dev/null 2>&1; then \
        EXISTING_USER=$(id -nu ${UID}); \
        usermod -l scraper ${EXISTING_USER} && \
        groupmod -n scraper $(id -ng scraper) 2>/dev/null || true; \
    else \
        groupadd --gid ${GID} scraper 2>/dev/null || true \
        && useradd --uid ${UID} --gid ${GID} --shell /bin/bash --create-home scraper; \
    fi

WORKDIR /app

# ── Python Dependencies ────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Playwright/Chromium Setup ──────────────────────────────────────────────
RUN apt-get update -qq \
 && playwright install-deps chromium \
 && playwright install chromium \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── Source & Permissions ───────────────────────────────────────────────────
COPY src/ ./src/

# Ensure the output directory belongs to the correct UID/GID
RUN mkdir -p /app/output && chown -R ${UID}:${GID} /app/output

USER scraper

# ── Runtime Stage ──────────────────────────────────────────────────────────
FROM base AS runtime
ENTRYPOINT ["python", "-u", "src/main.py"]
CMD ["--input", "input.json"]