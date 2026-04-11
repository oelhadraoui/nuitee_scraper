import asyncio
import csv
import re
import urllib.parse
import random
import json
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, Route, Request

try:
    from playwright_stealth import stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    print("[WARN] playwright_stealth not installed. Run: pip install playwright-stealth")

from dotenv import load_dotenv
import os
load_dotenv()


# ---------------------------------------------------------------------------
# Timeouts (ms)
# ---------------------------------------------------------------------------
T = {
    "page_load":          60_000,
    "network_idle":       60_000,
    "url_change":         45_000,
    "first_card":         45_000,
    "selector_wait":      45_000,
    "element_visible":     5_000,
    "price_selector":      5_000,
    "cookie_btn":          5_000,
    "confirm_btn":         2_000,
    "popup_dismiss":       3_000,
    "suggestion_click":   10_000,
    "load_more_btn":       3_000,
    "after_goto":          2_000,
    "after_destination":   1_500,
    "calendar_open":       1_500,
    "after_day_click":       800,
    "after_guests_open":   1_000,
    "after_guests_apply":    800,
    "adult_btn_click":       150,
    "add_room":              800,
    "scroll_settle":         800,   # shorter: we're driven by network events now
    "next_month_btn":        500,
}

# ---------------------------------------------------------------------------
# Browser args
# ---------------------------------------------------------------------------
BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--window-size=1280,800",
    "--blink-settings=imagesEnabled=false,fontsEnabled=false",
    "--disable-gpu",
    "--disable-setuid-sandbox",
    "--no-zygote",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

PRICE_SELECTORS = [
    '[data-testid="price-and-discounted-price"]',
    '[data-testid="recommended-units-price"]',
    '.bui-price-display__value',
    '.prco-valign__middle-helper',
    'span[aria-label*="MAD"]',
    'span[aria-label*="price"]',
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

OUTPUT_CSV = Path("/app/output/prices.csv")
CSV_HEADER = [
    "Hotel Name", "City", "Check-in", "Check-out",
    "Adults per Room", "Rooms",
    "Nuitee Price (MAD)", "Booking Price (MAD)",
    "Price Difference (MAD)", "Booking URL",
]


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def _ensure_csv_header():
    if not OUTPUT_CSV.exists():
        with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)

def _append_csv_row(row: list):
    with OUTPUT_CSV.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)


# ---------------------------------------------------------------------------
# Price parser
# ---------------------------------------------------------------------------

def _parse_price(text: str, aggressive: bool = False) -> float | None:
    text = text.replace("\xa0", " ").replace("\u202f", " ").replace(",", "")
    if aggressive:
        for c in re.findall(r'\b(\d{3,6}(?:\.\d{1,2})?)\b', text):
            val = float(c)
            if 100 <= val <= 99_999:
                return val
        return None
    m = re.search(r'([\d\s]+(?:\.\d{1,2})?)', text)
    if m:
        cleaned = re.sub(r'\s+', '', m.group(1))
        try:
            return float(cleaned)
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Hotel data extractor — works on any JSON shape the API returns
# ---------------------------------------------------------------------------

def _extract_hotels_from_json(data) -> list[dict]:
    """
    Recursively walk a JSON structure and pull out objects that look like
    hotel listings: must have a name-like key and a price-like key.
    Returns list of {"name": str, "nuitee_price": float}.
    """
    results = []

    NAME_KEYS  = {"name", "hotelName", "hotel_name", "title", "propertyName",
                  "property_name", "displayName", "display_name"}
    PRICE_KEYS = {"price", "rate", "totalPrice", "total_price", "lowestPrice",
                  "lowest_price", "pricePerNight", "price_per_night",
                  "basePrice", "base_price", "amount", "totalRate", "total_rate",
                  "displayPrice", "display_price", "netPrice", "net_price"}

    def _walk(obj):
        if isinstance(obj, dict):
            name  = None
            price = None
            for k, v in obj.items():
                kl = k.lower()
                if any(nk.lower() == kl for nk in NAME_KEYS) and isinstance(v, str) and len(v) > 2:
                    name = v.strip()
                if any(pk.lower() == kl for pk in PRICE_KEYS):
                    try:
                        price = float(str(v).replace(",", "").replace(" ", ""))
                    except (ValueError, TypeError):
                        pass
            if name and price and price > 0:
                results.append({"name": name, "nuitee_price": price})
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(data)
    return results


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------

def _parse_rooms_config(params: dict) -> list[int]:
    if "rooms_config" in params:
        return [int(a) for a in params["rooms_config"]]
    adults = int(params.get("adults", 2))
    rooms  = int(params.get("rooms",  1))
    return [adults] * rooms


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class PriceCompare:
    def __init__(self, city: str, checkin: str, checkout: str,
                 rooms_config: list[int] = None):
        self.nuitee_url   = os.getenv("NUITEE_URL", "https://amine-sadik-2911w.nuitee.link/")
        self.city         = city
        self.checkin      = checkin
        self.checkout     = checkout
        self.rooms_config = rooms_config or [2]
        self.rooms        = len(self.rooms_config)
        self.total_adults = sum(self.rooms_config)

    # ------------------------------------------------------------------
    # Nuitee scraper
    # ------------------------------------------------------------------
    async def get_nuitee_data(self, browser) -> list[dict]:
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page    = await context.new_page()

        # ── Network interception ───────────────────────────────────────
        # Collect every JSON API response that comes in while we scroll.
        # We store raw parsed JSON blobs; after scrolling we extract hotels.
        api_blobs: list = []
        api_urls:  list = []

        async def _capture_response(response):
            """Called for every network response. Grab JSON from API calls."""
            url = response.url
            ct  = response.headers.get("content-type", "")
            # Only look at JSON responses from the same origin or known API paths
            if "json" not in ct:
                return
            # Skip tiny responses (e.g. analytics pings)
            try:
                body = await response.body()
                if len(body) < 100:
                    return
                data = json.loads(body)
                api_blobs.append(data)
                api_urls.append(url)
            except Exception:
                pass

        page.on("response", _capture_response)

        await page.goto(self.nuitee_url, wait_until="networkidle", timeout=T["network_idle"])

        try:
            await page.locator('[data-testid="accept-button"]').click(timeout=T["cookie_btn"])
        except Exception:
            pass

        # ── Destination ────────────────────────────────────────────────
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
                await dropdown_li.wait_for(state="visible", timeout=T["suggestion_click"])
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
                fallback = page.locator("ul li").first
                await fallback.wait_for(state="visible", timeout=T["suggestion_click"])
                await fallback.click()
            except Exception:
                pass

        # ── Date picker ────────────────────────────────────────────────
        await page.locator('[data-testid="date-picker"]').click()
        await page.wait_for_timeout(T["calendar_open"])

        checkin_dt  = datetime.strptime(self.checkin,  "%Y-%m-%d")
        checkout_dt = datetime.strptime(self.checkout, "%Y-%m-%d")

        async def _read_visible_month_year() -> tuple[int, int] | None:
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
                except:
                    pass
            if found:
                return min(found)
            try:
                all_text = await page.evaluate("""
                    () => {
                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                        const texts = [];
                        let node;
                        while (node = walker.nextNode()) {
                            const t = node.textContent.trim();
                            if (t.length > 3) texts.push(t);
                        }
                        return texts;
                    }
                """)
                months = ["January","February","March","April","May","June",
                        "July","August","September","October","November","December"]
                for t in all_text:
                    for m in months:
                        match = re.match(rf'^{m}\s+(\d{{4}})$', t.strip())
                        if match:
                            found.append((int(match.group(1)), months.index(m) + 1))
                if found:
                    return min(found)
            except:
                pass
            return None

        async def _click_next_month():
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
                except:
                    pass
            return False

        async def _is_active_date_visible(dt: datetime) -> bool:
            """
            Returns True only if the target date is visible in an ACTIVE month cell
            (i.e., not a ghost/overflow day marked with .is-not-in-month).
            Also verifies the visible month header matches the target year/month.
            """
            iso = dt.strftime("%Y-%m-%d")  # always zero-padded: 2026-05-08

            # Strategy 1: strict CSS — cell must NOT carry the out-of-month class
            for not_in_month_cls in ("is-not-in-month", "not-in-month", "outside-month"):
                try:
                    cell = page.locator(
                        f'.vc-day.id-{iso}:not(.{not_in_month_cls}) .vc-day-content'
                    ).first
                    if await cell.is_visible(timeout=300):
                        return True
                except:
                    pass

            # Strategy 2: data-date attribute present and cell not out-of-month
            try:
                active = await page.evaluate("""
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
                """, iso)
                if active:
                    return True
            except:
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

                # In a dual-pane calendar the right pane shows cur+1, so stop
                # advancing once the target month is either pane.
                if tgt_total <= cur_total + 1:
                    return

                if not await _click_next_month():
                    return

        async def _click_day(dt: datetime):
            iso = dt.strftime("%Y-%m-%d")  # always zero-padded

            # Strategy 1: strict locator — active cell only, no ghost days
            for not_in_month_cls in ("is-not-in-month", "not-in-month", "outside-month"):
                try:
                    cell = page.locator(
                        f'.vc-day.id-{iso}:not(.{not_in_month_cls}) .vc-day-content'
                    ).first
                    if await cell.is_visible(timeout=2000):
                        await cell.click(force=True)
                        return
                except:
                    pass

            # Strategy 2: aria-label (active cells typically have a full date label)
            try:
                aria_label = dt.strftime("%A, %b %-d, %Y")
                # Ensure we do NOT hit a ghost cell by scoping to a visible, active parent
                cell = page.locator(
                    f'[aria-label="{aria_label}"]'
                    ':not(.is-not-in-month)'
                ).first
                if await cell.is_visible(timeout=1000):
                    await cell.click()
                    return
            except:
                pass

            # Strategy 3: JS fallback — explicitly skip out-of-month elements
            try:
                await page.evaluate("""
                    (targetIso) => {
                        // Strict selector first
                        const strict = document.querySelector(
                            `.vc-day.id-${targetIso}:not(.is-not-in-month) .vc-day-content`
                        );
                        if (strict) { strict.click(); return 'strict'; }

                        // Fallback: match by day number but skip ghost cells
                        const dayNum = targetIso.split('-')[2].replace(/^0/, '');
                        const allDays = document.querySelectorAll('.vc-day-content');
                        for (const d of allDays) {
                            const parent = d.closest('.vc-day');
                            if (
                                d.textContent.trim() === dayNum &&
                                parent &&
                                !parent.classList.contains('is-not-in-month') &&
                                !parent.classList.contains('not-in-month') &&
                                !parent.classList.contains('outside-month')
                            ) {
                                d.click();
                                return 'text-match';
                            }
                        }
                        return false;
                    }
                """, iso)
            except:
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
            except:
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
            dec = row.locator("button").nth(0)
            inc = row.locator("button").nth(1)
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

        # edge case if gests is default 2 and one room: we need to click the sherch button to trigger the search
        if self.rooms == 1 and self.rooms_config[0] == 2:
            try:
                await page.locator('button:has-text("Search")').first.click()
            except Exception:
                pass

        # ── Wait for results page ──────────────────────────────────────
        try:
            await page.wait_for_url(
                lambda url: "placeId" in url or "/hotels" in url,
                timeout=T["url_change"]
            )
        except Exception:
            pass

        await page.wait_for_selector(
            '[data-testid="hotel-search-result"], .p-hotelCard__content',
            timeout=T["selector_wait"]
        )

        # ── Wait for result count banner ───────────────────────────────
        print(f"[NUITEE] {self.city}: waiting for results to load...", flush=True)
        try:
            await page.wait_for_selector('[data-testid="hotel-search-result"]',
                                         timeout=T["selector_wait"])
            result_text = await page.locator('[data-testid="hotel-search-result"]').inner_text()
            total_match = re.search(r'(\d[\d,]*)', result_text.replace(",", ""))
            total_expected = int(total_match.group(1)) if total_match else 9999
            print(f"[NUITEE] {self.city}: page reports {total_expected} properties", flush=True)
        except Exception:
            total_expected = 9999
            print(f"[NUITEE] {self.city}: could not read result count", flush=True)

        # ── Scroll & Harvest ───────────────────────────────────────────
        print(f"[NUITEE] {self.city}: starting optimised scroll harvest…", flush=True)

        all_hotels : dict[str, dict]  = {}   # name → record  (O(1) upsert)
        seen_names : set[str]         = set() # O(1) membership test

        # Scroll dynamics
        SCROLL_MIN      = 400    # px — normal step
        SCROLL_MAX      = 1800   # px — leapfrog step
        SCROLL_BOOST    = 200    # px added each stall iteration
        scroll_step     = SCROLL_MIN

        # Stall / exit guards
        MAX_STALLS      = 6
        stall_streak    = 0
        last_count      = 0

        # How many tail-cards to re-inspect each iteration.
        # Only the bottom of the DOM has new nodes; no need to re-read the top.
        TAIL_WINDOW     = 20

        async def _harvest_tail() -> int:
            """
            Inspect only the last TAIL_WINDOW cards in the DOM.
            Returns the number of *new* hotels added this pass.
            """
            added = 0
            locator  = page.locator('[data-testid="hotel-card"], .p-hotelCard')
            total_rendered = await locator.count()

            # Clamp window to actual count so we never go negative
            start = max(0, total_rendered - TAIL_WINDOW)

            for idx in range(start, total_rendered):
                card = locator.nth(idx)
                try:
                    # ── Name ──────────────────────────────────────────
                    name_el = card.locator(
                        '.p-hotelCard__content__top__title h3'
                    ).first
                    name = (await name_el.inner_text()).strip()
                    if not name or name in seen_names:
                        continue                          # O(1) check

                    # ── Price ─────────────────────────────────────────
                    price_el = card.locator(
                        '.p-hotelCard__content__bottom__right__price__new'
                    ).first
                    raw = (await price_el.inner_text()).strip()
                    cleaned = (
                        raw
                        .replace("MAD", "")
                        .replace(",",   "")
                        .replace("/ night", "")
                        .replace("/night",  "")
                        .strip()
                    )
                    m = re.search(r'[\d.]+', cleaned)
                    price = float(m.group()) if m else 0.0

                    # ── Rating ────────────────────────────────────────
                    rating: float | None = None
                    try:
                        r_el   = card.locator(
                            '.p-hotelCard__content__top__score__number'
                        ).first
                        rating = float((await r_el.inner_text()).strip())
                    except Exception:
                        pass

                    # ── Commit ────────────────────────────────────────
                    seen_names.add(name)
                    all_hotels[name] = {
                        "name":         name,
                        "nuitee_price": price,
                        "rating":       rating,
                    }
                    added += 1

                except Exception:
                    continue   # stale / recycled node — skip silently

            return added

        # ── Main leapfrog loop ─────────────────────────────────────────
        while True:
            # ── Prioritise api_blobs if data has already arrived ───────
            if api_blobs:                                 # opt-4: skip sleep if ready
                for blob in api_blobs:
                    try:
                        b_name  = blob.get("name", "").strip()
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
                api_blobs.clear()                         # opt-5: free memory immediately

            # ── DOM tail harvest ──────────────────────────────────────
            new_this_pass = await _harvest_tail()
            unique_now    = len(all_hotels)

            print(
                f"[NUITEE] {self.city}: "
                f"{unique_now}/{total_expected} unique "
                f"(+{new_this_pass} this pass | scroll={scroll_step}px)",
                flush=True,
            )

            # ── Exit: target reached ───────────────────────────────────
            if unique_now >= total_expected:
                print(f"[NUITEE] {self.city}: target reached.", flush=True)
                break

            # ── Stall logic + variable scroll step ────────────────────
            if new_this_pass == 0:
                stall_streak += 1
                # Leapfrog: grow the scroll jump each consecutive stall
                scroll_step = min(scroll_step + SCROLL_BOOST, SCROLL_MAX)  # opt-3

                if stall_streak >= MAX_STALLS:
                    print(
                        f"[NUITEE] {self.city}: "
                        f"{MAX_STALLS} stalls — harvest complete.",
                        flush=True,
                    )
                    break
            else:
                stall_streak = 0
                scroll_step  = SCROLL_MIN    # reset to fine-grain on progress

            last_count = unique_now

            # ── End-of-list sentinel (cheap check, no sleep) ──────────
            try:
                eol = page.locator(
                    '[data-testid="end-of-results"], '
                    '.p-noResults, '
                    '[class*="endOfList"], '
                    '[class*="no-more-results"]'
                ).first
                if await eol.is_visible(timeout=150):
                    print(f"[NUITEE] {self.city}: end-of-list sentinel.", flush=True)
                    await _harvest_tail()    # final sweep
                    break
            except Exception:
                pass

            # ── Scroll ────────────────────────────────────────────────
            await page.evaluate(f"window.scrollBy(0, {scroll_step})")

            # opt-4: only sleep if api_blobs is empty (no data arrived yet)
            if not api_blobs:
                await page.wait_for_timeout(T.get("scroll_settle", 500))

        # ── Serialise ─────────────────────────────────────────────────
        hotels: list[dict] = [
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
        return hotels

    # ------------------------------------------------------------------
    # Booking.com scraper
    # ------------------------------------------------------------------
    async def get_booking_price(self, browser, hotel_name: str) -> tuple[float | None, str | None]:
        ua = random.choice(USER_AGENTS)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=ua,
            locale="en-US",
            timezone_id="Europe/Paris",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.google.com/",
                "DNT": "1",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        page = await context.new_page()

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

        async def _dismiss(locator):
            try:
                await locator.first.click(timeout=T["popup_dismiss"], force=True)
            except Exception:
                pass

        await page.add_locator_handler(page.locator(POPUP_SELECTORS), _dismiss)

        try:
            await page.goto("https://www.booking.com",
                            wait_until="domcontentloaded", timeout=T["page_load"])
            await page.wait_for_timeout(T["after_goto"])

            query = urllib.parse.quote(hotel_name)
            search_url = (
                f"https://www.booking.com/searchresults.html"
                f"?ss={query}"
                f"&checkin={self.checkin}"
                f"&checkout={self.checkout}"
                f"&selected_currency=MAD"
                f"&lang=en-us"
                f"&group_adults={self.total_adults}"
                f"&no_rooms={self.rooms}"
                f"&dest_type=hotel"
                f"&nflt=ht_id%3D204"
            )

            await page.goto(search_url, wait_until="domcontentloaded", timeout=T["page_load"])
            final_url = page.url

            card_loc = page.locator('[data-testid="property-card"]')
            try:
                await card_loc.first.wait_for(state="visible", timeout=T["first_card"])
            except Exception:
                return None, final_url

            first_card = card_loc.first
            card_text  = (await first_card.inner_text()).lower()

            if any(s in card_text for s in SOLD_OUT_SIGNALS):
                has_price = any([
                    await first_card.locator(sel).first.is_visible(timeout=T["element_visible"])
                    for sel in PRICE_SELECTORS
                ])
                if not has_price:
                    return None, final_url

            for sel in PRICE_SELECTORS:
                try:
                    el = first_card.locator(sel).first
                    if await el.is_visible(timeout=T["price_selector"]):
                        raw = await el.inner_text()
                        price = _parse_price(raw)
                        if price and price > 0:
                            return price, final_url
                except Exception:
                    pass

            price = _parse_price(card_text, aggressive=True)
            if price and price > 0:
                return price, final_url

            return None, final_url

        except Exception:
            return None, None
        finally:
            await context.close()

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------
    async def run(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                slow_mo=0,
                args=BROWSER_ARGS,
            )

            hotels = await self.get_nuitee_data(browser)

            for hotel in hotels:
                b_price, b_url = await self.get_booking_price(browser, hotel["name"])

                if b_price is not None:
                    diff      = b_price - hotel["nuitee_price"]
                    diff_str  = f"{diff:+.2f}"
                    b_display = f"{b_price:.2f}"
                else:
                    diff_str  = "N/A"
                    b_display = "N/A / SOLD OUT"

                print(f"[BOOKING] {hotel['name'][:50]} → {b_display} MAD (diff: {diff_str})")
                _append_csv_row([
                    hotel["name"],
                    self.city,
                    self.checkin,
                    self.checkout,
                    str(self.rooms_config),
                    self.rooms,
                    f"{hotel['nuitee_price']:.2f}",
                    b_display,
                    diff_str,
                    b_url or "",
                ])

            if not hotels:
                print(f"[NUITEE] No hotels found for {self.city}")

            await browser.close()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _ensure_csv_header()

    # input passed via --input flag load them
    # old code before using docker
    input_path = Path("/app/input.json")
    if not input_path.is_file():
        print(f"[ERROR] Input file not found at {input_path}")
        exit(1)

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
            print(f"[ERROR] {city}: {e}")