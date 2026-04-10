# Hotel Price Comparator — Nuitee vs Booking.com

Async Playwright scraper that compares hotel prices between a **Nuitee white-label portal** and **Booking.com** across world cities, writing results to `data.csv` hotel-by-hotel so no data is lost on interruption. Built to run overnight in a Docker container on an Oracle Cloud server.

---

## How it works

```
input.json  →  Nuitee portal (scroll all cards)  →  Booking.com (per hotel)  →  data.csv
```

1. Reads one or more search configs from `input.json`
2. For each config, opens the Nuitee portal, fills destination / dates / guests, scrolls until all hotel cards are loaded
3. For every hotel found, searches Booking.com with the same parameters and extracts the MAD price
4. Appends a row to `data.csv` immediately after each hotel — if the script crashes mid-run, all previous rows are already saved

---

## Timeout strategy

All timeouts live in a single `T` dictionary at the top of `scraper.py`. They are tuned for a remote Oracle server with variable latency:

| Group | Timeout | Why |
|---|---|---|
| `page_load` | 60 s | Full page navigation — remote servers can be slow |
| `network_idle` | 60 s | Nuitee portal waits for all XHR before showing results |
| `first_card` | 30 s | Booking.com property cards may load after JS hydration |
| `selector_wait` | 30 s | Nuitee hotel cards wait |
| `scroll_settle` | 2 s | After each scroll, give lazy-loader time to fetch next batch |
| `element_visible` | 5 s | Per-element checks (price, buttons) |
| `after_goto` | 2 s | Dwell on Booking homepage — reduces cold-session fingerprinting |

To tune for a faster network, reduce `scroll_settle` and `after_goto`. To handle an extremely slow server, increase `page_load` and `first_card`.

---

## Project structure

```
hotel-price-comparator/
├── scraper.py          # Main script
├── input.json          # Search configs (50 world cities pre-loaded)
├── Dockerfile          # Container definition
├── requirements.txt
├── README.md
└── data.csv            # Output (auto-created on first run)
```

---

## Running locally

### Prerequisites

- Python 3.11+
- pip

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install Chromium
playwright install chromium

# 4. Run
python scraper.py
```

---

## Running in Docker (Oracle server)

### Build the image

```bash
docker build -t hotel-scraper .
```

### Run with persistent output

```bash
# Create a local output folder first
mkdir -p $(pwd)/output

# Run the container — data.csv will be written to ./output/
docker run --rm \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/input.json:/app/input.json \
  hotel-scraper
```

### Run detached (overnight job)

```bash
docker run -d \
  --name hotel-scraper-run \
  --restart on-failure:3 \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/input.json:/app/input.json \
  hotel-scraper

# Watch live output
docker logs -f hotel-scraper-run

# Check progress
tail -f output/data.csv
```

### Update input and re-run without rebuilding

```bash
# Edit input.json locally, then:
docker run --rm \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/input.json:/app/input.json \
  hotel-scraper
```

---

## Configuration — `input.json`

Each object in the array is one city/date search run.

```json
[
  {
    "city": "Madrid",
    "checkin": "2026-09-09",
    "checkout": "2026-09-10",
    "adults": 4,
    "rooms": 1
  }
]
```

| Field      | Type   | Required | Description                    |
|------------|--------|----------|--------------------------------|
| `city`     | string | ✅       | Destination name               |
| `checkin`  | string | ✅       | Check-in date `YYYY-MM-DD`     |
| `checkout` | string | ✅       | Check-out date `YYYY-MM-DD`    |
| `adults`   | int    | ❌       | Adults per room (default: `2`) |
| `rooms`    | int    | ❌       | Number of rooms (default: `1`) |

The included `input.json` covers **50 world cities** across Europe, Middle East, Africa, Americas, and Asia-Pacific.

---

## Output — `data.csv`

| Column | Description |
|---|---|
| Hotel Name | Name as listed on Nuitee |
| City | From `input.json` |
| Check-in | From `input.json` |
| Check-out | From `input.json` |
| Adults | From `input.json` |
| Rooms | From `input.json` |
| Nuitee Price (MAD) | Scraped from the Nuitee portal |
| Booking Price (MAD) | Scraped from Booking.com, or `N/A / SOLD OUT` |
| Price Difference (MAD) | Booking − Nuitee (e.g. `+180.00`) |
| Booking URL | Final Booking.com search URL for that hotel |

> `data.csv` is appended to on every run. Delete it manually for a clean slate.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Many `N/A / SOLD OUT` results | Increase `first_card` and `scroll_settle` in the `T` dict |
| Script hangs on date picker | Calendar selectors may have changed — run locally with `headless=False` to inspect |
| CAPTCHA on Booking.com | Increase `after_goto` to `4_000`–`6_000` ms |
| Only 8–10 hotels from Nuitee | Increase `scroll_settle` to `3_000` ms on slow connections |
| Container OOM on Oracle free tier | Add `--memory 2g` flag to `docker run` |