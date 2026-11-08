"""
src/scrapers/nuitee.py
=============================================================================
Nuitee scraper: search -> results harvest -> hotel detail -> checkout tunnel
-> FINAL PAYABLE PRICE.

This is the module the client's Requirement A lives in. The public surface is
deliberately small:

    scraper = NuiteeScraper(manager, settings, registry)
    hotels  = await scraper.harvest(window)                  # results page
    priced  = await scraper.price_hotels(window, hotels)     # tunnel walks

``harvest`` is fast and wide (one page, many hotels). ``price_hotels`` is slow
and deep (one tunnel walk per hotel, bounded concurrency), which is why they
are separate: a failure in one hotel's tunnel must never cost you the harvest.

The heavy lifting of the tunnel itself lives in ``src/scrapers/tunnel.py``;
``TunnelNavigator`` is re-exported here so callers only ever import from
``src.scrapers.nuitee``.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from typing import Optional
from urllib.parse import urljoin

from playwright.async_api import Page

from src.config import Settings
from src.corridors import country_labels
from src.models import NuiteeHotel, SearchWindow, TunnelResult, TunnelStatus
from src.scrapers.tunnel import TunnelNavigator
from src.utils.browser import (
    BrowserManager,
    dismiss_overlays,
    human_scroll,
    is_visible,
    safe_text,
)
from src.utils.hotel_ids import IdentityCapture
from src.utils.logging_setup import get_logger
from src.utils.pricing import parse_price
from src.utils.selectors import SelectorRegistry
from src.utils.text import normalise_display, normalise_key, parse_distance_km

log = get_logger(__name__)

__all__ = ["NuiteeScraper", "TunnelNavigator"]


class NuiteeSearchError(RuntimeError):
    """The search form could not be driven to a results page."""


class NuiteeScraper:
    """Drives the Nuitee white-label site end to end."""

    def __init__(
        self,
        manager: BrowserManager,
        settings: Settings,
        registry: SelectorRegistry,
    ) -> None:
        self.manager = manager
        self.settings = settings
        self.registry = registry
        self._t = settings.timeouts
        self._cfg = settings.nuitee
        self._overlays = registry.get_optional("nuitee.overlays").candidates

    # ======================================================================
    # PHASE 1 - results harvest
    # ======================================================================

    async def harvest(self, window: SearchWindow) -> list[NuiteeHotel]:
        """:meth:`_harvest` under the ``timeouts.harvest_hard`` deadline."""
        budget = self._t.harvest_hard / 1000
        try:
            return await asyncio.wait_for(self._harvest(window), timeout=budget)
        except asyncio.TimeoutError:
            log.error(
                "[%s %s] harvest exceeded %.0fs - skipping this window",
                window.city, window.horizon_label, budget,
            )
            return []

    async def _harvest(self, window: SearchWindow) -> list[NuiteeHotel]:
        """
        Fill the search form for ``window`` and harvest the results list.

        Returns hotels sorted cheapest-first and truncated to
        ``NUITEE_HOTELS_PER_CITY`` - the tunnel walk is the expensive step, so
        we spend it on the offers most likely to be competitive.

        Before the page/context closes, every kept hotel's real detail URL is
        captured by clicking through (see ``_capture_detail_urls``): the
        results-list cards carry no ``href`` at all on this site, so this is
        the only reliable way to populate ``NuiteeHotel.detail_url``.
        """
        async with self.manager.page(referer=self._cfg.url) as (_, page):
            # Live 2026-11-04: on a freshly launched browser the first one or
            # two searches failed within ~4s with "destination input not
            # found" (Casablanca, Marrakech, Paris), then every later window
            # worked - a cold-start page, not a broken selector. Each failure
            # cost a whole window; a fresh navigation fixes it.
            attempts = 3
            for attempt in range(1, attempts + 1):
                try:
                    await self._open_and_search(page, window)
                    break
                except Exception as exc:
                    if attempt == attempts:
                        log.error(
                            "[%s %s] search failed after %d attempts: %s",
                            window.city, window.horizon_label, attempts, exc,
                        )
                        return []
                    log.warning(
                        "[%s %s] search attempt %d failed (%s) - reloading",
                        window.city, window.horizon_label, attempt, exc,
                    )
                    await page.wait_for_timeout(2_000 * attempt)

            hotels = await self._harvest_results(page, window)
            hotels.sort(
                key=lambda h: (h.display_price is None, h.display_price or float("inf"))
            )
            limited = hotels[: self._cfg.hotels_per_city]

            await self._capture_detail_urls(page, limited)

        with_url = sum(1 for h in limited if h.detail_url)
        log.info(
            "[%s %s %s] harvested %d hotels, keeping %d (%d with a detail URL)",
            window.city, window.horizon_label, window.checkin_str,
            len(hotels), len(limited), with_url,
        )
        return limited

    async def _capture_detail_urls(
        self, page: Page, hotels: list[NuiteeHotel]
    ) -> None:
        """
        Click through to each hotel's detail page to capture its real URL.

        The results list has no ``<a href>`` at all - navigation happens via
        a ``window.open()`` call constructed at click-time inside a JS click
        handler (confirmed live: ``hasAttribute('href') === false`` on every
        card link). The site's own name-filter input
        (``[data-testid="hotel-name-filter"]``) is used to isolate one card
        at a time before clicking, rather than trusting a scroll-pass index -
        the results list is virtualised and re-renders, so an index recorded
        during the harvest pass can point at a different hotel by the time
        this runs.

        Each click is expected to open a new tab/popup (``window.open``);
        that popup's URL is read and then the popup is closed immediately -
        we only need the URL here, the actual tunnel walk happens later in
        its own dedicated page.
        """
        filter_cascade = self.registry.get_optional("nuitee.hotel_name_filter")
        card_cascade = self.registry.get("nuitee.hotel_card")
        link_cascade = self.registry.get_optional("nuitee.see_availability_link")

        filter_field = None
        if filter_cascade.candidates:
            filter_field = await filter_cascade.first_visible(
                page, timeout=self._t.element_visible
            )

        for hotel in hotels:
            try:
                if filter_field is not None:
                    await filter_field.fill("")
                    await filter_field.type(hotel.name[:60], delay=20)
                    await page.wait_for_timeout(self._t.after_destination)

                cards, count = await card_cascade.all_matching(page, timeout=3_000)
                if cards is None or count == 0:
                    continue
                card = cards.first

                popup = None
                try:
                    async with page.expect_popup(timeout=self._t.tunnel_cta) as popup_info:
                        clicked = await self._click_availability(card, link_cascade)
                        if not clicked:
                            raise RuntimeError("no availability link was clickable")
                    popup = await popup_info.value
                    await popup.wait_for_load_state(
                        "domcontentloaded", timeout=self._t.page_load
                    )
                    hotel.detail_url = popup.url
                finally:
                    if popup is not None:
                        try:
                            await popup.close()
                        except Exception:
                            pass
            except Exception as exc:
                log.debug(
                    "detail-url capture failed for %r: %s", hotel.name[:40], exc
                )
                continue

    async def _click_availability(self, card, link_cascade) -> bool:
        """Click a card's "See availability" trigger, restricted to that card."""
        for candidate in link_cascade.candidates:
            try:
                locator = card.locator(candidate).first
                if not await locator.is_visible(timeout=800):
                    continue
                await locator.click(timeout=self._t.tunnel_cta)
                return True
            except Exception:
                continue
        # Last resort: the card itself is often the click target.
        try:
            await card.click(timeout=self._t.tunnel_cta)
            return True
        except Exception:
            return False

    # -- search form driving ------------------------------------------------

    async def _open_and_search(self, page: Page, window: SearchWindow) -> None:
        await page.goto(
            self._cfg.url, wait_until="domcontentloaded", timeout=self._t.page_load
        )
        await page.wait_for_timeout(self._t.after_goto)

        await self.registry.get_optional("nuitee.cookie_accept").click(
            page, timeout=self._t.cookie_btn
        )
        if self._overlays:
            await dismiss_overlays(page, self._overlays)

        await self._fill_destination(page, window.city)
        await self._fill_dates(page, window.checkin, window.checkout)
        await self._fill_guests(page, list(window.rooms_config))
        await self._submit_search(page)
        await self._wait_for_results(page, window)

    async def _fill_destination(self, page: Page, city: str) -> None:
        """Type the city and pick the matching city-level suggestion."""
        input_cascade = self.registry.get("nuitee.destination_input")
        field = await input_cascade.first_visible(page, timeout=self._t.element_visible)
        if field is None:
            raise NuiteeSearchError("destination input not found")

        suggestion_cascade = self.registry.get("nuitee.destination_suggestion")

        for attempt in range(3):
            try:
                await field.click()
                await field.fill("")
                await field.type(city, delay=80)
            except Exception as exc:
                raise NuiteeSearchError(f"cannot type destination: {exc}") from exc

            await page.wait_for_timeout(self._t.after_destination)

            if await self._click_city_suggestion(page, suggestion_cascade, city):
                return

            log.debug("destination suggestion retry %d for %r", attempt + 1, city)

        # No suggestion is the city itself. Pressing Enter (or clicking the
        # first row) is what scraped "Rome" in Romania and "Faro" in the
        # Faroe Islands - better to lose the window than to store it.
        raise NuiteeSearchError(f"no destination suggestion matched {city!r}")

    async def _click_city_suggestion(
        self, page: Page, cascade, city: str
    ) -> bool:
        """
        Choose the CITY suggestion, not an airport, a namesake or a hotel.

        Rows read "<place> / <where>" ("Faro / Portugal", "Faroe Islands",
        "Venice Beach / CA, USA"), so see :func:`pick_city_suggestion`. Live
        2026-11: picking the shortest matching label scraped Turin in
        Thuringia, Faro in the Faroe Islands and Venice in Los Angeles.
        """
        countries = country_labels(city)

        for candidate in cascade.candidates:
            try:
                items = page.locator(candidate)
                await items.first.wait_for(
                    state="visible", timeout=self._t.suggestion_click
                )
                count = min(await items.count(), 12)
            except Exception:
                continue

            texts = [await safe_text(items.nth(i), timeout=500) for i in range(count)]
            best_index = pick_city_suggestion(texts, city, countries)
            if best_index is None:
                log.debug("no city suggestion for %r among %r", city, texts)
                continue

            try:
                await items.nth(best_index).click(timeout=4_000)
                await page.wait_for_timeout(400)
                return True
            except Exception:
                continue

        return False

    async def _fill_dates(self, page: Page, checkin: date, checkout: date) -> None:
        """Open the calendar, page forward to each month, click both days."""
        opened = await self.registry.get("nuitee.date_picker").click(
            page, timeout=self._t.element_visible
        )
        if opened is None:
            raise NuiteeSearchError("date picker did not open")
        await page.wait_for_timeout(self._t.calendar_open)

        await self._navigate_to_month(page, checkin)
        await self._click_day(page, checkin)
        await page.wait_for_timeout(self._t.after_day_click)

        await self._navigate_to_month(page, checkout)
        await self._click_day(page, checkout)
        await page.wait_for_timeout(self._t.after_day_click)

        await self.registry.get_optional("nuitee.calendar_confirm").click(
            page, timeout=2_000
        )

    async def _read_visible_month(self, page: Page) -> Optional[tuple[int, int]]:
        """Earliest (year, month) currently rendered in the calendar."""
        cascade = self.registry.get("nuitee.calendar_month_title")
        found: list[tuple[int, int]] = []

        for candidate in cascade.candidates:
            try:
                texts = await page.locator(candidate).all_inner_texts()
            except Exception:
                continue
            for text in texts:
                parsed = _parse_month_year(text)
                if parsed:
                    found.append(parsed)

        return min(found) if found else None

    async def _navigate_to_month(self, page: Page, target: date) -> None:
        """Click 'next month' until ``target`` is rendered as an active day."""
        day_cascade = self.registry.get("nuitee.calendar_day").format(
            iso=target.isoformat()
        )
        next_cascade = self.registry.get("nuitee.calendar_next_month")

        for _ in range(36):
            if await day_cascade.any_present(page, timeout=350):
                return

            current = await self._read_visible_month(page)
            if current is not None:
                current_index = current[0] * 12 + current[1]
                target_index = target.year * 12 + target.month
                # Calendars usually render two months side by side.
                if target_index <= current_index + 1:
                    return

            if await next_cascade.click(page, timeout=1_500) is None:
                return
            await page.wait_for_timeout(self._t.next_month_btn)

    async def _click_day(self, page: Page, target: date) -> None:
        """Click a specific day cell, with a JS fallback for odd calendars."""
        cascade = self.registry.get("nuitee.calendar_day").format(iso=target.isoformat())
        if await cascade.click(page, timeout=2_500, force=True) is not None:
            return

        # aria-label form: "Thursday, Aug 1, 2026"
        try:
            label = target.strftime("%A, %b %d, %Y").replace(" 0", " ")
            locator = page.locator(f'[aria-label="{label}"]').first
            if await is_visible(locator, timeout=1_000):
                await locator.click(force=True)
                return
        except Exception:
            pass

        # Final fallback: match the day number among in-month cells.
        try:
            await page.evaluate(
                """
                (iso) => {
                  const dayNum = String(parseInt(iso.split('-')[2], 10));
                  const outOfMonth = ['is-not-in-month', 'not-in-month', 'outside-month'];
                  const cells = document.querySelectorAll(
                    '.vc-day-content, [role="gridcell"], td button'
                  );
                  for (const cell of cells) {
                    if (cell.textContent.trim() !== dayNum) continue;
                    const parent = cell.closest('.vc-day') || cell.parentElement;
                    if (parent && outOfMonth.some((c) => parent.classList.contains(c))) {
                      continue;
                    }
                    cell.click();
                    return true;
                  }
                  return false;
                }
                """,
                target.isoformat(),
            )
        except Exception:
            log.debug("day click fallback failed for %s", target)

    async def _fill_guests(self, page: Page, rooms_config: list[int]) -> None:
        """Set the adult count per room, adding rooms as needed."""
        if await self.registry.get("nuitee.guests_button").click(
            page, timeout=self._t.element_visible
        ) is None:
            log.debug("guests button not found - using the site default occupancy")
            return
        await page.wait_for_timeout(self._t.after_guests_open)

        await self._set_room_adults(page, 0, rooms_config[0])

        add_room = self.registry.get_optional("nuitee.guests_add_room")
        for index in range(1, len(rooms_config)):
            if await add_room.click(page, timeout=3_000) is None:
                log.warning("cannot add room %d - occupancy will be partial", index + 1)
                break
            await page.wait_for_timeout(self._t.add_room)
            await self._set_room_adults(page, index, rooms_config[index])

        await self.registry.get_optional("nuitee.guests_apply").click(page, timeout=3_000)
        await page.wait_for_timeout(self._t.after_guests_apply)

    async def _set_room_adults(self, page: Page, room_index: int, target: int) -> None:
        """Click +/- until the room's adult stepper reads ``target``."""
        panel_cascade = self.registry.get("nuitee.guests_room_panel").format(
            n=room_index + 1
        )
        panel = await panel_cascade.first_visible(page, timeout=1_500)
        scope = panel if panel is not None else page

        row_cascade = self.registry.get("nuitee.guests_stepper_row")
        row = await row_cascade.first_visible(scope, timeout=1_500)
        if row is None:
            log.debug("no adult stepper for room %d", room_index + 1)
            return

        value_cascade = self.registry.get("nuitee.guests_stepper_value")
        raw = await value_cascade.text(row, timeout=800)
        try:
            current = int(re.sub(r"\D", "", raw or "")) if raw else 2
        except ValueError:
            current = 2

        delta = target - current
        if delta == 0:
            return

        buttons = row.locator("button")
        try:
            button = buttons.nth(1 if delta > 0 else 0)
        except Exception:
            return

        for _ in range(abs(delta)):
            try:
                await button.click(timeout=1_500)
            except Exception:
                break
            await page.wait_for_timeout(self._t.adult_btn_click)

    async def _submit_search(self, page: Page) -> None:
        await self.registry.get_optional("nuitee.search_submit").click(
            page, timeout=self._t.element_visible
        )

    async def _wait_for_results(self, page: Page, window: SearchWindow) -> None:
        """Wait for either the URL to change or result cards to render."""
        try:
            await page.wait_for_url(
                lambda url: "placeId" in url or "/hotels" in url or "search" in url,
                timeout=self._t.url_change,
            )
        except Exception:
            pass

        ready = self.registry.get("nuitee.results_ready")
        if not await ready.wait_for(page, timeout=self._t.selector_wait):
            raise NuiteeSearchError(
                f"no results rendered for {window.city} {window.checkin_str}"
            )

    # -- results harvesting -------------------------------------------------

    async def _expected_total(self, page: Page) -> int:
        """Parse the '1,234 properties found' banner, if present."""
        text = await self.registry.get_optional("nuitee.results_count").text(
            page, timeout=2_000
        )
        if not text:
            return 0
        match = re.search(r"(\d[\d,\s]*)", text.replace("\xa0", ""))
        if not match:
            return 0
        try:
            return int(re.sub(r"\D", "", match.group(1)))
        except ValueError:
            return 0

    async def _harvest_results(
        self, page: Page, window: SearchWindow
    ) -> list[NuiteeHotel]:
        """
        Infinite-scroll harvest with stall detection and a hard time budget.

        Terminates on: target count reached, end-of-results marker, N passes
        with zero new hotels, pass limit, or the time budget - whichever comes
        first. Every branch is logged so a short harvest is explainable.
        """
        expected = await self._expected_total(page)
        target = self._cfg.hotels_per_city
        card_cascade = self.registry.get("nuitee.hotel_card")
        eol_cascade = self.registry.get_optional("nuitee.end_of_results")

        collected: dict[str, NuiteeHotel] = {}
        too_far: set[str] = set()
        stalls = 0
        scroll_step = 600
        started = datetime.now()
        base_url = page.url

        for pass_index in range(self._cfg.max_scroll_passes):
            added = await self._harvest_visible_cards(
                page, card_cascade, collected, base_url, too_far
            )

            elapsed = (datetime.now() - started).total_seconds()
            log.debug(
                "[%s] pass %d: +%d (total %d/%s) %.0fs",
                window.city, pass_index, added, len(collected),
                expected or "?", elapsed,
            )

            if len(collected) >= target:
                log.debug("[%s] target of %d hotels reached", window.city, target)
                break
            if expected and len(collected) >= expected:
                break
            if elapsed >= self._cfg.max_scroll_seconds:
                log.info("[%s] harvest time budget reached", window.city)
                break

            if added == 0:
                stalls += 1
                scroll_step = min(scroll_step + 300, 2_200)
                if stalls >= self._cfg.scroll_stall_limit:
                    log.debug("[%s] harvest stalled - stopping", window.city)
                    break
            else:
                stalls = 0
                scroll_step = 600

            if await eol_cascade.any_present(page, timeout=200):
                log.debug("[%s] end-of-results marker", window.city)
                await self._harvest_visible_cards(
                    page, card_cascade, collected, base_url, too_far
                )
                break

            await human_scroll(page, scroll_step, steps=3)
            await page.wait_for_timeout(self._t.scroll_settle)

        if too_far:
            log.info(
                "[%s] dropped %d result(s) more than %g km from the centre",
                window.city, len(too_far), self._cfg.max_distance_km,
            )
        return list(collected.values())

    async def _harvest_visible_cards(
        self,
        page: Page,
        card_cascade,
        collected: dict[str, NuiteeHotel],
        base_url: str,
        too_far: set[str],
    ) -> int:
        """
        Parse rendered cards into ``collected``, keyed by normalised name.

        Cards further than ``NUITEE_MAX_DISTANCE_KM`` from the centre go into
        ``too_far`` instead: they still count as progress for stall
        detection, but never reach the tunnel or the output.

        Only the tail of the list is scanned on each pass: virtualised lists
        re-render constantly, and re-parsing 800 cards every pass is what made
        the original harvest O(n^2).
        """
        cards, count = await card_cascade.all_matching(page, timeout=3_000)
        if cards is None or count == 0:
            return 0

        name_cascade = self.registry.get("nuitee.hotel_card_name")
        price_cascade = self.registry.get("nuitee.hotel_card_price")
        rating_cascade = self.registry.get_optional("nuitee.hotel_card_rating")
        link_cascade = self.registry.get_optional("nuitee.hotel_card_link")

        window_size = 40
        start = max(0, count - window_size) if len(collected) else 0

        added = 0
        for index in range(start, count):
            card = cards.nth(index)
            try:
                raw_name = await name_cascade.text(card, timeout=500)
                if not raw_name:
                    continue
                name = normalise_display(raw_name)
                key = normalise_key(name)
                if not key or key in collected or key in too_far:
                    continue

                max_km = self._cfg.max_distance_km
                distance_km = parse_distance_km(await card.inner_text(timeout=500))
                if max_km and distance_km is not None and distance_km > max_km:
                    too_far.add(key)
                    added += 1
                    continue

                raw_price = await price_cascade.text(card, timeout=500)
                parsed = parse_price(
                    raw_price or "",
                    min_value=self.settings.quality.min_price_mad,
                    max_value=self.settings.quality.max_price_mad,
                    default_currency=self._cfg.currency,
                    strategy="min",
                )

                rating = None
                raw_rating = await rating_cascade.text(card, timeout=350)
                if raw_rating:
                    try:
                        rating = float(re.sub(r"[^\d.]", "", raw_rating))
                    except ValueError:
                        rating = None

                detail_url = None
                link = await link_cascade.first_attached(card, timeout=350)
                if link is not None:
                    href = await link.get_attribute("href")
                    if href:
                        detail_url = urljoin(base_url, href)

                collected[key] = NuiteeHotel(
                    name=name,
                    display_price=parsed.amount_mad,
                    currency=parsed.currency,
                    rating=rating,
                    detail_url=detail_url,
                    card_index=index,
                    distance_km=distance_km,
                )
                added += 1
            except Exception:
                continue

        return added

    # ======================================================================
    # PHASE 2 - tunnel pricing
    # ======================================================================

    async def price_hotels(
        self, window: SearchWindow, hotels: list[NuiteeHotel]
    ) -> dict[str, TunnelResult]:
        """
        Walk the checkout tunnel for every hotel, bounded by
        ``NUITEE_TUNNEL_CONCURRENCY``.

        Returns a mapping of normalised hotel name -> TunnelResult. Failures
        are returned as results with an error status, never raised: one dead
        tunnel must not abort the batch.
        """
        if not self.settings.tunnel.enabled:
            log.warning("tunnel disabled - falling back to display prices")
            return {
                normalise_key(h.name): TunnelResult(
                    status=TunnelStatus.NOT_ATTEMPTED,
                    final_price=h.display_price,
                    display_price=h.display_price,
                    detail_url=h.detail_url,
                )
                for h in hotels
            }

        semaphore = asyncio.Semaphore(self._cfg.tunnel_concurrency)
        results: dict[str, TunnelResult] = {}

        async def _one(hotel: NuiteeHotel) -> None:
            async with semaphore:
                results[normalise_key(hotel.name)] = await self._price_one(window, hotel)

        await asyncio.gather(*(_one(h) for h in hotels), return_exceptions=True)

        succeeded = sum(1 for r in results.values() if r.succeeded)
        log.info(
            "[%s %s] tunnel: %d/%d hotels priced",
            window.city, window.horizon_label, succeeded, len(hotels),
        )
        return results

    async def _price_one(
        self, window: SearchWindow, hotel: NuiteeHotel
    ) -> TunnelResult:
        """Walk one hotel's tunnel in its own isolated browser context."""
        if not hotel.detail_url:
            return TunnelResult(
                status=TunnelStatus.ERROR,
                display_price=hotel.display_price,
                error="no detail URL harvested for this hotel",
            )

        detail_url = self._with_search_params(hotel.detail_url, window)

        async def _walk() -> TunnelResult:
            async with self.manager.page(referer=self._cfg.url) as (_, page):
                # The detail page fetches its own /v1/hotels/details/{id};
                # reading that response gives the Booking id + coordinates
                # for free (see src/utils/hotel_ids.py).
                capture = IdentityCapture(page, detail_url)
                navigator = TunnelNavigator(
                    page, self.settings, self.registry, hotel_name=hotel.name
                )
                result = await navigator.run(
                    detail_url, display_price=hotel.display_price
                )
                result.identity = await capture.result()
                return result

        budget = self._t.tunnel_hotel_hard / 1000
        try:
            return await asyncio.wait_for(_walk(), timeout=budget)
        except asyncio.TimeoutError:
            log.warning("[%s] tunnel exceeded %.0fs - abandoned", hotel.name[:40], budget)
            return TunnelResult(
                status=TunnelStatus.ERROR,
                display_price=hotel.display_price,
                detail_url=detail_url,
                error=f"hard timeout: tunnel walk exceeded {budget:.0f}s",
            )
        except Exception as exc:
            log.warning("[%s] tunnel context failed: %s", hotel.name[:40], exc)
            return TunnelResult(
                status=TunnelStatus.ERROR,
                display_price=hotel.display_price,
                detail_url=detail_url,
                error=f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _with_search_params(url: str, window: SearchWindow) -> str:
        """
        Ensure the detail URL carries the stay parameters.

        Loading a detail page without dates makes the SPA fall back to its own
        defaults, which would silently price the wrong stay - the most
        dangerous class of bug in this pipeline, because the output still
        looks perfectly valid.
        """
        if "checkin" in url and "checkout" in url:
            return url
        separator = "&" if "?" in url else "?"
        return (
            f"{url}{separator}checkin={window.checkin_str}"
            f"&checkout={window.checkout_str}"
            f"&adults={window.total_adults}"
            f"&rooms={window.rooms}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MONTH_FORMATS = ("%B %Y", "%b %Y", "%B, %Y", "%b, %Y")


def _parse_month_year(text: str) -> Optional[tuple[int, int]]:
    """Parse a calendar header like 'August 2026' into (2026, 8)."""
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > 32:
        return None
    for fmt in _MONTH_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return parsed.year, parsed.month
        except ValueError:
            continue
    return None


_NON_CITY = ("airport", "aeroport", "station", "district", "region", "province")


def pick_city_suggestion(
    texts: list[str], city: str, countries: tuple[str, ...] = ()
) -> Optional[int]:
    """
    Index of the autocomplete row that IS ``city``, or ``None``.

    Only a row whose place name (the part before " / ") equals the city is
    eligible - prefixes ("Faroe Islands" for Faro), longer names ("Medina
    del Campo") and hotels named after the city never are. When the city's
    country is known, the row must also name it ("Los Angeles / CA, USA",
    not "Los Ángeles / Chile"). Ties go to the earliest row: Nuitee lists
    its best guess first.
    """
    target = normalise_key(city)
    wanted = [normalise_key(c) for c in countries]
    for index, text in enumerate(texts):
        place, _, where = (text or "").replace("\n", " / ").partition(" / ")
        if normalise_key(place) != target:
            continue
        if any(token in place.lower() for token in _NON_CITY):
            continue
        # Hotel/address rows carry a street ("Cancun / PASEO NEPTUNO,65,
        # Gandia"); a country check rules those out when one is known.
        if wanted and not any(w in normalise_key(where) for w in wanted):
            continue
        return index
    return None
