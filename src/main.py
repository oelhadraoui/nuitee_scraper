"""
src/main.py  (scraper.py)
─────────────────────────────────────────────────────────────────────────────
Hotel price comparison scraper: Nuitee → Booking.com

Pipeline
────────
  1. For each city in input.json:
       a. Scrape Nuitee for a full hotel list (name + price).
       b. For each hotel:
            i.  Build an enriched Booking.com query: "Hotel Name City"
           ii.  Navigate → dismiss modals → click Search button.
          iii.  Fuzzy-match the first result card against the expected name
                (≥ FUZZY_THRESHOLD). Mark as MATCH_FAIL if never matches.
           iv.  Extract price from the matched card.
            v.  Save the post-search page.url as the direct "Booking URL".
  2. Append each hotel row to output/prices.csv.

Search query enrichment prevents the "Sticky Recommendation" bug where
Booking.com returns a popular unrelated hotel instead of the target.

Execution Environment
─────────────────────
  • Headless Ubuntu on Oracle VM (Playwright Chromium, headless=True)
  • playwright_stealth optional but recommended
  • input.json must exist at project root

Usage
─────
  python3 src/main.py
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import random
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# ── fuzzy matching ────────────────────────────────────────────────────────────
try:
    from rapidfuzz import fuzz as _rfuzz
    def _fuzzy_score(a: str, b: str) -> float:
        return _rfuzz.token_set_ratio(a, b)
except ImportError:
    import difflib
    def _fuzzy_score(a: str, b: str) -> float:                    # type: ignore[misc]
        return difflib.SequenceMatcher(
            None, a.lower(), b.lower()
        ).ratio() * 100

# ── optional playwright-stealth ───────────────────────────────────────────────
try:
    from playwright_stealth import stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    print("[WARN] playwright_stealth not installed – run: pip install playwright-stealth")

from dotenv import load_dotenv
load_dotenv()


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
FUZZY_THRESHOLD = float(os.getenv("FUZZY_THRESHOLD", "85"))   # 0–100

# ── Timeouts (ms) ─────────────────────────────────────────────────────────────
T = {
    "page_load":           60_000,
    "network_idle":        60_000,
    "url_change":          45_000,
    "first_card":          45_000,
    "selector_wait":       45_000,
    "element_visible":      5_000,
    "price_selector":       5_000,
    "cookie_btn":           5_000,
    "popup_dismiss":        3_000,
    "suggestion_click":    10_000,
    "after_goto":           2_000,
    "after_destination":    1_500,
    "calendar_open":        1_500,
    "after_day_click":        800,
    "after_guests_open":    1_000,
    "after_guests_apply":     800,
    "adult_btn_click":        150,
    "add_room":               800,
    "scroll_settle":          800,
    "next_month_btn":         500,
    # Booking-side interaction
    "search_btn_visible":   4_000,
    "after_search_click":   2_500,
    "post_search":          8_000,
    "card_name":            3_000,
}

BOOKING_CONCURRENCY = max(1, int(os.getenv("BOOKING_CONCURRENCY", "4")))
MAX_SCROLL_PASSES = max(30, int(os.getenv("MAX_SCROLL_PASSES", "1000")))
MAX_SCROLL_SECONDS = max(30, int(os.getenv("MAX_SCROLL_SECONDS", "600")))

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--window-size=1280,800",
    "--blink-settings=imagesEnabled=false,fontsEnabled=false",
    "--disable-gpu",
    "--no-zygote",
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
    "no availability", "sold out", "unavailable",
    "we don't have any available", "no rooms available",
]

POPUP_SELECTORS = ", ".join([
    'button[aria-label="Dismiss sign-in info."]',
    'button[aria-label="Close"]',
    'button[aria-label="Dismiss"]',
    '[data-testid="modal-mask"]',
    'button.modal-mask-closeBtn',
    '#b2searchresultsPage button[class*="close"]',
])

SEARCH_BTN_SELS = [
    'button[type="submit"]',
    '.de576f5064',
    '[data-testid="searchbox-submit-button"]',
]

# ── Output CSV ────────────────────────────────────────────────────────────────
OUTPUT_CSV = Path("output/prices.csv")

# NOTE: "Booking URL" is intentionally the last column.
CSV_HEADER = [
    "Hotel Name",
    "City",
    "Check-in",
    "Check-out",
    "Adults per Room",
    "Rooms",
    "Nuitee Price (MAD)",
    "Booking Price (MAD)",
    "Price Difference (MAD)",
    "match_status",
    "Booking URL",            # ← last column
]


# ─────────────────────────────────────────────────────────────────────────────
# CSV HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_csv_header() -> None:
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not OUTPUT_CSV.exists():
        with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)


def _append_csv_row(row: list) -> None:
    with OUTPUT_CSV.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)


# ─────────────────────────────────────────────────────────────────────────────
# STRICT PRICE PARSER  (shared with verify_and_correct.py)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    """
    Extract the correct room price while discarding noise:
      - Review counts  ("144 reviews", "1,234 reviews")
      - Rating scores  ("4.5", "8.9")
      - Distances      ("0.3 km", "2 miles")

    Returns the largest numeric value ≥ 150 MAD found after cleaning.
    """
    if not text:
        return None

    noise_patterns = [
        r'\d[\d,\.]*\s*(?:reviews?|avis|ratings?|stars?|étoiles?)',
        r'\d[\d,\.]*\s*(?:km|miles?|mi)\b',
        r'\b\d\.\d\b',
    ]
    clean = text
    for pat in noise_patterns:
        clean = re.sub(pat, '', clean, flags=re.IGNORECASE)

    # Replace Unicode non-breaking / thin spaces (thousands separator) only.
    # Plain ASCII spaces are kept so unrelated numbers don't get merged.
    for ch in ('\xa0', '\u202f', '\u2009'):
        clean = clean.replace(ch, '')
    clean = re.sub(r'(\d),(\d)', r'\1\2', clean)  # "1,250" → "1250"

    candidates: list[float] = []
    for m in re.finditer(r'\d+(?:\.\d{1,2})?', clean):
        try:
            val = float(m.group())
            if val >= 150:
                candidates.append(val)
        except ValueError:
            continue

    return max(candidates) if candidates else None


# ─────────────────────────────────────────────────────────────────────────────
# SHARED PAGE UTILITIES
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


async def _force_search(page: Page) -> bool:
    """Click the Booking.com search/submit button. Returns True on success."""
    for sel in SEARCH_BTN_SELS:
        try:
            btn = page.locator(sel).first
            await btn.wait_for(state="visible", timeout=T["search_btn_visible"])
            await btn.scroll_into_view_if_needed(timeout=2_000)
            await btn.click(force=True, timeout=3_000)
            return True
        except Exception:
            continue
    print("[WARN] No search button found – proceeding without click", flush=True)
    return False


async def _get_first_card_name(page: Page) -> str | None:
    """Extract the displayed hotel name from the first property card."""
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
                name = _normalise_text((await el.inner_text()).strip())
                if name:
                    return name
        except Exception:
            continue

    try:
        h3 = first_card.locator("h3").first
        name = _normalise_text((await h3.inner_text()).strip())
        if name:
            return name
    except Exception:
        pass

    return None


async def _extract_price_from_first_card(page: Page) -> float | None:
    """Try each PRICE_SELECTOR; fall back to full card text."""
    card_loc = page.locator('[data-testid="property-card"]')
    try:
        await card_loc.first.wait_for(state="visible", timeout=T["first_card"])
    except Exception:
        return None

    first_card = card_loc.first

    return await _extract_price_from_card(first_card)


async def _extract_price_from_card(card) -> float | None:
    """Extract price from a specific result card."""

    for sel in PRICE_SELECTORS:
        try:
            el = card.locator(sel).first
            if await el.is_visible(timeout=T["price_selector"]):
                raw = await el.inner_text()
                price = _parse_price(raw)
                if price:
                    return price
        except Exception:
            continue

    try:
        card_text = await card.inner_text()
        return _parse_price(card_text)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# INPUT NORMALISATION
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_text(value: str) -> str:
    """Replace commas with spaces and collapse repeated whitespace."""
    return re.sub(r"\s+", " ", value.replace(",", " ")).strip()


async def _best_booking_card(page: Page, expected_name: str) -> tuple[int | None, str | None, float]:
    """Pick the best fuzzy hotel-name match among top visible Booking cards."""
    cards = page.locator('[data-testid="property-card"]')
    try:
        await cards.first.wait_for(state="visible", timeout=T["first_card"])
    except Exception:
        return None, None, 0.0

    try:
        count = await cards.count()
    except Exception:
        return None, None, 0.0

    best_idx: int | None = None
    best_name: str | None = None
    best_score = 0.0

    for idx in range(min(count, 10)):
        card = cards.nth(idx)
        card_name: str | None = None
        for sel in CARD_NAME_SELECTORS:
            try:
                el = card.locator(sel).first
                if await el.is_visible(timeout=700):
                    txt = _normalise_text((await el.inner_text()).strip())
                    if txt:
                        card_name = txt
                        break
            except Exception:
                continue

        if not card_name:
            continue

        score = _fuzzy_score(expected_name, card_name)
        if score > best_score:
            best_score = score
            best_idx = idx
            best_name = card_name

    return best_idx, best_name, best_score


async def _booking_no_availability(page: Page) -> bool:
    """Conservative no-availability detection."""
    try:
        body = (await page.locator("body").inner_text()).lower()
    except Exception:
        return False
    strict = [
        "no rooms available",
        "for your dates",
        "is sold out on our site",
        "no availability for",
    ]
    return any(s in body for s in strict)

def _parse_rooms_config(params: dict) -> list[int]:
    if "rooms_config" in params:
        return [int(a) for a in params["rooms_config"]]
    adults = int(params.get("adults", 2))
    rooms  = int(params.get("rooms",  1))
    return [adults] * rooms


# ─────────────────────────────────────────────────────────────────────────────
# MAIN SCRAPER CLASS
# ─────────────────────────────────────────────────────────────────────────────

class PriceCompare:
    def __init__(
        self,
        city:         str,
        checkin:      str,
        checkout:     str,
        rooms_config: list[int] | None = None,
    ):
        self.nuitee_url   = os.getenv("NUITEE_URL", "https://amine-sadik-2911w.nuitee.link/")
        self.city         = city
        self.checkin      = checkin
        self.checkout     = checkout
        self.rooms_config = rooms_config or [2]
        self.rooms        = len(self.rooms_config)
        self.total_adults = sum(self.rooms_config)
        self.booking_concurrency = BOOKING_CONCURRENCY

    # ──────────────────────────────────────────────────────────────────────────
    # Nuitee scraper  (unchanged harvest logic, kept intact)
    # ──────────────────────────────────────────────────────────────────────────
    async def get_nuitee_data(self, browser: Browser) -> list[dict]:
        import json as _json  # local import to keep top-level imports clean

        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page    = await context.new_page()

        api_blobs: list = []

        async def _capture_response(response):
            url = response.url
            ct  = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = await response.body()
                if len(body) < 100:
                    return
                api_blobs.append(_json.loads(body))
            except Exception:
                pass

        page.on("response", _capture_response)

        await page.goto(
            self.nuitee_url, wait_until="networkidle", timeout=T["network_idle"]
        )

        try:
            await page.locator('[data-testid="accept-button"]').click(
                timeout=T["cookie_btn"]
            )
        except Exception:
            pass

        # ── Destination ───────────────────────────────────────────────────────
        search_input = page.get_by_placeholder("Enter a destination")
        await search_input.click()
        await search_input.type(self.city, delay=80)
        await page.wait_for_timeout(T["after_destination"])

        suggestion_clicked = False
        for attempt in range(3):
            try:
                dropdown_li = (
                    page.locator("ul li")
                    .filter(has_text=self.city)
                    .filter(has_not_text="Airport")
                    .filter(has_not_text="Region")
                    .filter(has_not_text="Province")
                    .first
                )
                await dropdown_li.wait_for(
                    state="visible", timeout=T["suggestion_click"]
                )
                await dropdown_li.click()
                suggestion_clicked = True
                break
            except Exception:
                if attempt < 2:
                    await search_input.triple_click()
                    await search_input.type(self.city, delay=80)
                    await page.wait_for_timeout(T["after_destination"])

        if not suggestion_clicked:
            try:
                await page.locator("ul li").first.wait_for(
                    state="visible", timeout=T["suggestion_click"]
                )
                await page.locator("ul li").first.click()
            except Exception:
                pass

        # ── Date picker ───────────────────────────────────────────────────────
        await page.locator('[data-testid="date-picker"]').click()
        await page.wait_for_timeout(T["calendar_open"])

        checkin_dt  = datetime.strptime(self.checkin,  "%Y-%m-%d")
        checkout_dt = datetime.strptime(self.checkout, "%Y-%m-%d")

        async def _read_visible_month_year():
            header_selectors = [
                '.p-datepicker-title', '.p-datepicker-month',
                '[class*="monthYear"]', '[class*="month-year"]',
                '[class*="MonthYear"]', '[class*="CalendarMonth_caption"]',
                'table caption',
                '[class*="calendar"] h2', '[class*="calendar"] h3',
                '[class*="datepicker"] h2', '[class*="datepicker"] h3',
                '[class*="picker"] h2',
            ]
            found = []
            for sel in header_selectors:
                try:
                    texts = await page.locator(sel).all_inner_texts()
                    for t in texts:
                        t = t.strip()
                        for fmt in ("%B %Y", "%b %Y"):
                            try:
                                dt = datetime.strptime(t, fmt)
                                found.append((dt.year, dt.month))
                                break
                            except ValueError:
                                pass
                except Exception:
                    pass
            if found:
                return min(found)
            try:
                all_text = await page.evaluate("""
                    () => {
                        const walker = document.createTreeWalker(
                            document.body, NodeFilter.SHOW_TEXT
                        );
                        const texts = [];
                        let node;
                        while (node = walker.nextNode()) {
                            const t = node.textContent.trim();
                            if (t.length > 3) texts.push(t);
                        }
                        return texts;
                    }
                """)
                months = [
                    "January", "February", "March", "April", "May", "June",
                    "July", "August", "September", "October", "November", "December",
                ]
                for t in all_text:
                    for m in months:
                        match = re.match(rf'^{m}\s+(\d{{4}})$', t.strip())
                        if match:
                            found.append((int(match.group(1)), months.index(m) + 1))
                if found:
                    return min(found)
            except Exception:
                pass
            return None

        async def _click_next_month() -> bool:
            next_selectors = [
                'button.p-datepicker-next',
                'button[aria-label="Next Month"]',
                'button[aria-label="Next month"]',
                'button[aria-label="next month"]',
                'button[class*="next"]', 'button[class*="Next"]',
                '[data-testid="next-month"]',
                'button:has(svg[class*="right"])',
                'button:has(svg[class*="chevron-right"])',
                'button:has(svg[class*="arrow-right"])',
                '.p-datepicker-header button:last-child',
            ]
            for sel in next_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=400):
                        await btn.click()
                        await page.wait_for_timeout(T["next_month_btn"])
                        return True
                except Exception:
                    pass
            return False

        async def _is_active_date_visible(dt: datetime) -> bool:
            iso = dt.strftime("%Y-%m-%d")
            for cls in ("is-not-in-month", "not-in-month", "outside-month"):
                try:
                    cell = page.locator(
                        f'.vc-day.id-{iso}:not(.{cls}) .vc-day-content'
                    ).first
                    if await cell.is_visible(timeout=300):
                        return True
                except Exception:
                    pass
            try:
                active = await page.evaluate(
                    """
                    (iso) => {
                        const cells = document.querySelectorAll(`[data-date="${iso}"]`);
                        for (const c of cells) {
                            const parent = c.closest('.vc-day') || c.parentElement;
                            if (
                                parent &&
                                !parent.classList.contains('is-not-in-month') &&
                                !parent.classList.contains('not-in-month') &&
                                !parent.classList.contains('outside-month')
                            ) return true;
                        }
                        return false;
                    }
                    """,
                    iso,
                )
                if active:
                    return True
            except Exception:
                pass
            return False

        async def _navigate_to_month(target_dt: datetime):
            for _ in range(36):
                if await _is_active_date_visible(target_dt):
                    return
                cur = await _read_visible_month_year()
                if cur is None:
                    await _click_next_month()
                    continue
                cur_total = cur[0] * 12 + cur[1]
                tgt_total = target_dt.year * 12 + target_dt.month
                if tgt_total <= cur_total + 1:
                    return
                if not await _click_next_month():
                    return

        async def _click_day(dt: datetime):
            iso = dt.strftime("%Y-%m-%d")
            for cls in ("is-not-in-month", "not-in-month", "outside-month"):
                try:
                    cell = page.locator(
                        f'.vc-day.id-{iso}:not(.{cls}) .vc-day-content'
                    ).first
                    if await cell.is_visible(timeout=2_000):
                        await cell.click(force=True)
                        return
                except Exception:
                    pass
            try:
                aria_label = dt.strftime("%A, %b %-d, %Y")
                cell = page.locator(
                    f'[aria-label="{aria_label}"]:not(.is-not-in-month)'
                ).first
                if await cell.is_visible(timeout=1_000):
                    await cell.click()
                    return
            except Exception:
                pass
            try:
                await page.evaluate(
                    """
                    (targetIso) => {
                        const strict = document.querySelector(
                            `.vc-day.id-${targetIso}:not(.is-not-in-month) .vc-day-content`
                        );
                        if (strict) { strict.click(); return; }
                        const dayNum = targetIso.split('-')[2].replace(/^0/, '');
                        for (const d of document.querySelectorAll('.vc-day-content')) {
                            const parent = d.closest('.vc-day');
                            if (
                                d.textContent.trim() === dayNum &&
                                parent &&
                                !parent.classList.contains('is-not-in-month') &&
                                !parent.classList.contains('not-in-month') &&
                                !parent.classList.contains('outside-month')
                            ) { d.click(); return; }
                        }
                    }
                    """,
                    iso,
                )
            except Exception:
                pass

        await _navigate_to_month(checkin_dt)
        await page.wait_for_timeout(T["after_day_click"])
        await _click_day(checkin_dt)
        await page.wait_for_timeout(500)

        await _navigate_to_month(checkout_dt)
        await page.wait_for_timeout(T["after_day_click"])
        await _click_day(checkout_dt)
        await page.wait_for_timeout(T["after_day_click"])

        for confirm_sel in (
            'button[aria-label="Done"]', 'button:has-text("Done")',
            'button:has-text("Apply")', '[data-testid="date-apply"]',
            '.p-datepicker-buttonbar button',
        ):
            try:
                btn = page.locator(confirm_sel).first
                if await btn.is_visible(timeout=600):
                    await btn.click()
                    break
            except Exception:
                pass

        await page.locator('[data-testid="guests-button"]').click()
        await page.wait_for_timeout(T["after_guests_open"])

        async def _set_adults(room_idx: int, target: int):
            panel    = page.locator(f'[id="room-{room_idx + 1}"]')
            row      = panel.locator(".p-selectNumber").first
            num_span = row.locator(".number").first
            try:
                current = int((await num_span.inner_text()).strip())
            except Exception:
                current = 2
            dec  = row.locator("button").nth(0)
            inc  = row.locator("button").nth(1)
            diff = target - current
            btn  = inc if diff > 0 else dec
            for _ in range(abs(diff)):
                await btn.click()
                await page.wait_for_timeout(T["adult_btn_click"])

        await _set_adults(0, self.rooms_config[0])
        for i in range(1, self.rooms):
            await page.locator('button[aria-label="Add room"]').click()
            await page.wait_for_timeout(T["add_room"])
            await _set_adults(i, self.rooms_config[i])

        await page.locator('[data-testid="guests-apply"]').click()
        await page.wait_for_timeout(T["after_guests_apply"])

        if self.rooms == 1 and self.rooms_config[0] == 2:
            try:
                await page.locator('button:has-text("Search")').first.click()
            except Exception:
                pass

        # Wait for results
        try:
            await page.wait_for_url(
                lambda url: "placeId" in url or "/hotels" in url,
                timeout=T["url_change"],
            )
        except Exception:
            pass

        await page.wait_for_selector(
            '[data-testid="hotel-search-result"], .p-hotelCard__content',
            timeout=T["selector_wait"],
        )

        print(f"[NUITEE] {self.city}: waiting for results…", flush=True)
        try:
            await page.wait_for_selector(
                '[data-testid="hotel-search-result"]', timeout=T["selector_wait"]
            )
            result_text = await page.locator(
                '[data-testid="hotel-search-result"]'
            ).inner_text()
            total_match = re.search(r'(\d[\d,]*)', result_text.replace(",", ""))
            total_expected = int(total_match.group(1)) if total_match else 9999
            print(
                f"[NUITEE] {self.city}: {total_expected} properties found",
                flush=True,
            )
        except Exception:
            total_expected = 9999

        # ── Scroll & Harvest ──────────────────────────────────────────────────
        all_hotels: dict[str, dict] = {}
        seen_names: set[str]        = set()

        SCROLL_MIN   = 400
        SCROLL_MAX   = 1800
        SCROLL_BOOST = 200
        scroll_step  = SCROLL_MIN
        MAX_STALLS   = 4
        stall_streak = 0
        TAIL_WINDOW  = 30
        scroll_started = time.monotonic()
        pass_count = 0

        async def _harvest_tail() -> int:
            added   = 0
            locator = page.locator('[data-testid="hotel-card"], .p-hotelCard')
            total_rendered = await locator.count()
            start = max(0, total_rendered - TAIL_WINDOW)
            for idx in range(start, total_rendered):
                card = locator.nth(idx)
                try:
                    name_el = card.locator(
                        '.p-hotelCard__content__top__title h3'
                    ).first
                    name = _normalise_text((await name_el.inner_text()).strip())
                    if not name or name in seen_names:
                        continue
                    price_el = card.locator(
                        '.p-hotelCard__content__bottom__right__price__new'
                    ).first
                    raw     = (await price_el.inner_text()).strip()
                    cleaned = (
                        raw
                        .replace("MAD",     "")
                        .replace(",",       "")
                        .replace("/ night", "")
                        .replace("/night",  "")
                        .strip()
                    )
                    m     = re.search(r'[\d.]+', cleaned)
                    price = float(m.group()) if m else 0.0

                    rating: float | None = None
                    try:
                        r_el   = card.locator(
                            '.p-hotelCard__content__top__score__number'
                        ).first
                        rating = float((await r_el.inner_text()).strip())
                    except Exception:
                        pass

                    seen_names.add(name)
                    all_hotels[name] = {
                        "name":         name,
                        "nuitee_price": price,
                        "rating":       rating,
                    }
                    added += 1
                except Exception:
                    continue
            return added

        while True:
            if api_blobs:
                for blob in api_blobs:
                    try:
                        b_name  = _normalise_text(str(blob.get("name", "")))
                        b_price = float(blob.get("price", 0))
                        if b_name and b_name not in seen_names:
                            seen_names.add(b_name)
                            all_hotels[b_name] = {
                                "name":         b_name,
                                "nuitee_price": b_price,
                                "rating":       blob.get("rating"),
                            }
                    except Exception:
                        pass
                api_blobs.clear()

            new_this_pass = await _harvest_tail()
            unique_now    = len(all_hotels)
            print(
                f"[NUITEE] {self.city}: {unique_now}/{total_expected} "
                f"(+{new_this_pass} | scroll={scroll_step}px)",
                flush=True,
            )

            if unique_now >= total_expected:
                break

            pass_count += 1
            if pass_count >= MAX_SCROLL_PASSES:
                print(
                    f"[NUITEE] {self.city}: pass limit reached ({MAX_SCROLL_PASSES}).",
                    flush=True,
                )
                break
            if (time.monotonic() - scroll_started) >= MAX_SCROLL_SECONDS:
                print(
                    f"[NUITEE] {self.city}: time limit reached ({MAX_SCROLL_SECONDS}s).",
                    flush=True,
                )
                break

            if new_this_pass == 0:
                stall_streak += 1
                scroll_step   = min(scroll_step + SCROLL_BOOST, SCROLL_MAX)
                if stall_streak >= MAX_STALLS:
                    print(f"[NUITEE] {self.city}: stall limit reached.", flush=True)
                    break
            else:
                stall_streak = 0
                scroll_step  = SCROLL_MIN

            try:
                eol = page.locator(
                    '[data-testid="end-of-results"], .p-noResults, '
                    '[class*="endOfList"], [class*="no-more-results"]'
                ).first
                if await eol.is_visible(timeout=150):
                    print(f"[NUITEE] {self.city}: end-of-list.", flush=True)
                    await _harvest_tail()
                    break
            except Exception:
                pass

            await page.evaluate(f"window.scrollBy(0, {scroll_step})")
            if not api_blobs:
                await page.wait_for_timeout(T["scroll_settle"])

        hotels = [
            {
                "name":         h["name"],
                "nuitee_price": h["nuitee_price"],
                **({"rating": h["rating"]} if h["rating"] is not None else {}),
            }
            for h in all_hotels.values()
        ]
        print(
            f"[NUITEE] {self.city}: harvest done — {len(hotels)} hotels.",
            flush=True,
        )
        await context.close()
        return hotels

    # ──────────────────────────────────────────────────────────────────────────
    # Booking.com price fetcher
    # Navigate → Dismiss Modals → Click Search → Fuzzy Match → Extract Price
    # ──────────────────────────────────────────────────────────────────────────
    async def get_booking_price(
        self,
        browser:    Browser,
        hotel_name: str,
    ) -> tuple[float | None, str | None, str]:
        """
        Returns (price, direct_url, match_status).

        Interaction pattern (mirrors verify_and_correct.py):
          1. Build enriched query: "Hotel Name City"
          2. Navigate to search URL
          3. Dismiss modals
          4. Fuzzy-check first card name
             • Match  → scrape price
             • No match → click Search button, wait, fuzzy-check again
             • Still no match → MATCH_FAIL
          5. Capture post-search page.url as the direct URL
        """
        expected_name = _normalise_text(hotel_name)

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

        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins',  { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages',{ get: () => ['en-US','en'] });
            window.chrome = { runtime: {} };
            const orig = window.navigator.permissions.query;
            window.navigator.permissions.query = (p) =>
                p.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : orig(p);
        """)

        async def _auto_dismiss(loc):
            try:
                await loc.first.click(timeout=T["popup_dismiss"], force=True)
            except Exception:
                pass

        await page.add_locator_handler(page.locator(POPUP_SELECTORS), _auto_dismiss)

        price:        float | None = None
        direct_url:   str | None   = None
        match_status: str          = "ERROR"

        try:
            # ── Step 1: build enriched query (Hotel Name + City) ──────────────
            enriched_query = f"{expected_name} {self.city}"
            query_enc      = urllib.parse.quote(enriched_query)
            search_url     = (
                f"https://www.booking.com/searchresults.html"
                f"?ss={query_enc}"
                f"&checkin={self.checkin}"
                f"&checkout={self.checkout}"
                f"&selected_currency=MAD"
                f"&lang=en-us"
                f"&group_adults={self.total_adults}"
                f"&no_rooms={self.rooms}"
            )

            # ── Step 2: navigate ──────────────────────────────────────────────
            await page.goto(
                search_url, wait_until="domcontentloaded", timeout=T["page_load"]
            )
            await page.wait_for_timeout(T["after_goto"])

            # ── Step 3: dismiss modals ────────────────────────────────────────
            await _dismiss_modals(page)
            await page.wait_for_timeout(400)

            # Always trigger Booking's internal search resolution before scraping.
            await _force_search(page)
            try:
                await page.wait_for_load_state("networkidle", timeout=T["post_search"])
            except Exception:
                await page.wait_for_timeout(T["after_search_click"])

            await _dismiss_modals(page)
            direct_url = page.url

            best_idx, best_name, best_score = await _best_booking_card(page, expected_name)

            if best_idx is not None and best_score >= FUZZY_THRESHOLD:
                card = page.locator('[data-testid="property-card"]').nth(best_idx)
                price = await _extract_price_from_card(card)
                match_status = "OK" if price else "NO_RESULTS"
                print(
                    f"[BOOKING] {expected_name[:45]} → best='{best_name or 'N/A'}' "
                    f"score={best_score:.0f} price={price}",
                    flush=True,
                )
            elif await _booking_no_availability(page):
                match_status = "NO_RESULTS"
                print(
                    f"[BOOKING] {expected_name[:45]} → NO_RESULTS (availability)",
                    flush=True,
                )
            else:
                match_status = "MATCH_FAIL"
                print(
                    f"[BOOKING] {expected_name[:45]} → MATCH_FAIL "
                    f"(best='{best_name or 'N/A'}' score={best_score:.0f})",
                    flush=True,
                )

        except Exception as exc:
            print(f"[BOOKING] {expected_name[:45]} → exception: {exc}", flush=True)
            match_status = "ERROR"
        finally:
            await context.close()

        return price, direct_url, match_status

    # ──────────────────────────────────────────────────────────────────────────
    # Orchestrator
    # ──────────────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                slow_mo=0,
                args=BROWSER_ARGS,
            )

            hotels = await self.get_nuitee_data(browser)

            if hotels:
                sem = asyncio.Semaphore(self.booking_concurrency)

                async def _fetch_booking(hotel: dict) -> tuple[float | None, str | None, str]:
                    async with sem:
                        return await self.get_booking_price(browser, hotel["name"])

                booking_results = await asyncio.gather(
                    *[_fetch_booking(hotel) for hotel in hotels]
                )

                for hotel, (b_price, b_url, m_status) in zip(hotels, booking_results):
                    hotel_name = _normalise_text(str(hotel["name"]))

                    if b_price is not None:
                        diff      = round(b_price - hotel["nuitee_price"], 2)
                        diff_str  = f"{diff:+.2f}"
                        b_display = f"{b_price:.2f}"
                    else:
                        diff_str  = "N/A"
                        b_display = "N/A / SOLD OUT" if m_status != "MATCH_FAIL" else "MATCH_FAIL"

                    print(
                        f"[RESULT] {hotel_name[:50]} → {b_display} MAD "
                        f"(diff: {diff_str})  [{m_status}]",
                        flush=True,
                    )

                    # COL_URL is last: see CSV_HEADER
                    _append_csv_row([
                        hotel_name,
                        self.city,
                        self.checkin,
                        self.checkout,
                        str(self.rooms_config),
                        self.rooms,
                        f"{hotel['nuitee_price']:.2f}",
                        b_display,
                        diff_str,
                        m_status,
                        b_url or "",      # ← Booking URL last
                    ])

            if not hotels:
                print(f"[NUITEE] No hotels found for {self.city}", flush=True)

            await browser.close()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _ensure_csv_header()

    input_path = Path("input.json")
    if not input_path.is_file():
        print(f"[ERROR] Input file not found at {input_path}")
        raise SystemExit(1)

    with input_path.open(encoding="utf-8") as f:
        inputs = json.load(f)

    for params in inputs:
        city = params["city"]
        try:
            print(f"\n=== {city} ({params['checkin']} → {params['checkout']}) ===")
            scraper = PriceCompare(
                city         = city,
                checkin      = params["checkin"],
                checkout     = params["checkout"],
                rooms_config = _parse_rooms_config(params),
            )
            asyncio.run(scraper.run())
        except Exception as e:
            print(f"[ERROR] {city}: {e}", flush=True)