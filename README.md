# Nuitée × Booking.com Price Competitiveness Pipeline

**V2** : full checkout-tunnel price extraction, live-verified against
both sites, with a two-part statistical acceptance gate (1,000 hotels overall,
200 per offset/stay-duration crossing).

Scrapes the **true final payable price** from Nuitée's reservation tunnel (not
the advertised list price), matches each hotel on Booking.com, and measures
price competitiveness from today's date at two booking horizons about a
week out and about three months out each tested at two stay lengths.

---

## In plain English

If you're not a developer, here's what this tool actually does and why it's
built the way it is.

**The problem it solves.** A hotel's price on Nuitée's results page is not
what a guest actually pays taxes, city tax, and service fees get added
later, at checkout. So a naive scraper that just reads the results page is
comparing an *incomplete* Nuitée price against a *complete* Booking.com price,
which makes Nuitée look artificially cheaper than it really is. This tool
avoids that trap: for every hotel, it clicks all the way through Nuitée's
booking flow to the screen right before payment, reads the real total there,
and only then compares it against Booking.com.

**What you get.** A spreadsheet (and a small database) listing, for each
hotel in each city, at two booking horizons (about a week ahead of check-in,
and about three months ahead) and two stay lengths (a short 3-night stay and
a longer 10-night stay): the real Nuitée price, the Booking.com price, which
one is cheaper, and by how much. Rows that couldn't be verified cleanly (a
hotel that couldn't be matched on Booking.com, a price that looks like a data
error, and so on) are kept but clearly marked as "not clean" nothing is
silently thrown away, so you can always see exactly how much data was usable
and why the rest wasn't.

**How it stays honest.** Every design decision here favours "found nothing"
over "made something up." If the tool can't confidently match a hotel between
the two sites, it says so instead of guessing. If it can't reach the final
price, it says so instead of reporting the (misleadingly low) results-page
price. And it never places a real booking or enters payment details anywhere,
under any circumstance that guarantee is described in detail in
[§14](#14-safety--compliance).

**Who should read what.** This document is organised so a non-technical
reader can get the full picture from §1 (what this measures, and how) and §2
(how to actually run it), while later sections go into full engineering
detail for whoever operates or maintains the tool day to day.

---

## Table of contents

1. [What this measures](#1-what-this-measures)
2. [Quick start](#2-quick-start)
3. [How the tunnel navigation works](#3-how-the-tunnel-navigation-works)
4. [Booking horizons and stay durations](#4-booking-horizons-and-stay-durations)
5. [Data quality: the two acceptance criteria](#5-data-quality-the-two-acceptance-criteria)
6. [Architecture](#6-architecture)
7. [CLI reference](#7-cli-reference)
8. [Make targets](#8-make-targets)
9. [Configuration](#9-configuration)
10. [Tuning selectors without touching Python](#10-tuning-selectors-without-touching-python)
11. [Output schema](#11-output-schema)
12. [Operations & troubleshooting](#12-operations--troubleshooting)
13. [Testing](#13-testing)
14. [Safety & compliance](#14-safety--compliance)

---

## 1. What this measures

This pipeline runs a price-competitiveness analysis across four dimensions:
**platform** (Nuitée vs. Booking.com), **city**, **booking horizon** (how far
ahead of check-in the traveller is booking), and **stay duration** (how many
nights). Every output row is tagged with all four, so any crossing of them
can be sliced from the CSV or the database after the fact.

**Two booking horizons, measured from today.** Every hotel is priced from a
single reference point today's date at two different "how far ahead is
this booking" horizons: a **short** horizon (about a week out, configurable)
and a **long** horizon (about three months out, configurable, with its own
fallback to a nearer month if that horizon's own results turn out too thin).
See [§4](#4-booking-horizons-and-stay-durations).

**Two stay durations, tested at both horizons.** Each horizon is tested at a
short stay (3 nights, configurable) and a longer stay (10 nights,
configurable), so every city produces exactly `2 horizons x 2 durations = 4`
search windows not a sweep of simulated scraping dates across a calendar
window. This keeps the analysis fast and its size predictable, regardless of
how many cities are configured.

**The second scraping approach: true final price, not the display price.**
For every hotel, a real browser clicks through Nuitée's reservation flow from
the results page to the screen immediately before payment, and reads the true
payable total there (room rate + taxes + fees). The results-page price is
only ever used as a secondary "display price" column for comparison it is
never what gets compared against Booking.com. Full technical detail,
including how this was verified against the live site, is in
[§3](#3-how-the-tunnel-navigation-works). The payment screen itself is never
entered or submitted see [§14](#14-safety--compliance).

**Statistical acceptance criteria.** Two thresholds are enforced, tracked, and
reported:

- **≥ 1,000 clean hotels overall** `QUALITY_TARGET_CLEAN_HOTELS` (default
  1,000). The run stops once this is reached.
- **≥ 200 clean hotels per horizon × stay-duration crossing**, summed across
  every city `QUALITY_MIN_CLEAN_HOTELS_PER_CROSSING` (default 200). The run
  does **not** stop early just because the global target is met if any
  crossing it has touched is still short it keeps going until every
  crossing it's responsible for clears the minimum, or it runs out of plan to
  execute.

Both numbers, and a full breakdown showing exactly which crossings pass and
which don't, are in every quality report. See
[§5](#5-data-quality-the-two-acceptance-criteria).

### Known limitation: single-city smoke tests can't satisfy the crossing rule

With only **one** city configured, there are only 4 crossings possible
(2 horizons × 2 durations), so the *global* 1,000-hotel target and the
*per-crossing* 200-hotel target both still apply, but there is no statistical
diversification across cities. This is expected and by design the crossing
requirement is scoped per horizon × duration, summed across cities, not per
city alone but it means a meaningful, representative dataset needs multiple
cities in `input.json`, not just the one used for a quick smoke-test run.

---

## 2. Quick start

### Docker (recommended)

```bash
cp .env.example .env          # adjust NUITEE_URL if needed
make build
make doctor                   # pre-flight: config, selectors, storage
make run-smoke                # 2 cities x 4 windows verifies the tunnel works
make run-full-pipeline        # the real run
```

### Local

```bash
make setup                    # installs deps + Chromium
python3 -m src.cli doctor
python3 -m src.cli run --only-offset all
```

### Recommended first run

Never launch a multi-day job blind. Escalate:

```bash
make run-smoke                                        # ~5 min  is the tunnel alive?
make report                                           # inspect the rejection flags
python3 -m src.cli run --cities "Casablanca,Marrakech" \
    --hotels-per-city 10                              # ~1 h is the yield sane?
make run-full-pipeline                                # full run
```

If `make run-smoke` yields zero clean rows, the tunnel or Booking selectors
need tuning see [§10](#10-tuning-selectors-without-touching-python). Do not
scale up first.

---

## 3. How the tunnel navigation works

This is the core of the implementation (`src/scrapers/tunnel.py`,
`src/scrapers/nuitee.py`) the direct answer to "how do we get the true
final payable price, not the advertised one" (§1). Its design reflects the
**real site**, confirmed by live inspection (browser DOM/network
inspection against the actual Nuitée white-label deployment) rather than
assumed from a generic booking-flow template see
[§3.4](#3-how-the-tunnel-navigation-works) for what that inspection changed
and why.

### 3.1 The problem

The results-page price is **not** what the customer pays. It typically
excludes city tax, VAT, service fees and resort fees. The only trustworthy
figure is the total shown on the booking page, before payment.

### 3.2 The real architecture: two pages, not a five-panel SPA flow

The site is a Vue 3 / PrimeVue single-page app backed by LiteAPI. Live
inspection established that it is exactly **two pages**:

```
results list (/search?...)
        │  results-list cards carry NO <a href> at all navigation
        │  fires from a JS click handler via window.open(url), so the
        │  target URL only exists once you actually click
        ▼
hotel detail page  /hotels/{id}                         ── ROOM_SELECT
  rate cards (.rate-card), each with a
  "Choose room" CTA [data-testid="hotel-choose-room-button"]
        │  click ⇒ client-side route push
        ▼
booking page  /booking?...                               ── BOOKING_PAGE
  ONE page: guest form + price summary + payment, together.
  The payable total [data-testid="checkout-total-price"]
  renders WITHOUT any guest-form field being filled
  (verified on a live, untouched tab).
```

Two consequences drove the whole design:

1. **No `href` to scrape.** `getAttribute('href')` on a results-page card
   link returns `null` confirmed live (`hasAttribute('href') === false`).
   The URL is only constructed inside the click handler. So capturing a
   hotel's detail URL means *clicking* it and catching the resulting
   `window.open()` popup, not reading markup.
2. **No multi-panel state machine needed.** There's nothing to classify: the
   flow is a fixed two-step walk (pick cheapest rate card → read the total on
   the page it lands on). The only genuine uncertainty is whether a given
   rate card's CTA is actually clickable, which is why the cheapest *N*
   offers are tried in order rather than just the first one.

### 3.3 The two-phase pipeline

**Phase 1 harvest + detail-URL capture (`NuiteeScraper.harvest`)**

While the results page is still open, every kept hotel (post cheapest-first
truncation to `NUITEE_HOTELS_PER_CITY`) is isolated one at a time via the
site's own name-filter input (`[data-testid="hotel-name-filter"]`) not by
a remembered scroll-index, because the results list is virtualised and
re-renders, so an index recorded during the scroll pass can silently point at
a different hotel by the time this runs. For each isolated card:

```
async with page.expect_popup() as popup_info:
    click the card's "See availability" trigger
popup = await popup_info.value
await popup.wait_for_load_state("domcontentloaded")
hotel.detail_url = popup.url          # the real, click-constructed URL
await popup.close()                   # only the URL was needed here
```

**Phase 2 the tunnel walk (`TunnelNavigator.run`)**, one hotel at a time,
in its own browser context:

1. `goto(detail_url)`, settle (dismiss overlays, wait out spinners).
2. Check for a sold-out / fatal-error marker → `NO_ROOMS` if found.
3. Parse every `.rate-card` into a `RoomOffer` (label + price, `min` parse
   strategy so a per-night fragment doesn't beat the nightly rate); sort
   ascending, unpriced offers sort last.
4. Click the cheapest offer's CTA, payment-guarded, and wait for the URL to
   contain `/booking`. If a click lands but nothing advances a race when
   inventory changes mid-page fall through to the next-cheapest offer, up
   to `TUNNEL_ROOM_CANDIDATES` (default 3).
5. On the booking page: try to read the total **immediately, with no form
   interaction**. Only if that fails, and `TUNNEL_FILL_GUEST_DETAILS` is on,
   fill the guest-form fields (synthetic identity) and retry the read once.
   No CTA on this page is ever clicked see [§3.7](#3-how-the-tunnel-navigation-works).

```
TunnelResult ⇐ OK (total read) | NO_ROOMS | TUNNEL_BLOCKED | PAYMENT_GUARD | ERROR
```

### 3.4 What live inspection corrected

The first version of this module modelled a generic 5-panel reservation flow
(`ROOM_SELECT → OPTIONS → GUEST_DETAILS → SUMMARY → PAYMENT`, each panel
independently classified via a `detect` cascade race) and read the detail URL
straight off each card's `href`. Running it against the live site surfaced a
0% tunnel-success rate; inspecting the actual DOM and network traffic via the
browser developer tools showed both assumptions were wrong for this
property: there is no `href` to read, and there are two pages, not five
independently-arriving panels. §3.2 and §3.3 describe what replaced them.
`PanelKind` keeps the old panel names for backward compatibility with any
already-persisted `Deepest Panel` values, but the navigator itself only ever
produces `room_select` or `booking_page` now.

### 3.5 Final price extraction three strategies, highest confidence first

| # | Strategy | Mechanism | When it wins |
|---|---|---|---|
| 1 | **Dedicated element** | `[data-testid="checkout-total-price"] .price.total` (live-verified) | Cleanest markup, tried first, waited for explicitly since the total can take a beat to hydrate after the route push |
| 2 | **Labelled row scan** | Single `page.evaluate` finds a node whose text matches `"total price"` / `"prix total"` / `"montant total"`, walks up ≤4 levels to a container holding a number | Table layouts with no test id |
| 3 | **Container sweep** | Largest plausible number in the summary container | Last resort the payable total is by construction the largest figure in an itemised summary |

All strategies feed the same currency-normalising parser, so a summary
rendered in EUR still lands in the dataset as MAD.

### 3.6 Resilience

- **Retries**: `TUNNEL_MAX_RETRIES` (default 2), each a *fresh* navigation
  the SPA holds reservation state in memory, so restarting beats resuming.
- **Error banners**: separate `fatal_errors` (don't retry the room is gone)
  and `retryable_errors` (do retry) cascades, checked on both the detail page
  and the booking page.
- **Overlay auto-dismiss**: Playwright locator handlers clear modals that
  interrupt any action.
- **Debug artefacts**: `TUNNEL_DEBUG_ARTIFACTS=true` writes a full-page
  screenshot + HTML dump for every failure into `output/debug/`.

### 3.7 🔒 The payment guard rail

**No booking is ever submitted.** This is a *structural* property, not just a
runtime check: the booking-page code path only ever **reads** the total and
**fills** text inputs it performs zero clicks on that page at all, so there
is no code path left that could press "Complete Booking" even by accident. On
top of that, two independent defences guard every click made anywhere in the
tunnel (i.e. on the detail page, selecting a rate card):

1. **Exact testid hard block** `_is_payment_trigger()` checks the
   element's `data-testid` against `nuitee.tunnel.payment.hard_block_testids`
   (`"checkout-submit-button"`). A literal match blocks the click regardless
   of the element's text.
2. **Text-token guard** the element's inner text, `aria-label`, `name`,
   `id`, `value` and `data-testid`, folded through an accent-stripping,
   whitespace-collapsing normaliser, is checked against
   `TUNNEL_PAYMENT_GUARD_TOKENS` (`"pay now"`, `"payer maintenant"`,
   `"confirm and pay"`, `"complete booking"`, …).

Either match means the candidate is **skipped, not clicked**.

The guest form, when filled, uses a configurable synthetic identity purely as
a fallback for properties where the total doesn't render until the form is
touched no real personal data is submitted, and again, nothing on that page
is ever clicked.

---

## 4. Booking horizons and stay durations

Implemented in `src/pipeline/horizon_generator.py`.

### 4.1 The concept

Competitiveness is measured from a single reference point today, call it
T at two different "how far ahead is this booking" offsets:

| Offset | Default | Meaning |
|---|---|---|
| **Short** | 1 week (`HORIZON_SHORT_OFFSET_DAYS=7`) | Traveller booking about a week out |
| **Long** | 3 months (`HORIZON_LONG_OFFSET_MONTHS=3`) | Traveller booking a few months out |

Both offsets are tested at **both** of two stay lengths (a short 3-night stay
and a longer 10-night stay, `HORIZON_STAY_DURATIONS_NIGHTS=3,10`), so every
city produces exactly `2 offsets x 2 durations = 4` search windows:

```
checkin(short) = T + HORIZON_SHORT_OFFSET_DAYS
checkin(long)  = T + HORIZON_LONG_OFFSET_MONTHS months
                 (or HORIZON_LONG_OFFSET_FALLBACK_MONTHS, see below)
checkout       = checkin + duration_nights
```

This is deliberately **not** a sweep of many simulated scraping dates across
a calendar window the plan's size stays proportional to the number of
cities and stay durations only, which is what keeps a full run finishing in a
practical amount of time regardless of how many cities are configured.

### 4.2 Offset labels

Each resolved offset gets a short label used throughout the reports and the
CSV: an exact number of weeks renders as `"1W"`, anything else as `"{n}D"`
(e.g. `"10D"`); a months-based offset renders as `"{n}M"` (e.g. `"3M"`,
or `"2M"` if the fallback fired).

### 4.3 The long-offset fallback

The long offset can turn out to have poor availability that far ahead. Rather
than guess, the pipeline runs the long offset for real, measures the clean
yield, and only if that yield is too thin re-runs just the long-offset
windows at the nearer fallback offset (`HORIZON_LONG_OFFSET_FALLBACK_MONTHS`,
default 2 months) instead. The short offset's windows are never affected by
this decision.

This is a **measured decision, not a guess**:

1. Run the long-offset windows.
2. Compute realised clean rows ÷ windows executed for the long offset only.
3. If that yield falls below `HORIZON_MIN_ROWS_PER_OFFSET` (default 10), and
   at least 3 long-offset windows have run (so an infrastructure hiccup is
   never misdiagnosed as an inventory problem), regenerate the long-offset
   windows at the fallback offset instead same effort, concentrated on a
   nearer, better-stocked date.

### 4.4 Execution order

Windows are emitted **offset-major**, then by stay duration, then by city. A
run killed halfway therefore still covers every city it reached for the
offset/duration combination it was on, rather than being all-Casablanca.
Partial runs stay analysable.

### 4.5 Stopping early both targets, not just one

A run stops before exhausting its full plan only when **both** are true: the
global clean-hotel target is met, **and** every `(offset x stay-duration)`
crossing the plan touches has independently reached the per-crossing minimum
(see [§5](#5-data-quality-the-two-acceptance-criteria)). Meeting the global
target alone is not sufficient to stop otherwise a run could hit 1,000
clean hotels entirely from one crossing and quietly skip the rest, which
would satisfy the raw count but not the statistical-weight requirement.

### 4.6 Inspecting a plan before running it

```bash
python3 -m src.cli plan --dry-run
python3 -m src.cli plan --only-offset short --json
python3 -m src.cli plan --durations 3,10 --save output/plan.json
python3 -m src.cli plan --reference-date 2026-08-15 --dry-run
```

### 4.7 Sizing a run

```bash
python3 scripts/generate_input.py --preview-plan --durations 3,10
```

```
  cities              : 80
  offsets             : 2 (short ~1 week, long ~3 months)
  stay durations      : [3, 10]
  search windows      : 320
  max rows            : 8,000
  expected clean rows : ~3,600
  Projection clears the 1,000 clean-hotel target.
```

`--only-offset` restricts a run to `short` or `long` alone (useful for
re-running just one side after a fallback), and `--durations` overrides the
configured stay lengths for a single invocation.

---

## 5. Data quality: the two acceptance criteria

Two statistical bars apply, not one: at least 1,000 clean hotels overall,
**and** at least 200 clean hotels in every offset × stay-duration crossing,
summed across every city. "Clean" is only a meaningful word if it's defined
precisely and applied mechanically `src/pipeline/quality.py` is that
definition, in code.

### 5.1 The nine checks

A record is **clean** only when all nine hold:

| # | Check | Failure flag |
|---|---|---|
| C1 | `match_status == OK` | `NO_MATCH` |
| C2 | `tunnel_status ∈ {OK, PARTIAL}` | `TUNNEL_FAILED` |
| C3 | Nuitée final price present, within `[min, max]` | `MISSING_NUITEE_PRICE` / `NUITEE_PRICE_OUT_OF_RANGE` |
| C4 | Booking price present, within `[min, max]` | `MISSING_BOOKING_PRICE` / `BOOKING_PRICE_OUT_OF_RANGE` |
| C5 | Currency normalised to MAD | `CURRENCY_NOT_MAD` |
| C6 | `fuzzy_score ≥ BOOKING_FUZZY_THRESHOLD` | `LOW_FUZZY_SCORE` |
| C7 | Inter-site price ratio ≤ `QUALITY_MAX_PRICE_RATIO` | `IMPLAUSIBLE_RATIO` |
| C8 | Hotel, city, dates, horizon all present | `MISSING_IDENTITY` |
| C9 | `checkout > checkin` | `INVALID_DATES` |

**Every** check runs no short-circuiting. Knowing a row failed on *both* the
tunnel and the match is far more diagnostic than knowing it failed the first
check.

### 5.2 Nothing is silently dropped

Failing rows are retained with `is_clean = 0` and a semicolon-separated
`quality_flags` string. Those rejects are the diagnostic trail that tells you
whether a shortfall is a *matching* problem, a *tunnel* problem, or an
*inventory* problem and `make verify` uses exactly that trail to repair
them.

### 5.3 The headline metrics

**Metric 1 global.** Counts **distinct clean hotels**, not clean rows:

```sql
COUNT(DISTINCT hotel_name_normalised || '|' || city) WHERE is_clean = 1
```

The same hotel scraped at the short offset and the long offset is 2 clean
rows but **1** clean hotel. This is deliberately the conservative reading of
the metric. Target: `QUALITY_TARGET_CLEAN_HOTELS` (default 1,000).

**Metric 2 per crossing.** The same count, grouped by `(horizon_label,
nights)` and summed across every city. Every crossing the plan touches must
independently reach `QUALITY_MIN_CLEAN_HOTELS_PER_CROSSING` (default 200).
This is what makes the sample statistically usable for a short-vs-long
comparison, rather than just a large but lopsided pile of rows.

### 5.4 Deduplication

The SQLite primary key is a SHA-1 of
`hotel_name_normalised | city | checkin | checkout | horizon_label | rooms | adults`.
Re-scraping the same stay **UPSERTs** rather than duplicating and the upsert
carries `WHERE excluded.is_clean >= price_records.is_clean`, so **a failed
re-scrape can never overwrite good data**.

### 5.5 Reading the report

```bash
make report
```

```
========================================================================
  DATA QUALITY REPORT
========================================================================
  Total rows              : 8,240
  Clean rows              : 3,710 (45.0%)
  Distinct clean hotels   : 1,247
  Target                  : 1,000
  Status                  : MET
  Per-crossing minimum    : 200 clean hotels (MET for all crossings)

  By crossing (offset x stay-duration):
    1W|3n                         clean=   612 / 200   OK
    1W|10n                        clean=   598 / 200   OK
    3M|3n                         clean=   287 / 200   OK
    3M|10n                        clean=   203 / 200   OK

  Top rejection reasons:
    NO_MATCH                       2,104  ( 25.5%)
    TUNNEL_FAILED                  1,402  ( 17.0%)
    MISSING_BOOKING_PRICE            655  (  7.9%)

  Price competitiveness (clean rows only):
    Nuitee cheaper          : 2,155 (58.1%)
    Booking cheaper         : 1,502 (40.5%)
    Mean difference (MAD)   : 143.21
========================================================================
```

**How to act on the flags:**

| Dominant flag | Meaning | Action |
|---|---|---|
| `NO_MATCH` | Booking matching is too strict, or the hotel isn't listed | Lower `BOOKING_FUZZY_THRESHOLD` to ~80; raise `BOOKING_MAX_CARDS_TO_SCAN` |
| `TUNNEL_FAILED` | Tunnel selectors are stale | Set `TUNNEL_DEBUG_ARTIFACTS=true`, inspect `output/debug/`, update `config/selectors.yaml` |
| `MISSING_BOOKING_PRICE` | Card matched but the destination never resolved (see [§12](#12-operations--troubleshooting)), or price markup changed | Usually self-heals via the retry built into `_resolve_destination`; if persistent, update `booking.card_price` in the YAML |
| `IMPLAUSIBLE_RATIO` | False-positive matches | *Raise* `BOOKING_FUZZY_THRESHOLD` |
| `CURRENCY_NOT_MAD` | Session fell back to a local currency | Verify `selected_currency=MAD` reaches the URL; set `FX_RATE_*` overrides |

Machine-readable version: `output/quality_report.json` (includes a
`crossings` object with the per-crossing breakdown and a `met` boolean).

---

## 6. Architecture

```
nuitee_scraper/
├── config/
│   └── selectors.yaml           ← ALL DOM selectors, as ordered cascades
├── src/
│   ├── config.py                ← Pydantic settings (8 validated sub-models)
│   ├── models.py                ← Typed domain models + CSV schema
│   ├── cli.py                   ← argparse CLI, 6 subcommands
│   ├── main.py                  ← legacy-compatible entry point
│   ├── verify_and_correct.py    ← tunnel + Booking repair pass
│   ├── scrapers/
│   │   ├── nuitee.py            ← search → harvest → tunnel orchestration
│   │   ├── tunnel.py            ← the 2-page tunnel walk + payment guard
│   │   └── booking.py           ← lookup, destination resolution, fuzzy match, price extraction
│   ├── pipeline/
│   │   ├── horizon_generator.py ← today + offsets x durations plans
│   │   ├── runner.py            ← orchestrator (crossing-aware early exit)
│   │   ├── storage.py           ← SQLite + CSV/Parquet export
│   │   └── quality.py           ← the clean gate + reporting (global + per-crossing)
│   └── utils/
│       ├── browser.py           ← contexts, fingerprints, stealth, pooling
│       ├── selectors.py         ← cascade resolution engine
│       ├── pricing.py           ← price parsing + FX normalisation
│       ├── text.py              ← normalisation + fuzzy matching
│       ├── retry.py             ← backoff with full jitter
│       └── logging_setup.py
├── scripts/generate_input.py
└── tests/                       ← 212 tests
```

### 6.1 Concurrency model

| Level | Bound by | Default | Rationale |
|---|---|---|---|
| Windows | sequential | 1 | Both sites rate-limit per IP; the bottleneck is the tunnel, which is already internally parallel |
| Tunnel walks | `NUITEE_TUNNEL_CONCURRENCY` | 3 | Heavy and stateful; each gets an isolated context |
| Booking lookups | `BOOKING_CONCURRENCY` | 4 | Light; the main block risk |
| Contexts | `BrowserManager` semaphore | max of the two | One browser, many contexts 40 ms and a clean cookie jar each |

### 6.2 Anti-blocking

- Self-consistent fingerprints: UA paired with matching `sec-ch-ua`,
  `sec-ch-ua-platform` and `navigator.platform` (a Windows UA reporting
  `platform: "Linux"` is a trivial tell).
- Viewport jitter and device-scale randomisation per context.
- Init script patches `navigator.webdriver`, plugins, mimeTypes,
  `window.chrome`, the notification-permission mismatch, and WebGL vendor
  strings (`SwiftShader` is a dead giveaway).
- Image/media/font requests aborted at the route level ~3× throughput and a
  smaller bandwidth signature.
- Randomised inter-request delays; exponential backoff with **full jitter** so
  concurrent workers don't retry in lockstep.
- Captcha detection triggers a hard backoff rather than a hot retry loop.

---

## 7. CLI reference

```bash
python3 -m src.cli <command> [options]
```

### `run` execute the pipeline

| Option | Default | Description |
|---|---|---|
| `--input, -i` | `input.json` | City list |
| `--output, -o` | `output/prices.csv` | CSV destination |
| `--only-offset` | `all` | `short`, `long`, or `all` |
| `--durations` | `3,10` | Comma-separated stay lengths in nights |
| `--reference-date` | today | The "today" offsets are measured from |
| `--cities` | | Comma-separated, overrides `--input` |
| `--limit-windows` | | Cap window count (smoke tests) |
| `--target` | `1000` | Clean-hotel target (global) |
| `--hotels-per-city` | `25` | Hotels priced per city per date |
| `--no-tunnel` | off | Use display prices (diagnostics only) |
| `--no-resume` | off | Re-scrape already-clean rows |
| `--headed` | off | Visible Chromium window |
| `--fail-under-target` | off | Exit code 2 if the global OR per-crossing target is missed |

### Other commands

```bash
python3 -m src.cli plan    --dry-run                  # inspect, don't scrape
python3 -m src.cli verify  --mode tunnel --limit 500  # repair failures
python3 -m src.cli report  --json                     # quality report
python3 -m src.cli export  --clean-only --parquet     # export
python3 -m src.cli doctor                             # pre-flight check
```

### `verify` modes

| Mode | Re-attempts |
|---|---|
| `tunnel` | Rows with a failed/missing tunnel extraction |
| `booking` | Rows with a failed Booking match |
| `all` | Either of the above (default) |
| `unclean` | Every row with `is_clean = 0` |

Legacy CSV migration:

```bash
python3 -m src.verify_and_correct --import-csv output/legacy_prices.csv
python3 -m src.verify_and_correct --mode tunnel     # backfill final prices
```

---

## 8. Make targets

```bash
make build                # build the image
make doctor               # pre-flight check
make input                # regenerate input.json + volume projection

make run-full-pipeline    # scrape → verify → export → report
make run-offset-short     # short offset only (~1 week out)
make run-offset-long      # long offset only (~3 months out)
make run-fallback-2m      # force the long offset's 2-month fallback
make run-smoke            # 2 cities x 4 windows fast sanity check
make plan                 # print the analysis plan

make verify               # repair tunnel + Booking failures
make verify-tunnel        # tunnel failures only
make verify-dry           # preview what would be re-attempted
make report               # quality report
make export-clean         # CSV of clean rows only

make logs / shell / stop / clean / nuke
make test / test-cov / lint / format
```

Every target accepts overrides:

```bash
make run-offset-short DURATIONS=3,10
make run CITIES="Casablanca,Rabat" LIMIT=10
```

---

## 9. Configuration

All settings live in `.env` (see `.env.example`) and are validated at
start-up by `src/config.py` a bad value fails immediately rather than
three hours in.

Highest-impact variables:

| Variable | Default | Effect |
|---|---|---|
| `NUITEE_HOTELS_PER_CITY` | `25` | **Main coverage/throughput dial** |
| `NUITEE_TUNNEL_CONCURRENCY` | `3` | Tunnel parallelism (memory-bound) |
| `BOOKING_CONCURRENCY` | `4` | Booking parallelism (block-risk-bound) |
| `BOOKING_FUZZY_THRESHOLD` | `85` | Lower ⇒ more matches, more false positives |
| `HORIZON_SHORT_OFFSET_DAYS` | `7` | The short booking horizon |
| `HORIZON_LONG_OFFSET_MONTHS` | `3` | The long booking horizon |
| `HORIZON_LONG_OFFSET_FALLBACK_MONTHS` | `2` | Fallback if the long horizon's yield is too thin |
| `HORIZON_STAY_DURATIONS_NIGHTS` | `3,10` | Stay lengths tested at both horizons |
| `HORIZON_MIN_ROWS_PER_OFFSET` | `10` | Fallback trigger; must stay below `NUITEE_HOTELS_PER_CITY` |
| `TUNNEL_ALLOW_PARTIAL_TUNNEL` | `true` | Accept a total from an earlier panel |
| `TUNNEL_DEBUG_ARTIFACTS` | `false` | Screenshot + HTML on every failure |
| `QUALITY_TARGET_CLEAN_HOTELS` | `1000` | The global acceptance target |
| `QUALITY_MIN_CLEAN_HOTELS_PER_CROSSING` | `200` | The per-(offset × stay-duration) acceptance target |
| `STORAGE_RESUME` | `true` | Skip already-clean work on restart |

---

## 10. Tuning selectors without touching Python

Every DOM selector lives in `config/selectors.yaml` as an **ordered cascade**.
The engine tries candidates in order and uses the first that resolves.

```yaml
nuitee:
  tunnel:
    summary:
      detect:
        - '[data-testid="checkout-total-price"]'
        - 'text=/includes taxes and fees/i'
      total_price:
        - '[data-testid="checkout-total-price"] .price.total'   # live-verified, most specific first
        - '[data-testid="checkout-total-price"] .grand-total'
        - '.p-booking__total__main.grand-total'
```

**When Nuitée or Booking.com ship a redesign, add one line at the top of the
relevant list and redeploy. No Python change, no rebuild of application
logic.**

Ordering convention: `data-testid` → class-based → structural/text fallbacks.
Text candidates are bilingual (EN + FR) because the white-label renders in
the browser's `Accept-Language` and Morocco-facing deployments frequently
fall back to French.

### 10.1 The feedback loop

Run with `LOG_LEVEL=DEBUG` and the runner prints a **selector hit report** at
the end: which candidate actually fired for each cascade, and how often.
Promote the winners to the top; delete candidates that never matched.

### 10.2 Workflow for a broken tunnel or a broken Booking match

```bash
# 1. Capture failures
TUNNEL_DEBUG_ARTIFACTS=true make run-smoke

# 2. Inspect output/debug/*.html find the real selector

# 3. Add it at the TOP of the relevant cascade in config/selectors.yaml

# 4. Re-verify only the broken rows
make verify-tunnel LIMIT=50
```

`python3 -m src.cli doctor` fails fast if any required selector path is
missing.

---

## 11. Output schema

Written to `output/` (`prices.csv`, `prices_clean.csv`, `hotel_prices.db`,
`quality_report.json`). `Booking URL` is the **last** column, matching the
original convention.

The CSV export deliberately leaves out a handful of internal/operational
fields (a numeric horizon-offset-in-days, the deepest tunnel panel reached,
the run identifier, a verification flag) that are tracked in the SQLite
database for diagnostics but aren't something a reader comparing prices
needs.

| Column | Description |
|---|---|
| `Hotel Name`, `City` | Identity |
| `Check-in`, `Check-out`, `Nights` | The stay |
| `Offset` | Which booking horizon (`1W`, `3M`, `2M` if the fallback fired, etc.) |
| `Adults per Room`, `Rooms` | Occupancy |
| **`Nuitee Display Price (MAD)`** | Advertised list price |
| **`Nuitee Final Payable (MAD)`** | **True total incl. taxes & fees** ← the whole point of the checkout-tunnel approach |
| **`Nuitee Hidden Fees (MAD)`** | Final − display: what the list price omitted |
| `Nuitee Room` | Cheapest offer selected |
| `Tunnel Status` | How far the walk got |
| `Booking Price (MAD)`, `Booking Matched Name`, `Fuzzy Score` | Booking side |
| `Price Difference (MAD)`, `Price Difference (%)` | Booking − Nuitée |
| `Cheaper Platform` | `NUITEE` / `BOOKING` / `TIE` |
| `Match Status`, `Clean`, `Quality Flags` | Provenance |
| `Nuitee URL`, `Booking URL` | Direct links |

The `Nuitee Hidden Fees` column is often the most commercially interesting:
it quantifies exactly how much the advertised price understates the payable
total the whole reason the checkout-tunnel approach exists.

`quality_report.json` additionally carries a `crossings` object:
`{"min_clean_hotels_per_crossing": 200, "met": true|false, "by_crossing":
{"Offset|Nnights": {"rows": ..., "clean": ..., "distinct_hotels": ...}, ...}}`.

---

## 12. Operations & troubleshooting

### 12.1 Resume after interruption

Enabled by default. `Ctrl-C` finishes the in-flight window, flushes buffers
and exports; the next run skips every already-clean row. A second `Ctrl-C`
forces an immediate exit.

### 12.2 The Booking.com "stale deep link" issue (fixed, but good to understand)

A live investigation found that Booking.com's search-results page, when
loaded directly via a deep link (`searchresults.html?ss=...`), frequently
renders a **false "Unavailable on our site for your selected dates"** card
for a property that has real, bookable inventory confirmed for three
unrelated hotels (a 5-star chain, a mid-range independent, a boutique riad),
all of which showed a normal priced card the instant the on-page Search
button was actually clicked. This is why, before the fix, a large share of
otherwise-healthy hotels were coming back `NO_RESULTS` with a perfect 100%
name match but no price the match was correct, but the page it was read
from was stale.

The fix (`BookingScraper._resolve_destination`) clicks Search and **verifies**
the URL actually gained a `dest_id` before trusting the page, retrying with a
fresh overlay sweep if not, and falling back to driving Booking's own
destination autocomplete for the handful of property names it can't resolve
via a plain query string at all (also live-confirmed, for a different
property). If you see `MISSING_BOOKING_PRICE` on properties you know are
bookable, this is the first place to look see
`src/scrapers/booking.py`'s module docstring for the full write-up.

### 12.3 Common issues

| Symptom | Cause | Fix |
|---|---|---|
| `doctor`: `SQLite cannot operate on …` | Filesystem lacks POSIX locking (NFS/SMB, some Docker Desktop bind mounts) | Set `STORAGE_OUTPUT_DIR` to a local path, or use a named Docker volume |
| All rows `TUNNEL_FAILED` | Tunnel selectors stale | `TUNNEL_DEBUG_ARTIFACTS=true`, inspect `output/debug/`, update the YAML |
| Many rows `NO_RESULTS` with a 100% name match but no price | Stale Booking.com deep link (see §12.2) | Should self-heal via `_resolve_destination`'s retry; if persistent, check for a new overlay/modal covering the Search button |
| All rows `NO_MATCH` | Threshold too strict, or blocked | Check for `captcha_markers` in the logs; lower `BOOKING_FUZZY_THRESHOLD` |
| Chromium crashes | `/dev/shm` too small | `shm_size: "2gb"` is already set in compose confirm it applied |
| `Executable doesn't exist` | Playwright/base-image version drift | Keep `PLAYWRIGHT_VERSION` in the Dockerfile in sync with `requirements.txt` |
| Zero hotels harvested | Wrong destination suggestion picked | Check the `_click_city_suggestion` debug log; the airport/region filter may need a new exclusion token |
| Memory growth | Too many concurrent contexts | Lower `NUITEE_TUNNEL_CONCURRENCY` |

### 12.4 Scaling to 1,000+ clean hotels (and 200+ per crossing)

Ordered by cost-effectiveness:

1. **Add cities** `python3 scripts/generate_input.py --preset morocco --preset europe`
   the most direct way to satisfy the per-crossing minimum broadly, since
   each new city contributes to all 4 crossings (2 offsets × 2 durations).
2. **Raise `NUITEE_HOTELS_PER_CITY`** linear yield increase, linear cost.
3. **Add stay durations** via `HORIZON_STAY_DURATIONS_NIGHTS` more
   crossings to spread the sample across, at the cost of more windows.
4. **Lower `BOOKING_FUZZY_THRESHOLD` to ~80** watch `IMPLAUSIBLE_RATIO` for
   the false-positive cost.
5. **Run `make verify`** typically recovers 20–40% of failed rows for a
   fraction of the original cost.

---

## 13. Testing

```bash
make test          # 212 tests
make test-cov      # with coverage
```

| Suite | Covers |
|---|---|
| `test_pricing.py` | Noise stripping, 6 locale separator formats, FX, min/max/first strategies, bounds |
| `test_horizon_generator.py` | Month clamping, offset resolution, plan cartesian, offset-major ordering, fallback decision |
| `test_quality.py` | All 9 clean checks, derived fields, dedup key stability, fuzzy matching |
| `test_storage.py` | UPSERT dedup, resume, "dirty never overwrites clean", CSV column order, report aggregates |
| `test_selectors_and_cli.py` | Cascade resolution, placeholder substitution, payment-guard config, verifier row selection, CLI parsing, both input formats |
| `test_tunnel_logic.py` | Payment guard (text-token + exact-testid hard block), room ranking, price-extraction cascade, required selector paths |
| `test_booking_logic.py` | Destination-resolution retry/fallback logic (the fix in §12.2) |
| `test_runner_integration.py` | **Mocked end-to-end**: harvest → tunnel → Booking → gate → persist → report → export, plus global *and* per-crossing early exit, and resume |

Notable regression tests:

- `test_ascii_space_does_not_merge_unrelated_numbers` `"890 1250"` must
  never parse as `8901250`, the classic silent-garbage bug.
- `test_dirty_row_never_overwrites_a_clean_one` a failed re-scrape must not
  destroy good data.
- `test_booking_skipped_for_unpriced_hotels` no page load is spent on a
  lookup that cannot produce a clean row.
- `test_click_that_does_nothing_is_not_trusted` a Booking search-submit
  click that "succeeds" but never resolves a destination must not be trusted
  as if it had (the actual production bug, reproduced as a unit test).
- `test_global_target_alone_does_not_trigger_early_exit` hitting the
  1,000-hotel target from one crossing must not skip a different crossing
  that still needs work.

---

## 14. Safety & compliance

- **No bookings are ever made.** The booking page's submit control is never
  clicked the navigator only reads its total and fills text inputs, so
  there's no code path that presses it even by accident. Every click made
  anywhere in the tunnel (selecting a rate card) is additionally screened
  against both an exact-testid hard block and the payment-token text guard.
- **No real personal data is submitted.** The guest form, when used as a
  fallback, receives a synthetic identity configured in `.env`.
- **Rate limiting is respected.** Bounded concurrency, randomised delays, and
  full-jitter exponential backoff; captcha detection triggers a hard backoff
  rather than a retry storm.
- **Read-only.** The pipeline reads publicly displayed prices and writes
  nothing to either platform.

Confirm that your use complies with both platforms' terms of service and any
applicable agreement with Nuitée before running at scale.
