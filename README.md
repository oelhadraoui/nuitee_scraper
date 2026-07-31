# Nuitée × Booking.com: Price Competitiveness Pipeline

**Version 2.4.** Full checkout-tunnel price extraction, live-verified
against both sites, national/international input splitting, and a hardened
Booking.com matching pipeline (destination-resolution verification, fuzzy
card re-check, per-card unavailability check).

Scrapes the **true final payable price** from Nuitée's reservation tunnel
(not the advertised list price), matches each hotel on Booking.com, and
measures price competitiveness from today's date at two booking horizons
(about a week out, and about three months out), each tested at two stay
lengths (3 and 10 nights by default).

**The problem it solves.** A hotel's price on Nuitée's results page isn't
what a guest actually pays: taxes and fees get added later, at checkout. A
naive scraper comparing that incomplete price against Booking's complete
price makes Nuitée look artificially cheaper. This tool clicks all the way
through Nuitée's booking flow to the screen right before payment, reads the
real total there, and only then compares it against Booking.com. It never
places a real booking or enters payment details anywhere (see
[Safety](#13-safety--compliance)).

**Nothing is silently dropped.** A hotel that can't be matched on
Booking.com, or a price that looks wrong, is kept in the output but clearly
flagged as "not clean" rather than thrown away, so you can always see how
much data is usable and why the rest isn't.

---

## Contents

1. [Quick start](#1-quick-start)
2. [Running a subset (single offset / single duration)](#2-running-a-subset-single-offset--single-duration)
3. [Morocco vs. international input files](#3-morocco-vs-international-input-files)
4. [Booking horizons and stay durations](#4-booking-horizons-and-stay-durations)
5. [Data quality gate](#5-data-quality-gate)
6. [Architecture](#6-architecture)
7. [CLI reference](#7-cli-reference)
8. [Make targets](#8-make-targets)
9. [Configuration](#9-configuration)
10. [Output schema](#10-output-schema)
11. [Troubleshooting](#11-troubleshooting)
12. [Testing](#12-testing)
13. [Safety & compliance](#13-safety--compliance)

---

## 1. Quick start

### Docker (recommended)

```bash
cp .env.example .env          # adjust NUITEE_URL if needed
make build
make doctor                   # pre-flight: config, selectors, storage
make run-smoke                # 2 cities x 4 windows, verifies the tunnel works
make run-full-pipeline        # the real run
```

### Local

```bash
make setup                    # installs deps + Chromium
python -m src.cli doctor
python -m src.cli run --only-offset all
```

### Recommended first run

Never launch a multi-day job blind:

```bash
make run-smoke                                        # ~5 min, is the tunnel alive?
make report                                           # inspect the rejection flags
python -m src.cli run --cities "Casablanca,Marrakech" \
    --hotels-per-city 10                              # ~1 h, is the yield sane?
make run-full-pipeline                                # full run
```

If `make run-smoke` yields zero clean rows, the tunnel or Booking selectors
need tuning (`config/selectors.yaml`). Don't scale up first.

---

## 2. Running a subset (single offset / single duration)

The full pipeline always covers every offset × duration combination, but any
single run can be scoped down to exactly one of each. This is useful for a
fast targeted re-run, a dedicated container per combination, or just testing
one slice at a time.

### Docker

```bash
# short offset (~1 week out), 3-night stays only, its own output folder
docker compose run --rm scraper run \
    --only-offset short --durations 3 --output output/short_3n

# long offset (~3 months out), 10-night stays only, detached
docker compose run -d --name nuitee_long_10n scraper run \
    --only-offset long --durations 10 --output output/long_10n
```

Equivalent Make targets (`DURATIONS` accepts a single value or a comma list):

```bash
make run-offset-short DURATIONS=3
make run-offset-long DURATIONS=10
```

### Local

```bash
python -m src.cli run --only-offset short --durations 3 --output output/short_3n
python -m src.cli run --only-offset long --durations 10 --output output/long_10n
```

Add `--cities "Casablanca,Rabat"` or `--reference-date 2026-08-15` to narrow
further. `--limit-windows N` caps how many search windows run at all, for a
quick sanity check before committing to a full pass.

---

## 3. Morocco vs. international input files

`scripts/generate_input.py` ships preset city lists so a run can be scoped to
domestic or international inventory without hand-editing a city list:

```bash
python scripts/generate_input.py --preset morocco --output input_morocco.json
python scripts/generate_input.py --preset europe --preset mena --preset world \
    --output input_international.json
```

Then point either run at the file you want:

```bash
python -m src.cli run --input input_morocco.json
python -m src.cli run --input input_international.json
```

**Docker note:** `docker-compose.yml` only mounts files it explicitly lists,
so `input_morocco.json` / `input_international.json` are declared there by
name alongside `input.json`. `make` targets generate both automatically the
first time they're needed (see `_ensure_input` in the Makefile), so this is
invisible if you're using `make`. Running `docker compose run` directly
instead, generate them yourself first (the two commands above) or Docker
fills the mount with an empty directory and the CLI reports "Input file not
found" even though the file exists right next to it on the host.

Every output row is also tagged with a **`Market Scope`** column
(`National (Morocco)` / `International`), computed automatically from the
city name, so the two datasets can be run separately, or merged back
together and still be told apart. See [§10](#10-output-schema).

---

## 4. Booking horizons and stay durations

Every hotel is priced from today at two offsets, each tested at both
configured stay durations: `2 offsets x 2 durations = 4` search windows per
city (not a sweep of simulated dates, which keeps run size proportional to
city count only):

| Offset | Default | Meaning |
|---|---|---|
| **Short** | 1 week (`HORIZON_SHORT_OFFSET_DAYS=7`) | Booking about a week out |
| **Long** | 3 months (`HORIZON_LONG_OFFSET_MONTHS=3`) | Booking a few months out |

The long offset can have poor availability that far ahead. Rather than
guess, the pipeline runs it for real, measures the clean yield, and only if
that yield is too thin (below `HORIZON_MIN_ROWS_PER_OFFSET`, after at least 3
windows), re-runs just the long-offset windows at a nearer fallback
(`HORIZON_LONG_OFFSET_FALLBACK_MONTHS`, default 2 months). The short offset
is never affected.

Windows execute **offset-major, then duration, then city**, so a run killed
halfway still covers every city it reached for the combination it was on.

```bash
python -m src.cli plan --dry-run                 # inspect a plan without scraping
python scripts/generate_input.py --preview-plan   # size a run before starting it
```

---

## 5. Data quality gate

A record is **clean** only when all nine checks pass. Otherwise it's kept
(never dropped) with `is_clean=0` and a semicolon-separated `Quality Flags`
trail explaining why:

| Check | Flag if it fails |
|---|---|
| Booking match found | `NO_MATCH` |
| Tunnel reached a usable panel | `TUNNEL_FAILED` |
| Nuitée price present, in range | `MISSING_NUITEE_PRICE` / `NUITEE_PRICE_OUT_OF_RANGE` |
| Booking price present, in range | `MISSING_BOOKING_PRICE` / `BOOKING_PRICE_OUT_OF_RANGE` |
| Currency normalised to MAD | `CURRENCY_NOT_MAD` |
| Fuzzy match score above threshold | `LOW_FUZZY_SCORE` |
| Inter-site price ratio plausible | `IMPLAUSIBLE_RATIO` |
| Hotel/city/dates/horizon all present | `MISSING_IDENTITY` |
| `checkout > checkin` | `INVALID_DATES` |

Two acceptance targets, both tracked in `output/quality_report.json`:

- **≥ 1,000 clean hotels overall** (`QUALITY_TARGET_CLEAN_HOTELS`), counting
  distinct hotels, not rows.
- **≥ 200 clean hotels per offset × stay-duration crossing**
  (`QUALITY_MIN_CLEAN_HOTELS_PER_CROSSING`), summed across cities. A run
  doesn't stop early just because the global target is met; every crossing
  it touches must independently clear this bar too.

```bash
make report            # human-readable quality report + top rejection reasons
```

| Dominant flag | Action |
|---|---|
| `NO_MATCH` | Lower `BOOKING_FUZZY_THRESHOLD` to ~80 |
| `TUNNEL_FAILED` | `TUNNEL_DEBUG_ARTIFACTS=true`, inspect `output/debug/`, update `config/selectors.yaml` |
| `MISSING_BOOKING_PRICE` | Usually self-heals on retry (see [Troubleshooting](#11-troubleshooting)) |
| `IMPLAUSIBLE_RATIO` | *Raise* `BOOKING_FUZZY_THRESHOLD` (false-positive matches) |

The store deduplicates on `hotel + city + dates + horizon + rooms`, and a
failed re-scrape can never overwrite a previously clean row.

---

## 6. Architecture

```
├── config/selectors.yaml        ← ALL DOM selectors, as ordered cascades
├── src/
│   ├── config.py                ← Pydantic settings (validated at start-up)
│   ├── models.py                ← Typed domain models + CSV schema
│   ├── cli.py                   ← argparse CLI (run/plan/verify/report/export/doctor)
│   ├── verify_and_correct.py    ← tunnel + Booking repair pass
│   ├── scrapers/
│   │   ├── nuitee.py            ← search → harvest → tunnel orchestration
│   │   ├── tunnel.py            ← the checkout-tunnel walk + payment guard
│   │   └── booking.py           ← lookup, destination resolution, fuzzy match, price extraction
│   ├── pipeline/
│   │   ├── horizon_generator.py ← today + offsets x durations plans
│   │   ├── runner.py            ← orchestrator (crossing-aware early exit)
│   │   ├── storage.py           ← CSV-backed store (in-memory SQLite query engine)
│   │   └── quality.py           ← the clean gate + reporting
│   └── utils/                   ← browser/stealth, selector cascades, pricing, fuzzy text, retry
├── scripts/generate_input.py    ← city-list presets (morocco/europe/mena/world)
└── tests/                       ← 273 tests
```

**CSV is the store, not a database file.** `prices.csv` / `prices_clean.csv`
are the durable output; a small in-memory SQLite table is rebuilt from the
CSV on every start (fast queries for resume/dedup/reporting, no on-disk
`.db` file, no filesystem-locking issues on network mounts). Both files are
rewritten after every search window, so you can open them mid-run.

**Concurrency:** windows run sequentially; within a window, tunnel walks
(`NUITEE_TUNNEL_CONCURRENCY`, default 3) and Booking lookups
(`BOOKING_CONCURRENCY`, default 4) run concurrently, each in an isolated
browser context. Anti-blocking measures (consistent fingerprints, viewport
jitter, blocked image/media/font requests, full-jitter backoff) are in
`src/utils/browser.py`.

---

## 7. CLI reference

```bash
python -m src.cli <command> [options]
```

### `run`

| Option | Default | Description |
|---|---|---|
| `--input, -i` | `input.json` | City list |
| `--output, -o` | `output/prices.csv` | CSV destination |
| `--only-offset` | `all` | `short`, `long`, or `all` |
| `--durations` | `3,10` | Comma-separated stay lengths in nights |
| `--reference-date` | today | The "today" offsets are measured from |
| `--cities` |  | Comma-separated, overrides `--input` |
| `--limit-windows` |  | Cap window count (smoke tests) |
| `--target` | `1000` | Clean-hotel target (global) |
| `--hotels-per-city` | `25` | Hotels priced per city per date |
| `--no-tunnel` | off | Use display prices (diagnostics only) |
| `--no-resume` | off | Re-scrape already-clean rows |
| `--headed` | off | Visible Chromium window |
| `--fail-under-target` | off | Exit code 2 if global or per-crossing target is missed |

### Other commands

```bash
python -m src.cli plan    --dry-run                  # inspect, don't scrape
python -m src.cli verify  --mode tunnel --limit 500  # repair failures (modes: tunnel/booking/all/unclean)
python -m src.cli report  --json                     # quality report
python -m src.cli export  --clean-only --parquet     # export
python -m src.cli doctor                             # pre-flight check
```

---

## 8. Make targets

```bash
make build                # build the image
make doctor                # pre-flight check
make input                 # regenerate input.json + volume projection

make run-full-pipeline     # scrape → verify → export → report
make run-offset-short      # short offset only (~1 week out)
make run-offset-long       # long offset only (~3 months out)
make run-smoke              # 2 cities x 4 windows, fast sanity check
make plan                   # print the analysis plan

make verify                 # repair tunnel + Booking failures
make report                 # quality report
make export-clean           # CSV of clean rows only

make logs / shell / stop / clean / nuke
make test / test-cov / lint / format
```

Every target accepts overrides, e.g.:

```bash
make run-offset-short DURATIONS=3
make run CITIES="Casablanca,Rabat" LIMIT=10
```

---

## 9. Configuration

All settings live in `.env` (see `.env.example`), validated at start-up.

| Variable | Default | Effect |
|---|---|---|
| `NUITEE_HOTELS_PER_CITY` | `25` | Main coverage/throughput dial |
| `NUITEE_TUNNEL_CONCURRENCY` | `3` | Tunnel parallelism (memory-bound) |
| `BOOKING_CONCURRENCY` | `4` | Booking parallelism (block-risk-bound, see [§11](#11-troubleshooting)) |
| `BOOKING_FUZZY_THRESHOLD` | `85` | Lower ⇒ more matches, more false positives |
| `HORIZON_SHORT_OFFSET_DAYS` | `7` | The short booking horizon |
| `HORIZON_LONG_OFFSET_MONTHS` | `3` | The long booking horizon |
| `HORIZON_LONG_OFFSET_FALLBACK_MONTHS` | `2` | Fallback if the long horizon's yield is too thin |
| `HORIZON_STAY_DURATIONS_NIGHTS` | `3,10` | Stay lengths tested at both horizons |
| `TIMEOUT_DEST_ID_RESOLVE` | `12000` | ms to confirm a Booking destination resolved before trusting the page |
| `QUALITY_TARGET_CLEAN_HOTELS` | `1000` | The global acceptance target |
| `QUALITY_MIN_CLEAN_HOTELS_PER_CROSSING` | `200` | The per-crossing acceptance target |
| `STORAGE_RESUME` | `true` | Skip already-clean work on restart |

---

## 10. Output schema

Written to `output/` (`prices.csv`, `prices_clean.csv`, `quality_report.json`),
rewritten after every search window, safe to open mid-run.

| Column | Description |
|---|---|
| `Hotel Name`, `City` | Identity |
| **`Market Scope`** | `National (Morocco)` or `International`, from `City` |
| `Check-in`, `Check-out`, `Nights`, `Offset` | The stay and horizon (`1W`, `3M`, `2M` if the fallback fired) |
| `Adults per Room`, `Rooms` | Occupancy |
| `Nuitee Display Price` | Advertised **per-night** list price |
| **`Nuitee Final Payable`** | True **whole-stay** total incl. taxes/fees (compared directly against Booking) |
| `Nuitee Local Fees` | The site's own fees figure, scraped directly. **Null, never an estimate**, when unavailable |
| `Nuitee Room`, `Tunnel Status` | Which offer, how far the walk got |
| `Booking Price`, `Booking Matched Name`, `Fuzzy Score` | Booking side (also a whole-stay total) |
| `Price Difference (MAD / %)`, `Cheaper Platform` | Booking − Nuitée, and who wins |
| `Match Status`, `Clean`, `Quality Flags` | Provenance (see [§5](#5-data-quality-gate)) |
| `Nuitee URL`, `Booking URL` | Direct links |

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Many rows `NO_RESULTS` with a perfect (100) fuzzy match but no price | Booking's destination-resolution race: a cold deep link renders a fake "unavailable" placeholder under the real hotel's name, and resolving it needs more time under real `BOOKING_CONCURRENCY` than the old hardcoded 6s wait allowed | Fixed: the wait is now `TIMEOUT_DEST_ID_RESOLVE` (12s, tunable), and an unresolved "unavailable" reading is retried, not trusted. If it still happens a lot, lower `BOOKING_CONCURRENCY` |
| `Booking Matched Name` / `Booking URL` refer to two different hotels | Booking's result list re-sorted between scoring a card and reading its price/URL | Fixed: the matched card's name is re-read and fuzzy-compared right before its price is trusted |
| A run never progresses past short-offset windows | `MATCH_FAIL` (deterministic, no card cleared threshold) was being retried like a transient error, tripling cost per unmatched hotel | Fixed: only `ERROR` (exception/timeout/block) is retried now |
| A clearly-unavailable Booking card still produced a price | The per-card unavailability check didn't cover current site wording, and only ran on a fallback path this case never reached | Fixed: wording updated, check now runs on the matched card directly |
| `doctor`: `Cannot create the output directory …` | `STORAGE_OUTPUT_DIR` points somewhere unwritable | Use a local writable path or a named Docker volume |
| All rows `TUNNEL_FAILED` | Tunnel selectors stale | `TUNNEL_DEBUG_ARTIFACTS=true`, inspect `output/debug/`, update `config/selectors.yaml` |
| All rows `NO_MATCH` | Threshold too strict, or blocked | Check logs for `captcha_markers`; lower `BOOKING_FUZZY_THRESHOLD` |
| Chromium crashes | `/dev/shm` too small | `shm_size: "2gb"` is already set in compose; confirm it applied |
| `SettingsError` mentioning field `"browser"` in `pytest`/`make test` | OS `$BROWSER` env var (VS Code Remote-SSH/WSL) collides with `Settings.browser` | Fixed in `src/config.py`: pull the fix, or `unset BROWSER` on an older checkout |
| `PermissionError: .../app/output/scraper.log` on the very first `make doctor`/`make run-*` | The mounted `./output` gets created fresh by the host, but the container's baked-in user UID/GID (set at image build time) doesn't match whoever owns it. A plain bind mount doesn't inherit any ownership fix baked into the image, since the host directory shadows it entirely | Fixed: `make`'s `_ensure_output` now `chmod -R 777`s the mount automatically; if you hit this from a raw `docker compose run` (bypassing Make), run `mkdir -p output && chmod -R 777 output` once yourself. The error message itself now says this too, instead of a bare traceback |
| `docker compose run ... --input input_international.json` reports "Input file not found" even though the file exists on the host | `docker-compose.yml` only bind-mounts files it explicitly names; a path Docker doesn't know about isn't visible inside the container no matter what's actually on the host, and if the file didn't exist yet the moment Compose started, the mount point is silently filled in with an empty directory instead | Fixed: `input_morocco.json` / `input_international.json` are now named bind mounts in `docker-compose.yml`, and `make` targets auto-generate both first via `_ensure_input`. Bypassing Make, generate them yourself before running `docker compose run` (see [§3](#3-morocco-vs-international-input-files)) |

**Scaling to 1,000+ clean hotels**, cheapest first: add cities
(`python scripts/generate_input.py --preset morocco --preset europe`), raise
`NUITEE_HOTELS_PER_CITY`, add stay durations, then `make verify` (typically
recovers 20-40% of failed rows cheaply).

---

## 12. Testing

```bash
make test          # 273 tests
make test-cov      # with coverage
```

Covers: price parsing (locale formats, FX, bounds), horizon/offset
generation and fallback logic, all 9 quality checks, storage
dedup/resume/CSV round-trip, selector cascades + CLI parsing, the tunnel
payment guard and price extraction, Booking destination-resolution/retry/
fuzzy-match/unavailability logic, and a mocked end-to-end run (harvest →
tunnel → Booking → gate → persist → report → export).

---

## 13. Safety & compliance

- **No bookings are ever made.** The payment page's submit control is never
  clicked; every click in the tunnel is screened against both an exact-testid
  hard block and a payment-token text guard.
- **No real personal data is submitted.** Any guest-form fallback uses a
  synthetic identity from `.env`.
- **Rate limiting is respected.** Bounded concurrency, randomised delays,
  full-jitter backoff; captcha detection triggers a hard backoff, not a
  retry storm.
- **Read-only.** The pipeline reads publicly displayed prices and writes
  nothing to either platform.

Confirm your use complies with both platforms' terms of service and any
applicable laws before running this against production sites.
