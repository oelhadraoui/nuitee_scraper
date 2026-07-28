"""
src/scrapers/booking.py
=============================================================================
Booking.com lookup, fuzzy matching, and price extraction.

Matching strategy
-----------------
The baseline implementation trusted the FIRST result card. Booking.com's
ranking is personalised and promotion-driven, so the first card is frequently
a sponsored property that has nothing to do with the query - the "sticky
recommendation" bug. Two defences are applied here:

1. **Query enrichment.** The search string is ``"<hotel name> <city>"`` unless
   the name already contains the city, which prevents "Le Meridien" from
   resolving to a Le Meridien on a different continent.

2. **Best-of-N scoring.** Every card in the top ``max_cards_to_scan`` is
   scored, and the best one wins - but only if it clears the threshold. A
   card that is merely *the best available* is still rejected when it is not
   *good enough*, and the row is marked ``MATCH_FAIL`` rather than being
   filled with a plausible-looking wrong number.

An adaptive threshold applies: when the hotel name is short (<= 2 meaningful
tokens, e.g. "Riad Fes"), false positives are far more likely, so the stricter
``fuzzy_threshold_strict`` is used instead.

The stale-deep-link bug (live-diagnosed, 2026-07)
--------------------------------------------------
A deep-linked ``searchresults.html?ss=...`` URL is NOT the same thing as a
real search: Booking only resolves ``ss`` to a specific property (a
``dest_id``/``dest_type`` pair) server-side, in response to the on-page
search actually being submitted. Loaded cold, the SAME URL routinely renders
a STALE card claiming "Unavailable on our site for your selected dates" for
a property that has real, bookable inventory seconds later.

This was confirmed live for three unrelated properties (a 5-star chain hotel,
a mid-range independent, and a boutique riad) - all three showed the false
"unavailable" card on the raw deep link, and all three revealed a normal,
priced card the instant the on-page Search button was clicked (which
attaches ``dest_id``/``dest_type`` to the URL). ``_resolve_destination()``
performs and verifies that click - checking the URL actually gained a
``dest_id``, not just that the click didn't raise - before any card is
scored or any price is read. Without this verification, the previous
implementation would silently score and "fail to extract a price from" a
stale, unresolved page, which is exactly why so many well-known, obviously
available hotels were coming back as ``NO_RESULTS``.

A second, different failure mode also came out of the same investigation:
some property names (e.g. "Ennesma Hotel") never resolve via the ``ss=``
querystring at all - Booking's autocomplete offers only city/airport-level
suggestions, never the property. For these, clicking Search just re-runs a
city-wide search, and the best-scoring card is whatever chain hotel Booking
ranks first for that city - explaining the occasional wildly-wrong
``MATCH_FAIL``. ``_resolve_destination()`` falls back to driving the
destination box's own autocomplete (type -> wait for suggestions -> click
the top one) when the plain click doesn't produce a ``dest_id``; if that
still doesn't resolve, the property is most likely absent from Booking's
index under this name, and the row is correctly left unmatched rather than
attributed to a random nearby hotel.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import Optional

from playwright.async_api import Page

from src.config import Settings
from src.models import BookingResult, MatchStatus, SearchWindow
from src.utils.browser import (
    BrowserManager,
    dismiss_overlays,
    install_overlay_autodismiss,
    safe_text,
)
from src.utils.logging_setup import get_logger
from src.utils.pricing import parse_price
from src.utils.retry import polite_delay
from src.utils.selectors import SelectorRegistry
from src.utils.text import contains_city, fuzzy_score, normalise_display, normalise_key

log = get_logger(__name__)

__all__ = ["BookingBlockedError", "BookingScraper"]


class BookingBlockedError(RuntimeError):
    """Booking.com served a captcha / bot-check page."""


class BookingScraper:
    """Looks up one hotel at a time on Booking.com and extracts its price."""

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
        self._cfg = settings.booking
        self._overlays = registry.get_optional("booking.overlays").candidates
        self._captcha_markers = registry.strings("booking.captcha_markers")
        self._no_avail_markers = registry.strings("booking.no_availability_markers")

    # ======================================================================
    # Public API
    # ======================================================================

    async def lookup_many(
        self, window: SearchWindow, hotel_names: list[str]
    ) -> dict[str, BookingResult]:
        """
        Look up every hotel with bounded concurrency.

        Returns ``{normalised_name: BookingResult}``. Never raises.
        """
        semaphore = asyncio.Semaphore(self._cfg.concurrency)
        results: dict[str, BookingResult] = {}

        async def _one(name: str) -> None:
            async with semaphore:
                results[normalise_key(name)] = await self.lookup(window, name)
                await polite_delay(0.3, 1.1)

        await asyncio.gather(*(_one(n) for n in hotel_names), return_exceptions=True)

        matched = sum(1 for r in results.values() if r.status is MatchStatus.OK)
        log.info(
            "[%s %s] booking: %d/%d matched",
            window.city, window.horizon_label, matched, len(hotel_names),
        )
        return results

    async def lookup(self, window: SearchWindow, hotel_name: str) -> BookingResult:
        """
        Look up one hotel, with retries on TRANSIENT failures only.

        The "block point" live-diagnosed 2026-07: a 22-hotel window taking
        13 minutes for 3 matches. Root cause was that ``MATCH_FAIL`` (no
        result card cleared the fuzzy threshold) was retried exactly like
        ``ERROR``, up to ``max_retries`` extra times - but a bad fuzzy score
        is a deterministic read of the SAME search results, not a fluke of
        the network. Retrying it re-runs the entire multi-second page-load +
        destination-resolution sequence for a result that essentially never
        changes, tripling the cost of every hotel Booking simply doesn't
        have a good match for - which, on this site, is most of them. Only
        ``ERROR`` (an exception, timeout, or blocked page) is retried now;
        ``MATCH_FAIL`` and ``NO_RESULTS`` are both treated as terminal,
        successfully-determined outcomes.
        """
        result = BookingResult(status=MatchStatus.ERROR)
        attempts = self._cfg.max_retries + 1

        for attempt in range(1, attempts + 1):
            result.attempts = attempt
            try:
                result = await self._lookup_once(window, hotel_name)
                result.attempts = attempt
            except BookingBlockedError as exc:
                log.warning("[%s] blocked: %s", hotel_name[:40], exc)
                result = BookingResult(
                    status=MatchStatus.ERROR, error=str(exc), attempts=attempt
                )
                # Backing off hard is the only thing that helps with a block.
                await asyncio.sleep(5.0 * attempt)
            except Exception as exc:
                result = BookingResult(
                    status=MatchStatus.ERROR,
                    error=f"{type(exc).__name__}: {exc}",
                    attempts=attempt,
                )

            if result.status is not MatchStatus.ERROR:
                break
            if attempt < attempts:
                await polite_delay(1.0, 2.5)

        return result

    # ======================================================================
    # Single lookup
    # ======================================================================

    async def _lookup_once(
        self, window: SearchWindow, hotel_name: str
    ) -> BookingResult:
        expected = normalise_display(hotel_name)
        search_url = self._build_search_url(window, expected)

        async with self.manager.page(referer="https://www.google.com/") as (_, page):
            if self._overlays:
                await install_overlay_autodismiss(page, self._overlays)

            await page.goto(
                search_url, wait_until="domcontentloaded", timeout=self._t.page_load
            )
            await page.wait_for_timeout(self._t.after_goto)

            if await self._is_blocked(page):
                raise BookingBlockedError("captcha / bot-check page served")

            if self._overlays:
                await dismiss_overlays(page, self._overlays, timeout=self._t.popup_dismiss)

            # Booking resolves the destination server-side only after the
            # search is actually submitted; without this, a deep-linked URL
            # routinely renders a stale "unavailable" result even for
            # properties with real inventory (see the module docstring -
            # live-verified against three unrelated hotels).
            resolved = await self._resolve_destination(page, expected)
            if not resolved:
                log.debug(
                    "[%s] destination never resolved to a dest_id - "
                    "proceeding with whatever the page shows",
                    expected[:40],
                )

            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=self._t.post_search
                )
            except Exception:
                await page.wait_for_timeout(self._t.after_search_click)

            if self._overlays:
                await dismiss_overlays(page, self._overlays, timeout=self._t.popup_dismiss)

            final_url = page.url

            index, matched_name, score = await self._best_match(page, expected, window)
            threshold = self._threshold_for(expected, window.city)

            if index is not None and score >= threshold:
                cards = page.locator(
                    self.registry.get("booking.property_card").combined
                )
                price = await self._extract_card_price(cards.nth(index))

                if price is not None:
                    log.debug(
                        "[%s] matched %r score=%.0f price=%.2f",
                        expected[:40], (matched_name or "")[:40], score, price,
                    )
                    return BookingResult(
                        status=MatchStatus.OK,
                        price=price,
                        currency=self._cfg.currency,
                        matched_name=matched_name,
                        fuzzy_score=score,
                        url=await self._card_url(cards.nth(index), final_url),
                    )

                return BookingResult(
                    status=MatchStatus.NO_RESULTS,
                    matched_name=matched_name,
                    fuzzy_score=score,
                    url=final_url,
                    error="card matched but no price element resolved",
                )

            if await self._is_unavailable(page):
                return BookingResult(
                    status=MatchStatus.NO_RESULTS,
                    matched_name=matched_name,
                    fuzzy_score=score,
                    url=final_url,
                    error="no availability for these dates",
                )

            return BookingResult(
                status=MatchStatus.MATCH_FAIL,
                matched_name=matched_name,
                fuzzy_score=score,
                url=final_url,
                error=(
                    f"best candidate {matched_name!r} scored {score:.0f} "
                    f"< threshold {threshold:.0f}"
                ),
            )

    # ======================================================================
    # URL construction
    # ======================================================================

    def _build_search_url(self, window: SearchWindow, hotel_name: str) -> str:
        """
        Deep link into Booking's results with the exact stay parameters.

        ``selected_currency=MAD`` is what keeps the whole dataset in one
        currency; without it Booking picks by IP geolocation and the output
        silently mixes EUR and MAD rows.
        """
        query = hotel_name
        if not contains_city(hotel_name, window.city):
            query = f"{hotel_name} {window.city}"

        params = {
            "ss": query,
            "checkin": window.checkin_str,
            "checkout": window.checkout_str,
            "group_adults": str(window.total_adults),
            "no_rooms": str(window.rooms),
            "group_children": "0",
            "selected_currency": self._cfg.currency,
            "lang": self._cfg.language,
            "sb_travel_purpose": "leisure",
        }
        return f"{self._cfg.base_url}?{urllib.parse.urlencode(params)}"

    # ======================================================================
    # Page interaction
    # ======================================================================

    async def _resolve_destination(self, page: Page, expected: str) -> bool:
        """
        Force Booking to actually resolve ``ss`` to a ``dest_id``/``dest_type``
        pair, and verify it did - a click that merely "didn't raise" is not
        evidence the destination resolved (see the module docstring).

        Two tiers, cheapest first:

        1. **Click the on-page Search button** (NOT force-clicked: a natural
           click gets Playwright's built-in "is anything covering this
           element" check, which force=True used to skip - the prime suspect
           for why this silently no-op'd against a not-yet-dismissed overlay
           in production). Retried once with a fresh overlay sweep in
           between, since the sweep can lose a race against a
           just-appeared sign-in/Genius modal.
        2. **Drive the destination box's own autocomplete** (type -> wait for
           the suggestion list -> click the top suggestion -> Search again).
           Needed for properties that ``ss=`` alone can't resolve at all
           (Booking's autocomplete offers no exact match, only city/airport
           level - confirmed live for at least one property name).

        Returns whether a ``dest_id`` was actually attached to the URL.
        """
        submit = self.registry.get_optional("booking.search_submit")

        for _ in range(2):
            if self._overlays:
                await dismiss_overlays(page, self._overlays, timeout=800)
            clicked = await submit.click(page, timeout=self._t.search_btn_visible)
            if clicked is None:
                continue
            if await self._wait_for_dest_id(page):
                return True

        # Tier 2: the plain click never produced a resolved destination.
        # Drive the autocomplete directly instead of trusting the querystring.
        destination = self.registry.get_optional("booking.destination_input")
        field = await destination.first_visible(page, timeout=self._t.element_visible)
        if field is None:
            return False

        try:
            await field.click()
            await field.fill("")
            await field.type(expected[:60], delay=40)
        except Exception:
            return False

        suggestion = self.registry.get_optional("booking.destination_suggestion")
        try:
            top = await suggestion.first_visible(page, timeout=3_000)
        except Exception:
            top = None
        if top is None:
            # No suggestion at all - this property isn't in Booking's
            # autocomplete index under this name.
            return False

        try:
            await top.click(timeout=2_000)
        except Exception:
            return False

        if await self._wait_for_dest_id(page):
            return True

        # The suggestion click sometimes submits on its own; sometimes it
        # only fills the box and still needs an explicit Search.
        if self._overlays:
            await dismiss_overlays(page, self._overlays, timeout=800)
        await submit.click(page, timeout=self._t.search_btn_visible)
        return await self._wait_for_dest_id(page)

    async def _wait_for_dest_id(self, page: Page, timeout: int = 6_000) -> bool:
        """True once the URL carries a real, server-resolved destination."""
        try:
            await page.wait_for_url(lambda url: "dest_id=" in url, timeout=timeout)
            return True
        except Exception:
            return "dest_id=" in page.url

    async def _is_blocked(self, page: Page) -> bool:
        """Detect a captcha / bot-check interstitial."""
        if not self._captcha_markers:
            return False
        try:
            body = (await page.locator("body").inner_text(timeout=4_000)).lower()
        except Exception:
            return False
        return any(marker.lower() in body for marker in self._captcha_markers)

    async def _is_unavailable(self, page: Page) -> bool:
        """Conservative no-availability detection (whole-page text scan)."""
        if not self._no_avail_markers:
            return False
        try:
            body = (await page.locator("body").inner_text(timeout=4_000)).lower()
        except Exception:
            return False
        return any(marker.lower() in body for marker in self._no_avail_markers)

    # ======================================================================
    # Matching
    # ======================================================================

    def _threshold_for(self, hotel_name: str, city: str) -> float:
        """
        Adaptive acceptance threshold.

        Short names carry little information, so a mediocre fuzzy score on
        "Riad Fes" is far more likely to be a false positive than the same
        score on "Sofitel Casablanca Tour Blanche". Those get the strict bar.
        """
        tokens = [t for t in normalise_key(hotel_name).split() if t]
        if len(tokens) <= 2:
            return self._cfg.fuzzy_threshold_strict
        return self._cfg.fuzzy_threshold

    async def _best_match(
        self, page: Page, expected: str, window: SearchWindow
    ) -> tuple[Optional[int], Optional[str], float]:
        """Score every visible card; return (index, name, score) of the best."""
        card_cascade = self.registry.get("booking.property_card")
        name_cascade = self.registry.get("booking.card_name")

        cards, count = await card_cascade.all_matching(page, timeout=self._t.first_card)
        if cards is None or count == 0:
            return None, None, 0.0

        best_index: Optional[int] = None
        best_name: Optional[str] = None
        best_score = 0.0

        for index in range(min(count, self._cfg.max_cards_to_scan)):
            raw = await name_cascade.text(cards.nth(index), timeout=700)
            if not raw:
                continue
            name = normalise_display(raw)
            score = fuzzy_score(expected, name)

            if score > best_score:
                best_index, best_name, best_score = index, name, score

            # An exact normalised hit cannot be beaten - stop scanning.
            if score >= 99.5:
                break

        return best_index, best_name, best_score

    # ======================================================================
    # Extraction
    # ======================================================================

    async def _extract_card_price(self, card) -> Optional[float]:
        """
        Read the price from a property card.

        Booking renders a struck-through original price next to the discounted
        one. The ``min`` strategy therefore picks the *payable* figure, not the
        crossed-out anchor price - the opposite of what a naive `max` sweep
        would do.
        """
        price_cascade = self.registry.get("booking.card_price")

        for candidate in price_cascade.candidates:
            try:
                locator = card.locator(candidate).first
                if not await locator.is_visible(timeout=self._t.price_selector):
                    continue
                raw = await safe_text(locator, timeout=1_200)
                if not raw:
                    continue
                parsed = parse_price(
                    raw,
                    min_value=self.settings.quality.min_price_mad,
                    max_value=self.settings.quality.max_price_mad,
                    default_currency=self._cfg.currency,
                    strategy="min",
                )
                if parsed.ok:
                    return parsed.amount_mad
            except Exception:
                continue

        # Fallback: sweep the whole card. Uses `max` because at this point we
        # are competing with review counts and distances, not anchor prices,
        # and the parser has already stripped those.
        raw_card = await safe_text(card, timeout=1_500)
        if raw_card:
            parsed = parse_price(
                raw_card,
                min_value=self.settings.quality.min_price_mad,
                max_value=self.settings.quality.max_price_mad,
                default_currency=self._cfg.currency,
                strategy="max",
            )
            if parsed.ok:
                return parsed.amount_mad

        return None

    async def _card_url(self, card, fallback: str) -> str:
        """Direct property URL from the card, or the search URL as fallback."""
        link_cascade = self.registry.get_optional("booking.card_link")
        locator = await link_cascade.first_attached(card, timeout=700)
        if locator is not None:
            try:
                href = await locator.get_attribute("href")
                if href:
                    return href.split("?")[0] if href.startswith("http") else fallback
            except Exception:
                pass
        return fallback
