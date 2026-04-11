# ---------------------------------------------------------------------------
# Hotel Price Scraper — Dockerfile
# ---------------------------------------------------------------------------
ARG UID=1000
ARG GID=1000

FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy AS base

ARG UID
ARG GID

RUN groupadd --gid ${GID} scraper 2>/dev/null || true \
 && useradd  --uid ${UID} --gid ${GID} \
             --shell /bin/bash --create-home scraper 2>/dev/null || true

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN apt-get update -qq \
 && playwright install-deps chromium \
 && playwright install chromium \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY src/ ./src/

RUN mkdir -p /app/output && chown ${UID}:${GID} /app/output

USER scraper

FROM base AS runtime
ENTRYPOINT ["python", "-u", "src/main.py"]
CMD ["--input", "input.json"]