import asyncio
import csv
import re
import urllib.parse
import random
import json
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

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
# Timeouts (ms) — tuned for Oracle Cloud server latency
# ---------------------------------------------------------------------------
T = {
    "page_load":          60_000,
    "network_idle":       60_000,
    "url_change":         90_000,   # bumped: London/large cities take longer to redirect
    "first_card":         45_000,   # bumped: large cities load slower
    "selector_wait":      45_000,
    "element_visible":     5_000,
    "price_selector":      5_000,
    "cookie_btn":          5_000,
    "confirm_btn":         2_000,
    "popup_dismiss":       3_000,
    "suggestion_click":   10_000,   # bumped: autocomplete can be slow on cold load
    "load_more_btn":       3_000,
    "after_goto":          2_000,
    "after_destination":   1_500,
    "calendar_open":       1_500,
    "after_day_click":       800,
    "after_guests_open":   1_000,
    "after_guests_apply":    800,
    "adult_btn_click":       150,
    "add_room":              800,
    # Scroll: 2.5 s per iteration gives the IntersectionObserver time to fire + XHR to return
    "scroll_settle":       2_500,
    "next_month_btn":        500,
}

# ---------------------------------------------------------------------------
# Browser args (container-safe)
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
    "no availability",
    "sold out",
    "unavailable",
    "we don't have any available",
    "no rooms available",
]

POPUP_SELECTORS = ", ".join([
    'button[aria-label="Dismiss sign-in info."]',
    'button[aria-label="Close"]',
    'button[aria-label="Dismiss"]',
    '[data-testid="modal-mask"]',
    'button.modal-mask-closeBtn',
    '#b2searchresultsPage button[class*="close"]',
])

OUTPUT_CSV = Path(__file__).parent / "data.csv"
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
# Input normalisation
# ---------------------------------------------------------------------------

def _parse_rooms_config(params: dict) -> list[int]:
    """
    Accepted formats in input.json:
      A) Simple (same adults in every room):
         { "rooms": 2, "adults": 3 }
         → [3, 3]

      B) Per-room (explicit adults per room):
         { "rooms_config": [3, 2] }
         → [3, 2]   (room 1 has 3 adults, room 2 has 2 adults)

      C) Old default (no rooms/adults key):
         {}  → [2]
    """
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
        """
        rooms_config: list of adults per room, e.g. [3, 2] means
                      room 1 = 3 adults, room 2 = 2 adults.
        """
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
        await page.goto(self.nuitee_url, wait_until="networkidle", timeout=T["network_idle"])

        # Cookie banner
        try:
            await page.locator('[data-testid="accept-button"]').click(timeout=T["cookie_btn"])
        except Exception:
            pass

        # ── Destination ────────────────────────────────────────────────
        search_input = page.get_by_placeholder("Enter a destination")
        await search_input.click()
        await search_input.type(self.city, delay=80)
        await page.wait_for_timeout(T["after_destination"])

        # Wait for the dropdown list to appear, then pick the first <li> that
        # matches the city name but is NOT an airport or region entry.
        # We try up to 3 times in case the dropdown is slow to render.
        suggestion_clicked = False
        for attempt in range(3):
            try:
                # Primary: target <li> elements specifically (avoids header/map divs)
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
                    # Clear and retype to re-trigger autocomplete
                    await search_input.triple_click()
                    await search_input.type(self.city, delay=80)
                    await page.wait_for_timeout(T["after_destination"])

        if not suggestion_clicked:
            # Last-resort fallback: click whatever first suggestion appears
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
                        await page.wait_for_timeout(380)
                        return True
                except:
                    pass
            return False

        async def _navigate_to_month(target_dt: datetime):
            iso = target_dt.strftime("%Y-%m-%d")
            for _ in range(36):
                try:
                    cell = page.locator(f'[data-date="{iso}"]').first
                    if await cell.is_visible(timeout=300):
                        return
                except:
                    pass
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
            try:
                day_selector = f".vc-day.id-{iso} .vc-day-content"
                cell = page.locator(day_selector).first
                if await cell.is_visible(timeout=2000):
                    await cell.click(force=True)
                    return
            except: pass
            try:
                aria_label = dt.strftime("%A, %b %-d, %Y")
                cell = page.get_by_role("button", name=aria_label).first
                if await cell.is_visible(timeout=1000):
                    await cell.click()
                    return
            except: pass
            try:
                await page.evaluate("""
                    (targetIso) => {
                        const selector = `.id-${targetIso} .vc-day-content`;
                        const el = document.querySelector(selector);
                        if (el) { el.click(); return true; }
                        const dayNum = targetIso.split('-')[2].replace(/^0/, '');
                        const allDays = document.querySelectorAll('.vc-day-content');
                        for (const d of allDays) {
                            if (d.textContent.trim() === dayNum && !d.parentElement.classList.contains('is-not-in-month')) {
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
        await page.wait_for_timeout(300)
        await _click_day(checkin_dt)
        await page.wait_for_timeout(500)

        await _navigate_to_month(checkout_dt)
        await page.wait_for_timeout(300)
        await _click_day(checkout_dt)
        await page.wait_for_timeout(500)

        for confirm_sel in (
            'button[aria-label="Done"]',
            'button:has-text("Done")',
            'button:has-text("Apply")',
            '[data-testid="date-apply"]',
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
        await page.wait_for_timeout(600)

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

        # ── Wait for results page ──────────────────────────────────────
        try:
            await page.wait_for_url(
                lambda url: "placeId" in url or "/hotels" in url,
                timeout=T["url_change"]
            )
        except Exception:
            # URL didn't change but page may have still loaded cards (London case)
            pass

        # Wait for at least 1 card — if this times out the city genuinely returned nothing
        await page.wait_for_selector('[data-testid="hotel-search-result"], .p-hotelCard__content',
                                     timeout=T["selector_wait"])

        # ── FIX #1: Scroll the INNER container to trigger all lazy cards ──
        #
        # The Nuitee portal renders cards in a fixed-height scrollable div,
        # NOT document.body. We must find that container and scroll it.
        # We also use scroll_into_view on the last card to fire IntersectionObserver.

        # Give the first batch of cards time to fully render before we start scrolling
        await page.wait_for_timeout(2_000)

        async def _load_all_cards(max_iter: int = 300) -> None:
            """
            Scroll the inner results container until card count stops growing.
            Count is read BEFORE each scroll so we know the baseline and only
            increment stale when a full scroll cycle produces nothing new.
            300 iterations × 2.5 s = 12.5 min max (enough for 400+ hotels).
            Stops early after 5 consecutive stale rounds.
            """
            # Seed prev from what is already in the DOM
            prev  = await page.locator(".p-hotelCard__content").count()
            stale = 0

            for _ in range(max_iter):
                # 1. Find & scroll the scrollable container
                await page.evaluate("""
                    () => {
                        // Try known Nuitee result container selectors first
                        const candidates = [
                            document.querySelector('.p-results'),
                            document.querySelector('.p-hotelList'),
                            document.querySelector('[data-testid="hotel-list"]'),
                            document.querySelector('[class*="resultList"]'),
                            document.querySelector('[class*="hotelList"]'),
                        ];
                        for (const el of candidates) {
                            if (el) { el.scrollTop = el.scrollHeight; return; }
                        }
                        // Fallback: walk up from the first card to find scrollable ancestor
                        const card = document.querySelector('.p-hotelCard__content');
                        if (!card) {
                            window.scrollTo(0, document.body.scrollHeight);
                            return;
                        }
                        let node = card.parentElement;
                        while (node && node !== document.body) {
                            const s = window.getComputedStyle(node);
                            if (/auto|scroll/.test(s.overflow + s.overflowY)
                                && node.scrollHeight > node.clientHeight) {
                                node.scrollTop = node.scrollHeight;
                                return;
                            }
                            node = node.parentElement;
                        }
                        window.scrollTo(0, document.body.scrollHeight);
                    }
                """)

                # 2. Also bring the last card into viewport (fires IntersectionObserver)
                try:
                    await page.locator(".p-hotelCard__content").last.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass

                await page.wait_for_timeout(T["scroll_settle"])

                cur = await page.locator(".p-hotelCard__content").count()
                if cur == prev:
                    stale += 1
                    if stale >= 5:
                        break
                else:
                    stale = 0
                    prev = cur

        await _load_all_cards()

        # ── Parse cards ────────────────────────────────────────────────
        hotels = []
        for card in await page.locator(".p-hotelCard__content").all():
            try:
                name = await card.locator(
                    ".p-hotelCard__content__top__title h3"
                ).inner_text()
                price_raw = await card.locator(
                    ".p-hotelCard__content__bottom__right__price__new"
                ).inner_text()
                price_val = float(
                    re.sub(r"[^\d.]", "", price_raw.split("/")[0].replace(",", ""))
                )
                hotels.append({"name": name.strip(), "nuitee_price": price_val})
            except Exception:
                pass

        print(f"[NUITEE] {self.city}: {len(hotels)} hotel(s) found")
        await context.close()
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
            # FIX #2: pass total_adults and rooms to Booking so pricing matches
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
                headless=False,
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
                    str(self.rooms_config),   # e.g. "[3, 2]"
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

    input_path = Path(__file__).parent / "input.json"
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