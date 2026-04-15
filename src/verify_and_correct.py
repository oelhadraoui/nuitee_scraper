"""
src/verify_and_correct.py
─────────────────────────────────────────────────────────────────────────────
Bulk verification & correction pass for a ~17 k-row hotel-price CSV.

Pipeline
────────
  1. Load the CSV produced by scraper.py.
  2. Re-scrape every row whose booking_price is NaN, 0, or flagged MATCH_FAIL.
  3. For each row:
       a. Navigate to the saved Booking URL.
       b. Dismiss modals.
       c. Fuzzy-match the first property card name vs. the expected Hotel Name.
          • Match  (≥ FUZZY_THRESHOLD): scrape price immediately.
          • No match: click the Search button to force a DB refresh, wait,
            then fuzzy-match again.
          • Still no match after retry: mark MATCH_FAIL.
  4. Strict price parsing ignores review counts, ratings, distances, and any
     numeric value below 150 MAD; returns the largest surviving candidate.
  5. Write results to /app/output/ (Docker) or output/ (host) – never in-place.
  6. Column order in output CSV: all original columns, with "Booking URL" last.

Execution Environment
─────────────────────
  • Headless Ubuntu on Oracle VM  (Playwright Chromium, headless=True)
  • Semaphore-bounded concurrency (default CONCURRENCY = 5)
  • Graceful shutdown on SIGINT / SIGTERM
  • Checkpoint every CHECKPOINT_EVERY rows so a crash is recoverable

Usage
─────
  python3 src/verify_and_correct.py \
      --input  output/prices.csv \
      --output output/hotels_verified.csv \
      --concurrency 5
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import re
import signal
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# ── fuzzy matching ────────────────────────────────────────────────────────────
try:
    from rapidfuzz import fuzz as _rfuzz
    def _fuzzy_score(a: str, b: str) -> float:
        """token_set_ratio: order-insensitive, handles partial names well."""
        return _rfuzz.token_set_ratio(a, b)
except ImportError:
    import difflib
    def _fuzzy_score(a: str, b: str) -> float:            # type: ignore[misc]
        return difflib.SequenceMatcher(
            None, a.lower(), b.lower()
        ).ratio() * 100

# ── optional playwright-stealth ───────────────────────────────────────────────
try:
    from playwright_stealth import stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
CONCURRENCY      = int(os.getenv("VERIFY_CONCURRENCY", "5"))
CHECKPOINT_EVERY = int(os.getenv("CHECKPOINT_EVERY",   "50"))
FUZZY_THRESHOLD  = float(os.getenv("FUZZY_THRESHOLD",  "85"))   # 0–100

# Output directory (Docker-safe)
_DOCKER_OUT = Path("/app/output")
_HOST_OUT   = Path("output")
OUTPUT_DIR  = _DOCKER_OUT if _DOCKER_OUT.exists() else _HOST_OUT
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Column names in the source CSV ────────────────────────────────────────────
COL_NAME      = "Hotel Name"
COL_NUITEE    = "Nuitee Price (MAD)"
COL_PRICE     = "Booking Price (MAD)"
COL_DIFF      = "Price Difference (MAD)"
COL_STATUS    = "match_status"
COL_VERIFIED  = "verified"
COL_URL       = "Booking URL"          # must be LAST column in output

# ── Playwright timeouts (ms) ──────────────────────────────────────────────────
T = {
    "page_load":        28_000,
    "after_goto":        1_500,
    "first_card":       14_000,
    "popup_dismiss":       800,
    "price_selector":    2_000,
    "post_search":       8_000,
    "search_btn_visible": 4_000,
    "card_name":         3_000,
    "after_search_click": 2_500,
}

PRICE_SELECTORS = [
    '[data-testid="price-and-discounted-price"]',
    '[data-testid="recommended-units-price"]',
    '.bui-price-display__value',
    '.prco-valign__middle-helper',
    'span[aria-label*="MAD"]',
    'span[aria-label*="price"]',
]

CARD_NAME_SELECTORS = [
    '[data-testid="title"]',
    '[data-testid="property-card-name"]',
    '.sr-hotel__name',
    'h3[class*="header"]',
    'a[data-testid="title-link"] span',
]

SOLD_OUT_SIGNALS = [
    "unavailable", "sold out", "no availability",
    "fully booked", "no rooms available",
]

POPUP_SELECTORS = ",".join([
    'button[aria-label="Dismiss sign-in info."]',
    'button[aria-label="Sign in"]',
    '[data-testid="header-signin-link"]',
    '#onetrust-accept-btn-handler',
    'button[aria-label="Close"]',
    'button[aria-label="Dismiss"]',
    '.sb-searchbox__backdrop',
    '.modal-mask',
])

SEARCH_BTN_SELS = [
    'button[type="submit"]',
    '.de576f5064',
    '[data-testid="searchbox-submit-button"]',
]

USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            OUTPUT_DIR / "verify_and_correct.log", encoding="utf-8"
        ),
    ],
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# STRICT PRICE PARSER
# ─────────────────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    """
    Extract the correct room price while discarding noise such as:
      - Review counts  ("144 reviews", "1,234 reviews")
      - Rating scores  ("4.5", "8.9")
      - Distances      ("0.3 km", "2 miles")
      - Small integers that are not prices

    Strategy
    ────────
    1. Strip noise tokens (reviews / ratings / distances) from the raw text.
    2. Normalise separators and collapse whitespace.
    3. Collect all remaining numeric candidates.
    4. Return the *largest* candidate that is ≥ 150 MAD.
       (Valid MAD room rates for this dataset are consistently above 150;
        review counts that survive cleaning – e.g. "144" – fall below.)
    """
    if not text:
        return None

    # ── 1. remove noise tokens ────────────────────────────────────────────────
    noise_patterns = [
        r'\d[\d,\.]*\s*(?:reviews?|avis|ratings?|stars?|étoiles?)',
        r'\d[\d,\.]*\s*(?:km|miles?|mi)\b',
        r'\b\d\.\d\b',   # lone decimals < 10 (review scores like "8.7", "4.5")
    ]
    clean = text
    for pat in noise_patterns:
        clean = re.sub(pat, '', clean, flags=re.IGNORECASE)

    # ── 2. normalise separators ───────────────────────────────────────────────
    # Replace Unicode non-breaking / thin spaces (Booking's thousands separator)
    # but do NOT strip plain ASCII spaces — that would merge unrelated numbers
    # like "890 1250" into the erroneous token "8901250".
    for ch in ('\xa0', '\u202f', '\u2009'):
        clean = clean.replace(ch, '')
    # Remove commas used as thousands separators within digit runs: "1,250" → "1250"
    clean = re.sub(r'(\d),(\d)', r'\1\2', clean)

    # ── 3. collect numeric candidates ─────────────────────────────────────────
    candidates: list[float] = []
    for m in re.finditer(r'\d+(?:\.\d{1,2})?', clean):
        try:
            val = float(m.group())
            if val >= 150:
                candidates.append(val)
        except ValueError:
            continue

    if not candidates:
        return None

    # ── 4. return the largest valid price ─────────────────────────────────────
    return max(candidates)


# ─────────────────────────────────────────────────────────────────────────────
# ROW SELECTION
# ─────────────────────────────────────────────────────────────────────────────

def _needs_verification(row: pd.Series) -> bool:
    """Return True if this row should be re-scraped."""
    status = str(row.get(COL_STATUS, "")).strip().upper()
    if status in ("MATCH_FAIL", "ERROR", "NO_RESULTS"):
        return True
    try:
        price = float(row.get(COL_PRICE, 0) or 0)
    except (ValueError, TypeError):
        price = 0.0
    if price <= 0:
        return True
    verified = str(row.get(COL_VERIFIED, "")).strip().lower()
    if verified in ("false", "0", "no", ""):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# MODAL CLEARANCE
# ─────────────────────────────────────────────────────────────────────────────

async def _dismiss_modals(page: Page) -> None:
    """Click every visible modal / sign-in overlay."""
    for sel in POPUP_SELECTORS.split(","):
        sel = sel.strip()
        try:
            loc = page.locator(sel)
            count = await loc.count()
            for i in range(count):
                try:
                    await loc.nth(i).click(timeout=600, force=True)
                    await page.wait_for_timeout(150)
                except Exception:
                    pass
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH BUTTON CLICK
# ─────────────────────────────────────────────────────────────────────────────

async def _force_search(page: Page) -> bool:
    """
    Click the Booking.com search/submit button.
    Returns True when a button was successfully clicked.
    """
    for sel in SEARCH_BTN_SELS:
        try:
            btn = page.locator(sel).first
            await btn.wait_for(state="visible", timeout=T["search_btn_visible"])
            await btn.scroll_into_view_if_needed(timeout=2_000)
            await btn.click(force=True, timeout=3_000)
            return True
        except Exception:
            continue
    log.warning("No search button found – proceeding without click")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# FIRST CARD NAME EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

async def _get_first_card_name(page: Page) -> str | None:
    """
    Extract the displayed hotel name from the first property card.
    Tries targeted selectors first, falls back to the card's h3 tag.
    """
    card_loc = page.locator('[data-testid="property-card"]')
    try:
        await card_loc.first.wait_for(state="visible", timeout=T["first_card"])
    except Exception:
        return None

    first_card = card_loc.first

    for sel in CARD_NAME_SELECTORS:
        try:
            el = first_card.locator(sel).first
            if await el.is_visible(timeout=T["card_name"]):
                name = (await el.inner_text()).strip()
                if name:
                    return name
        except Exception:
            continue

    # Generic fallback: first h3 inside the card
    try:
        h3 = first_card.locator("h3").first
        name = (await h3.inner_text()).strip()
        if name:
            return name
    except Exception:
        pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# PRICE EXTRACTION FROM FIRST PROPERTY CARD
# ─────────────────────────────────────────────────────────────────────────────

async def _extract_price_from_first_card(page: Page) -> float | None:
    """
    Try each PRICE_SELECTOR on the first property card.
    Falls back to full card text (noise already stripped by _parse_price).
    """
    card_loc = page.locator('[data-testid="property-card"]')
    try:
        await card_loc.first.wait_for(state="visible", timeout=T["first_card"])
    except Exception:
        log.warning("No property cards visible")
        return None

    first_card = card_loc.first

    for sel in PRICE_SELECTORS:
        try:
            el = first_card.locator(sel).first
            if await el.is_visible(timeout=T["price_selector"]):
                raw = await el.inner_text()
                price = _parse_price(raw)
                if price:
                    log.debug("Price via selector '%s': %s", sel, price)
                    return price
        except Exception:
            continue

    # Fallback: full card text
    try:
        card_text = await first_card.inner_text()
        price = _parse_price(card_text)
        if price:
            log.debug("Price via full card text fallback: %s", price)
            return price
    except Exception:
        pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# CORE FETCH  (one hotel) — Multi-Step Fuzzy Validation Loop
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_price(
    browser:     Browser,
    hotel_name:  str,
    booking_url: str,
) -> dict:
    """
    Full scrape + fuzzy-validation cycle for a single hotel.

    Step 1  Navigate to the saved Booking URL.
    Step 2  Dismiss modals.
    Step 3  Fuzzy-match first card name vs. expected hotel name.
            • Score ≥ FUZZY_THRESHOLD → scrape price immediately.
            • Score <  FUZZY_THRESHOLD → click Search button, wait for refresh.
    Step 4  Post-click second fuzzy check.
            • Match → scrape price.
            • No match → MATCH_FAIL.
    Step 5  Record the post-search page.url as the direct URL.
    """
    result: dict = {
        "price":        None,
        "url":          booking_url,
        "match_status": "ERROR",
        "fuzzy_score":  0.0,
    }

    context: BrowserContext = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=random.choice(USER_AGENTS),
        locale="en-US",
        timezone_id="Europe/Paris",
        extra_http_headers={
            "Accept-Language":           "en-US,en;q=0.9",
            "Accept":                    (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
            "Referer":                   "https://www.google.com/",
            "DNT":                       "1",
            "Upgrade-Insecure-Requests": "1",
        },
    )
    page: Page = await context.new_page()

    if HAS_STEALTH:
        await stealth_async(page)

    # Auto-dismiss any modal that surfaces during the session
    async def _auto_dismiss(loc):
        try:
            await loc.first.click(timeout=T["popup_dismiss"], force=True)
        except Exception:
            pass

    await page.add_locator_handler(page.locator(POPUP_SELECTORS), _auto_dismiss)

    try:
        # ── Step 1: navigate ─────────────────────────────────────────────────
        await page.goto(
            booking_url,
            wait_until="domcontentloaded",
            timeout=T["page_load"],
        )
        await page.wait_for_timeout(T["after_goto"])

        # ── Step 2: dismiss modals ───────────────────────────────────────────
        await _dismiss_modals(page)
        await page.wait_for_timeout(400)

        # ── Step 3: initial fuzzy check ──────────────────────────────────────
        first_card_name = await _get_first_card_name(page)
        score = _fuzzy_score(hotel_name, first_card_name or "")
        log.debug(
            "[%s] Initial card name: '%s'  score=%.1f",
            hotel_name[:40], first_card_name or "N/A", score,
        )

        if score >= FUZZY_THRESHOLD:
            # Direct hit — scrape price right away
            log.info(
                "[%s] ✓ fuzzy match (%.1f) on first card → scraping price",
                hotel_name[:40], score,
            )
            price = await _extract_price_from_first_card(page)
            result["url"] = page.url
            if price is not None:
                result.update(price=price, match_status="OK", fuzzy_score=score)
                log.info(
                    "[%s] ✓ price=%.2f  url=%s",
                    hotel_name[:40], price, result["url"][:60],
                )
            else:
                result["match_status"] = "NO_RESULTS"
                result["fuzzy_score"]   = score
                log.warning("[%s] ✗ matched card but no price extracted", hotel_name[:40])

        else:
            # No match — click Search to force a DB refresh
            log.info(
                "[%s] First card '%s' (score=%.1f) < threshold — clicking Search",
                hotel_name[:40], first_card_name or "N/A", score,
            )
            await _force_search(page)

            # Wait for the results to reload
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=T["post_search"]
                )
            except Exception:
                await page.wait_for_timeout(T["after_search_click"])

            result["url"] = page.url   # capture the direct/refreshed URL

            # ── Step 4: secondary fuzzy check ───────────────────────────────
            second_card_name = await _get_first_card_name(page)
            score2 = _fuzzy_score(hotel_name, second_card_name or "")
            log.debug(
                "[%s] Post-search card name: '%s'  score=%.1f",
                hotel_name[:40], second_card_name or "N/A", score2,
            )

            if score2 >= FUZZY_THRESHOLD:
                price = await _extract_price_from_first_card(page)
                if price is not None:
                    result.update(
                        price=price, match_status="OK", fuzzy_score=score2
                    )
                    log.info(
                        "[%s] ✓ price=%.2f (post-search)  url=%s",
                        hotel_name[:40], price, result["url"][:60],
                    )
                else:
                    result["match_status"] = "NO_RESULTS"
                    result["fuzzy_score"]   = score2
                    log.warning(
                        "[%s] ✗ post-search card matched but no price",
                        hotel_name[:40],
                    )
            else:
                result["match_status"] = "MATCH_FAIL"
                result["fuzzy_score"]   = score2
                log.warning(
                    "[%s] ✗ MATCH_FAIL — post-search card '%s' (score=%.1f)",
                    hotel_name[:40], second_card_name or "N/A", score2,
                )

    except Exception as exc:
        log.warning("[%s] exception: %s", hotel_name[:40], exc)
        result["match_status"] = "ERROR"
    finally:
        await context.close()

    return result


# ─────────────────────────────────────────────────────────────────────────────
# WORKER
# ─────────────────────────────────────────────────────────────────────────────

async def _worker(
    sem:      asyncio.Semaphore,
    browser:  Browser,
    idx:      int,
    row:      pd.Series,
    results:  dict,
    progress: list,
) -> None:
    hotel_name  = str(row.get(COL_NAME, "")).strip()
    booking_url = str(row.get(COL_URL,  "")).strip()

    if not hotel_name or not booking_url:
        results[idx] = {
            "price": None, "url": booking_url, "match_status": "ERROR"
        }
        progress.append(1)
        return

    async with sem:
        r = await _fetch_price(browser, hotel_name, booking_url)
        results[idx] = r

    progress.append(1)


# ─────────────────────────────────────────────────────────────────────────────
# CHECKPOINT WRITER
# ─────────────────────────────────────────────────────────────────────────────

def _write_checkpoint(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_suffix(".tmp.csv")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    tmp.replace(path)


# ─────────────────────────────────────────────────────────────────────────────
# RESULT FLUSH  (merge results dict → DataFrame, enforce column order)
# ─────────────────────────────────────────────────────────────────────────────

def _flush_results(df: pd.DataFrame, results: dict) -> None:
    """
    Merge scraped results into df, recalculate Price Difference, and
    ensure COL_URL is the last column.

    Price Difference = Booking Price − Nuitee Price
    """
    for idx, res in results.items():
        if res["price"] is not None:
            df.at[idx, COL_PRICE] = res["price"]

        df.at[idx, COL_STATUS]   = res["match_status"]
        df.at[idx, COL_VERIFIED] = (
            "true" if res["match_status"] == "OK" else "false"
        )

        # Recalculate price difference
        try:
            booking_price = float(df.at[idx, COL_PRICE] or 0)
            nuitee_price  = float(df.at[idx, COL_NUITEE] or 0)
            df.at[idx, COL_DIFF] = round(booking_price - nuitee_price, 2)
        except (ValueError, TypeError):
            pass

        # Always update Booking URL with the direct/post-search URL
        if res.get("url"):
            df.at[idx, COL_URL] = res["url"]

    # ── Enforce column order: everything first, COL_URL last ─────────────────
    if COL_URL in df.columns:
        other_cols = [c for c in df.columns if c != COL_URL]
        df = df.reindex(columns=other_cols + [COL_URL])
    # df is modified in-place for column values; reindex returns a new object,
    # so we reassign – caller must use the return value when ordering matters.
    return df     # noqa: R504  (explicit return for column-order enforcement)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────

async def run(
    input_path:  str,
    output_path: str,
    concurrency: int,
    force_all:   bool,
) -> None:
    log.info("Loading CSV: %s", input_path)
    df = pd.read_csv(input_path, dtype=str, keep_default_na=False)

    # Ensure required output columns exist
    for col in (COL_STATUS, COL_VERIFIED, COL_DIFF):
        if col not in df.columns:
            df[col] = ""

    output_path_obj = Path(output_path)

    # Decide which rows to verify
    mask = (
        pd.Series([True] * len(df))
        if force_all
        else df.apply(_needs_verification, axis=1)
    )
    indices_to_verify = df.index[mask].tolist()
    total = len(indices_to_verify)
    log.info(
        "Rows to verify: %d / %d  (concurrency=%d  fuzzy_threshold=%.0f)",
        total, len(df), concurrency, FUZZY_THRESHOLD,
    )

    if total == 0:
        log.info("Nothing to verify – writing output as-is.")
        df = _flush_results(df, {}) or df
        _write_checkpoint(df, output_path_obj)
        return

    sem      = asyncio.Semaphore(concurrency)
    results  : dict = {}
    progress : list = []
    shutdown  = asyncio.Event()

    def _handle_signal(*_):
        log.warning("Shutdown signal – finishing in-flight tasks …")
        shutdown.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except (NotImplementedError, RuntimeError):
            pass

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1280,800",
            ],
        )

        tasks = []
        for i, idx in enumerate(indices_to_verify):
            if shutdown.is_set():
                break
            row  = df.loc[idx]
            task = asyncio.create_task(
                _worker(sem, browser, idx, row, results, progress),
                name=f"row-{idx}",
            )
            tasks.append(task)

            # Periodic checkpoint (flush completed results so far)
            if (i + 1) % CHECKPOINT_EVERY == 0:
                done_results = dict(results)  # snapshot
                updated_df   = _flush_results(df.copy(), done_results) or df
                _write_checkpoint(updated_df, output_path_obj)
                log.info("Checkpoint: %d / %d rows processed", len(progress), total)

        log.info("All tasks dispatched – awaiting completion …")
        await asyncio.gather(*tasks, return_exceptions=True)
        await browser.close()

    # ── Final merge, column ordering, and write ───────────────────────────────
    updated_df = _flush_results(df, results)
    if updated_df is not None:
        df = updated_df
    _write_checkpoint(df, output_path_obj)

    # ── Summary stats ─────────────────────────────────────────────────────────
    counts: dict[str, int] = {}
    for res in results.values():
        s = res.get("match_status", "ERROR")
        counts[s] = counts.get(s, 0) + 1

    log.info(
        "Done. OK=%d  MATCH_FAIL=%d  NO_RESULTS=%d  ERROR=%d  |  output → %s",
        counts.get("OK",         0),
        counts.get("MATCH_FAIL", 0),
        counts.get("NO_RESULTS", 0),
        counts.get("ERROR",      0),
        output_path_obj,
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _build_default_output(input_path: str) -> str:
    stem = Path(input_path).stem
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(OUTPUT_DIR / f"{stem}_verified_{ts}.csv")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify and correct Booking.com prices in a CSV."
    )
    parser.add_argument("--input",        required=True,          help="Path to source CSV")
    parser.add_argument("--output",       default=None,           help="Path for output CSV")
    parser.add_argument("--concurrency",  default=CONCURRENCY,    type=int)
    parser.add_argument("--fuzzy-threshold",
                        default=FUZZY_THRESHOLD, type=float,
                        help="Minimum fuzzy score (0-100) to accept a hotel name match")
    parser.add_argument(
        "--force-all",
        action="store_true",
        help="Re-verify every row, not just flagged ones",
    )
    args = parser.parse_args()

    out = args.output or _build_default_output(args.input)
    asyncio.run(run(args.input, out, args.concurrency, args.force_all))


if __name__ == "__main__":
    main()