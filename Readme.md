# 🏨 Hotel Price Scraper

A production-grade, headless-browser scraper that compares hotel prices between
**Nuitee** and **Booking.com** across dozens of cities simultaneously. Built with
Python + Playwright, containerised with Docker, and optimised for Oracle Cloud
Free Tier (Ampere A1 ARM64).

---

## Table of Contents

1. [What it does](#what-it-does)
2. [Architecture](#architecture)
3. [Project structure](#project-structure)
4. [Quick start — Local](#quick-start--local)
5. [Quick start — Docker](#quick-start--docker)
6. [Input format](#input-format)
7. [Environment variables](#environment-variables)
8. [Output format](#output-format)
9. [Makefile reference](#makefile-reference)
10. [Troubleshooting](#troubleshooting)

---

## What it does

For each city in `input.json` the scraper:

1. Opens **Nuitee.com** in an isolated browser context, logs in, and searches
   for hotels on the requested dates.
2. Harvests every hotel name and nightly price from the results page —
   even when the list contains 300+ entries rendered via a virtualised DOM.
3. Opens **Booking.com** in a second isolated context and performs the same
   search.
4. Merges the two datasets on hotel name and writes a CSV with columns:
   `city, hotel_name, nuitee_price, booking_price, diff, diff_pct`.

---

## Architecture

### Dual-source price collection

```
input.json
    │
    ├─▶ NuiteeScraper ──▶ API interception (primary)
    │                 └─▶ DOM Scroll & Harvest (fallback)
    │
    └─▶ BookingScraper ─▶ DOM Scroll & Harvest
    │
    └─▶ Merger ──▶ output/results_YYYY-MM-DD.csv
```

### API Interception (Nuitee primary path)

The scraper registers a Playwright `route` handler that intercepts XHR/fetch
responses matching `/api/hotels` or `/v2/search`. When the browser fires the
hotel-list request, the handler parses the JSON payload directly — no DOM
interaction required. This path is instantaneous and returns 100 % of results
in a single pass.

### Scroll & Harvest (DOM fallback)

Used when the API path returns no data (auth token rotation, CDN changes, etc.).

**The challenge:** Nuitee uses a *virtualised list* (PrimeVue `VirtualScroller`).
Only ~12–15 cards exist in the DOM at any moment; as you scroll, nodes are
recycled. A naïve `locator.all()` therefore never sees more than one screen-full
of data.

**The solution — leapfrog harvesting:**

```
while unique_count < total_expected:
    1. Read the last 20 DOM nodes  (tail-window, O(1) per pass)
    2. For each unseen card → extract name + price → add to seen_names set
    3. If new cards were found → scroll SCROLL_MIN (400 px) — stay precise
    4. If no new cards        → increase scroll step by 200 px (up to 1800 px)
    5. If 6 consecutive empty passes → list exhausted → break
```

Key complexity properties:

| Operation | Naïve approach | Optimised approach |
|---|---|---|
| Cards inspected per pass | All N cards | Last 20 cards (constant) |
| Duplicate check | O(N) list scan | O(1) set lookup |
| Overall complexity | O(N²) | O(N) |

### Browser context isolation

Each city runs in its own `browser.new_context()`. Contexts share the same
browser process (saving ~200 MB RAM vs one process per city) but have
completely separate cookies, storage, and network state. This prevents session
bleed between concurrent city runs.

---

## Project structure

```
.
├── src/
│   ├── main.py              # Entry point — reads input.json, fans out tasks
│   ├── nuitee_scraper.py    # Nuitee login + harvest logic
│   ├── booking_scraper.py   # Booking.com harvest logic
│   ├── merger.py            # Joins the two datasets, writes CSV
│   └── config.py            # Timeouts, selectors, constants
├── output/                  # CSV results + per-run logs (git-ignored)
├── input.json               # City search list (see Input format below)
├── .env                     # Secrets — never commit this
├── .env.example             # Template to copy
├── Dockerfile
├── docker-compose.yml
├── Makefile
├── requirements.txt
└── README.md
```

---

## Quick start — Local

### Prerequisites

- Python 3.11+
- Node.js is **not** required — Playwright's Python package bundles everything.

```bash
# 1. Clone
git clone https://github.com/oelhadraoui/nuitee_scraper.git
cd hotel-price-scraper

# 2. Virtual environment
python -m venv .venv
source .venv/bin/activate

# 3. Python dependencies
pip install -r requirements.txt

# 4. Playwright browser + system libraries
playwright install-deps chromium
playwright install chromium

# 5. Configuration
nano .env 

# 6. Run
python src/main.py --input input.json
```

Results are written to `output/results_YYYY-MM-DD.csv`.

---

## Quick start — Docker

### Prerequisites

- Docker 24+ with the Compose plugin (`docker compose version`)
- `make` (pre-installed on macOS/Linux; Windows users: use Git Bash or WSL)

```bash
# 1. Configure
cp .env.example .env
$EDITOR .env

# 2. Build the image
make build

# 3. Run
make start
# or
make run

# 4. View results
ls output/
```

---

## Input format

`input.json` is an array of search jobs. Each object represents one city search:

```jsonc
[
  {
    "city":     "Madrid",       // Free-text city name
    "checkin":  "2026-09-01",   // ISO-8601 date (YYYY-MM-DD)
    "checkout": "2026-09-02",   // Must be after checkin
    "adults":   2               // Number of adult guests (integer ≥ 1)
  },
  {
    "city":     "London",
    "checkin":  "2026-09-01",
    "checkout": "2026-09-02",
    "adults":   2
  }
]
```

**Constraints:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `city` | string | ✅ | Matched against the site's autocomplete |
| `checkin` | string | ✅ | `YYYY-MM-DD` |
| `checkout` | string | ✅ | `YYYY-MM-DD`, must be > `checkin` |
| `adults` | integer | ✅ | 1–9 |

---

## Environment variables

this is how `.env` should looks like:

```dotenv
# ── Nuitee link ─────────────────────────────────────────────────
NUITEE_URL="https://amine-sadik-2911w.nuitee.link/"
```

---

## Output format

`output/results_YYYY-MM-DD.csv`:

```
city,hotel_name,nuitee_price,booking_price,diff,diff_pct
Madrid,Hotel Gran Via,558.0,612.0,-54.0,-8.82
Madrid,Ibis Madrid Centro,210.0,198.0,12.0,6.06
London,The Savoy,950.0,,, 
```

- `diff` = `nuitee_price − booking_price` (negative → Nuitee is cheaper)
- `diff_pct` = `diff / booking_price × 100`
- Empty `booking_price` means the hotel was not found on Booking.com

---

## Makefile reference

| Command | Description |
|---|---|
| `make build` | Build (or rebuild) the Docker image |
| `make start` | Run scraper in background, tail logs |
| `make run` | Run in foreground — exits when done |
| `make stop` | Stop the container (preserves output) |
| `make logs` | Tail last 200 lines + live stream |
| `make shell` | Open bash inside the container |
| `make clean` | Stop + remove containers + clear `output/` |
| `make nuke` | `clean` + remove image layers |

---
