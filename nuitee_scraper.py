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


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--window-size=1280,800",
    "--start-maximized",
    "--blink-settings=imagesEnabled=false,fontsEnabled=false",
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
    "Adults", "Rooms",
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
        candidates = re.findall(r'\b(\d{3,6}(?:\.\d{1,2})?)\b', text)
        for c in candidates:
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

# load env variables from .env file
from dotenv import load_dotenv
import os
load_dotenv()


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class PriceCompare:
    def __init__(self, city, checkin, checkout, adults: int = 2, rooms: int = 1):
        self.nuitee_url = os.getenv("NUITEE_URL")
        self.city     = city
        self.checkin  = checkin
        self.checkout = checkout
        self.adults   = adults
        self.rooms    = rooms

    # ------------------------------------------------------------------
    # Nuitee side
    # ------------------------------------------------------------------
    async def get_nuitee_data(self, browser):
        context = await browser.new_context(viewport={'width': 1280, 'height': 800})
        page = await context.new_page()
        await page.goto(self.nuitee_url, wait_until="networkidle")

        try:
            await page.locator('[data-testid="accept-button"]').click(timeout=3000)
        except:
            pass

        search_input = page.get_by_placeholder("Enter a destination")
        await search_input.click()
        await search_input.type(self.city, delay=100)

        suggestion = (
            page.locator('li, div')
            .filter(has_text=self.city)
            .filter(has_not_text="Airport")
            .first
        )
        await suggestion.click()

        await page.locator('[data-testid="date-picker"]').click()
        await page.wait_for_timeout(800)

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

        async def _set_adults_for_room(room_index: int, target: int):
            room_panel  = page.locator(f'[id="room-{room_index + 1}"]')
            counter_row = room_panel.locator('.p-selectNumber').first
            num_span    = counter_row.locator('.number').first
            try:
                current = int((await num_span.inner_text()).strip())
            except:
                current = 2
            dec_btn = counter_row.locator('button').nth(0)
            inc_btn = counter_row.locator('button').nth(1)
            diff = target - current
            if diff > 0:
                for _ in range(diff):
                    await inc_btn.click()
                    await page.wait_for_timeout(120)
            elif diff < 0:
                for _ in range(abs(diff)):
                    await dec_btn.click()
                    await page.wait_for_timeout(120)

        await _set_adults_for_room(0, self.adults)

        for room_idx in range(1, self.rooms):
            add_room_btn = page.locator('button[aria-label="Add room"]')
            await add_room_btn.click()
            await page.wait_for_timeout(500)
            await _set_adults_for_room(room_idx, self.adults)

        await page.locator('[data-testid="guests-apply"]').click()
        await page.wait_for_timeout(400)

        await page.wait_for_url(lambda url: "placeId" in url, timeout=20000)
        await page.wait_for_selector('.p-hotelCard__content', timeout=25000)

        # ── Scroll until no new cards appear (lazy-load trigger) ───────
        prev_count = 0
        stale_rounds = 0
        while stale_rounds < 3:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)
            cur_count = await page.locator('.p-hotelCard__content').count()
            if cur_count == prev_count:
                stale_rounds += 1
            else:
                stale_rounds = 0
                prev_count = cur_count

        # ── Click "Load more" if the site paginates ────────────────────
        for _ in range(10):
            try:
                load_more = page.locator(
                    'button:has-text("Load more"), button:has-text("Show more"), '
                    '[data-testid="load-more-button"]'
                ).first
                if await load_more.is_visible(timeout=1500):
                    await load_more.click()
                    await page.wait_for_timeout(1500)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(1000)
                else:
                    break
            except:
                break

        # ── Parse all visible cards ────────────────────────────────────
        hotels = []
        cards = await page.locator('.p-hotelCard__content').all()
        for card in cards:
            try:
                name = await card.locator(
                    '.p-hotelCard__content__top__title h3'
                ).inner_text()
                price_raw = await card.locator(
                    '.p-hotelCard__content__bottom__right__price__new'
                ).inner_text()
                price_val = float(
                    re.sub(r'[^\d.]', '', price_raw.split('/')[0].replace(',', ''))
                )
                hotels.append({"name": name.strip(), "nuitee_price": price_val})
            except:
                pass

        print(f"[NUITEE] {self.city}: {len(hotels)} hotel(s) found")
        await context.close()
        return hotels

    # ------------------------------------------------------------------
    # Booking.com side
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
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
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
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) =>
                parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : originalQuery(parameters);
        """)

        async def _dismiss_popup(locator):
            try:
                await locator.first.click(timeout=1500, force=True)
            except:
                pass

        await page.add_locator_handler(
            page.locator(POPUP_SELECTORS),
            _dismiss_popup,
        )

        try:
            await page.goto("https://www.booking.com", wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(random.randint(800, 1800))

            query = urllib.parse.quote(hotel_name)
            search_url = (
                f"https://www.booking.com/searchresults.html"
                f"?ss={query}"
                f"&checkin={self.checkin}"
                f"&checkout={self.checkout}"
                f"&selected_currency=MAD"
                f"&lang=en-us"
                f"&group_adults={self.adults}"
                f"&no_rooms={self.rooms}"
                f"&dest_type=hotel"
                f"&nflt=ht_id%3D204"
            )

            await page.goto(search_url, wait_until="domcontentloaded", timeout=45_000)
            final_url = page.url

            card_locator = page.locator('[data-testid="property-card"]')
            try:
                await card_locator.first.wait_for(state="visible", timeout=20_000)
            except Exception:
                return None, final_url

            first_card = card_locator.first
            card_text = (await first_card.inner_text()).lower()

            if any(signal in card_text for signal in SOLD_OUT_SIGNALS):
                has_price_el = any([
                    await first_card.locator(sel).first.is_visible(timeout=800)
                    for sel in PRICE_SELECTORS
                ])
                if not has_price_el:
                    return None, final_url

            for selector in PRICE_SELECTORS:
                try:
                    el = first_card.locator(selector).first
                    if await el.is_visible(timeout=2_000):
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
                slow_mo=50,
                args=STEALTH_ARGS
            )

            nuitee_results = await self.get_nuitee_data(browser)

            if nuitee_results:
                for hotel in nuitee_results:
                    b_price, b_url = await self.get_booking_price(browser, hotel["name"])

                    if b_price:
                        diff     = b_price - hotel["nuitee_price"]
                        diff_str = f"{diff:+.2f}"
                        b_display = f"{b_price:.2f}"
                    else:
                        diff_str = "N/A"
                        b_display = "N/A / SOLD OUT"

                    print(f"[BOOKING] {hotel['name'][:50]} → {b_display} MAD (diff: {diff_str})")

                    _append_csv_row([
                        hotel["name"],
                        self.city,
                        self.checkin,
                        self.checkout,
                        self.adults,
                        self.rooms,
                        f"{hotel['nuitee_price']:.2f}",
                        b_display,
                        diff_str,
                        b_url or "",
                    ])
            else:
                print(f"[NUITEE] No hotels found for {self.city}")

            await browser.close()


if __name__ == "__main__":
    _ensure_csv_header()

    input_path = Path(__file__).parent / "input.json"
    with input_path.open() as f:
        inputs = json.load(f)

    for params in inputs:
        try:
            print(f"\n=== Processing: {params['city']} ({params['checkin']} to {params['checkout']}) ===")
            comparer = PriceCompare(
                city=params["city"],
                checkin=params["checkin"],
                checkout=params["checkout"],
                adults=params.get("adults", 2),
                rooms=params.get("rooms", 1),
            )
            asyncio.run(comparer.run())
        except Exception as e:
            print(f"[ERROR] Failed to process {params['city']}: {e}")