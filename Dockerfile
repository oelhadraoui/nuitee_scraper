# ── Stage 1: Playwright + Chromium base ──────────────────────────────────────
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Set working directory
WORKDIR /app

# Install Python dependencies first (layer cache friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (Chromium only — saves ~500 MB vs full install)
RUN playwright install chromium --with-deps

# Copy project files
COPY nuitee_scraper.py .
COPY input.json .

# Output directory — mount a host volume here to persist data.csv
RUN mkdir -p /app/output
ENV OUTPUT_DIR=/app/output

# Run as non-root for Oracle server security policies
RUN useradd -m scraper
RUN chown -R scraper:scraper /app
USER scraper

CMD ["python", "nuitee_scraper.py"]