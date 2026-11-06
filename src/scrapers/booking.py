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

The "verification itself races the thing it's verifying" bug (live-diagnosed, 2026-07)
----------------------------------------------------------------------------------------
A production run showed ~60% of lookups coming back ``NO_RESULTS`` at a
*perfect* ``fuzzy_score`` of 100 - including flagship chain hotels (Marriott,
Fairmont, Hilton, Barceló) that are certainly bookable months out. That
combination (exact name match, but no price) only comes from one branch:
``_card_is_unavailable()`` reading "unavailable on our site" off the matched
card itself. Live reproduction confirmed why: cold-loaded, Booking renders a
fake "unavailable" placeholder under the searched property's exact name (see
above); clicking Search resolves a real ``dest_id`` and the SAME hotel
reappears fully bookable with a real price. ``_wait_for_dest_id()`` is
supposed to catch exactly this by confirming the URL actually gained a
``dest_id`` before trusting the page - but it was waiting a hardcoded 6
seconds, less than this module's own ``post_search`` budget (8s) for the
page to settle *after* that. Under real ``BOOKING_CONCURRENCY`` (several
headless Chromium contexts sharing one host's CPU/network), the resolving
navigation routinely took longer than 6s without actually failing - so the
verification timed out on a resolution that was simply still in flight,
and the code fell back to reading the cold, unresolved, always-"unavailable"
page as if it were live. Fixed two ways: the wait is now a configurable
``settings.timeouts.dest_id_resolve`` (12s, comfortably past ``post_search``)
instead of a magic number; and as a second layer, if the destination never
did verifiably resolve, an "unavailable" reading off that page is no longer
trusted as a terminal result - it comes back ``ERROR`` (retried) rather than
``NO_RESULTS`` (accepted as final).
"""

from __future__ import annotations

import asyncio
import dataclasses
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import Page

from src.config import Settings
from src.models import (
    BookingResult,
    HotelIdentity,
    MatchMethod,
    MatchStatus,
    SearchWindow,
)
from src.utils.browser import (
    BrowserManager,
    dismiss_overlays,
    install_overlay_autodismiss,
    safe_text,
)
from src.utils.hotel_ids import (
    PROPERTY_INDEX_JS,
    BookingProperty,
    HotelIdMap,
    booking_slug_from_url,
    haversine_m,
    parse_property_index,
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


def _unavailable_card_outcome(
    *, resolved: bool, matched_name: Optional[str], score: float, final_url: str
) -> BookingResult:
    """
    Decide the terminal status for a matched card that reads "unavailable".

    Only trusted as a real, terminal ``NO_RESULTS`` when the destination was
    actually verified as resolved (a ``dest_id`` was confirmed in the URL -
    see ``_resolve_destination``). When it was NOT resolved, this reading is
    very likely the cold, stale placeholder card described in the module
    docstring's "stale-deep-link bug" - it always shows a perfect name match
    and always claims unavailability, regardless of real inventory. Rather
    than accept that as final, this returns ``ERROR`` so the outer retry
    loop in ``lookup()`` gives it another attempt.

    Live-diagnosed 2026-07: this exact combination (fuzzy_score=100,
    NO_RESULTS, destination never resolved) accounted for ~60% of lookups in
    a production run, including flagship chain hotels (Marriott, Fairmont,
    Hilton, Barceló) confirmed live to be genuinely bookable.
    """
    if not resolved:
        return BookingResult(
            status=MatchStatus.ERROR,
            matched_name=matched_name,
            fuzzy_score=score,
            error=(
                "matched card read unavailable but destination was never "
                "confirmed resolved - likely a stale placeholder, not a "
                "real result"
            ),
        )
    return BookingResult(
        status=MatchStatus.NO_RESULTS,
        matched_name=matched_name,
        fuzzy_score=score,
        url=final_url,
        error="matched card marked unavailable for these dates",
    )


@dataclass(slots=True)
class _Candidate:
    """One scored result card."""

    index: int
    name: str
    score: float
    method: MatchMethod = MatchMethod.NAME
    booking_id: Optional[str] = None
    distance_m: Optional[float] = None

    @property
    def confirmed(self) -> bool:
        """Matched by id or location, not just by name."""
        return self.method in (MatchMethod.BOOKING_ID, MatchMethod.GEO)

    def accepted(self, name_threshold: float) -> bool:
        return self.confirmed or self.score >= name_threshold

    def info(self) -> dict:
        return {
            "match_method": self.method.value,
            "booking_hotel_id": self.booking_id,
            "distance_m": round(self.distance_m, 1) if self.distance_m is not None else None,
        }


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
        self._ids = HotelIdMap(
            settings.storage.id_map_path if self._cfg.use_hotel_ids else None
        )

    # ======================================================================
    # Public API
    # ======================================================================

    async def lookup_many(
        self,
        window: SearchWindow,
        hotel_names: list[str],
        identities: Optional[dict[str, HotelIdentity]] = None,
    ) -> dict[str, BookingResult]:
        """
        Look up every hotel with bounded concurrency.

        ``identities`` maps normalised name -> HotelIdentity (from the
        tunnel) so lookups can use Booking ids and coordinates.
        Returns ``{normalised_name: BookingResult}``. Never raises.
        """
        semaphore = asyncio.Semaphore(self._cfg.concurrency)
        results: dict[str, BookingResult] = {}
        identities = identities or {}

        async def _one(name: str) -> None:
            key = normalise_key(name)
            async with semaphore:
                results[key] = await self.lookup(window, name, identities.get(key))
                await polite_delay(0.3, 1.1)

        await asyncio.gather(*(_one(n) for n in hotel_names), return_exceptions=True)

        ok = [r for r in results.values() if r.status is MatchStatus.OK]
        methods = {m.value: 0 for m in MatchMethod}
        for r in ok:
            if r.match_method in methods:
                methods[r.match_method] += 1
        log.info(
            "[%s %s] booking: %d/%d matched (by id %d, by location %d, by name %d)",
            window.city, window.horizon_label, len(ok), len(hotel_names),
            methods[MatchMethod.BOOKING_ID.value], methods[MatchMethod.GEO.value],
            methods[MatchMethod.NAME.value],
        )
        return results

    async def lookup(
        self,
        window: SearchWindow,
        hotel_name: str,
        identity: Optional[HotelIdentity] = None,
    ) -> BookingResult:
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
                # Hard deadline: a frozen renderer ignores per-call timeouts
                # (page.evaluate / locator.count take none) and once froze
                # a whole run for days - see TimeoutSettings.booking_lookup_hard.
                result = await asyncio.wait_for(
                    self._lookup_once(window, hotel_name, identity),
                    timeout=self._t.booking_lookup_hard / 1000,
                )
                result.attempts = attempt
            except asyncio.TimeoutError:
                log.warning(
                    "[%s] Booking lookup exceeded %.0fs - abandoning attempt %d",
                    hotel_name[:40], self._t.booking_lookup_hard / 1000, attempt,
                )
                result = BookingResult(
                    status=MatchStatus.ERROR,
                    error="hard timeout: lookup never finished",
                    attempts=attempt,
                )
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
        self,
        window: SearchWindow,
        hotel_name: str,
        identity: Optional[HotelIdentity] = None,
    ) -> BookingResult:
        """
        One lookup attempt: by Booking id first when the hotel's id is known
        (from Nuitee's ``thirdPartyId`` or a previously confirmed match),
        then the name search - which still uses the id and coordinates to
        pick and verify the card.
        """
        identity = self._effective_identity(identity)
        if identity is not None and identity.booking_hotel_id:
            by_id = await self._search(window, hotel_name, identity, by_id=True)
            if by_id is not None:
                return by_id
            log.debug(
                "[%s] Booking id %s not among the results - falling back to "
                "the name search", hotel_name[:40], identity.booking_hotel_id,
            )
        result = await self._search(window, hotel_name, identity, by_id=False)
        assert result is not None
        return result

    def _effective_identity(
        self, identity: Optional[HotelIdentity]
    ) -> Optional[HotelIdentity]:
        """``identity`` plus any learned Booking id; None when ids are off."""
        if not self._cfg.use_hotel_ids or identity is None:
            return None
        if not identity.booking_hotel_id:
            learned = self._ids.get(identity.nuitee_hotel_id)
            if learned:
                return dataclasses.replace(identity, booking_hotel_id=learned)
        return identity

    async def _search(
        self,
        window: SearchWindow,
        hotel_name: str,
        identity: Optional[HotelIdentity],
        *,
        by_id: bool,
    ) -> Optional[BookingResult]:
        """
        Load one results page and match/price the hotel on it.

        ``by_id`` searches ``dest_id={booking id}&dest_type=hotel`` and
        returns None when that exact property isn't among the cards, so the
        caller can fall back to the name search.
        """
        expected = normalise_display(hotel_name)
        search_url = self._build_search_url(
            window, expected,
            booking_hotel_id=identity.booking_hotel_id if by_id and identity else None,
        )

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
            # live-verified against three unrelated hotels). A by-id URL
            # already carries its dest_id, so there is nothing to resolve.
            resolved = True if by_id else await self._resolve_destination(page, expected)
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

            properties = await self._property_index(page) if identity else {}
            candidate = await self._best_match(
                page, expected, window,
                identity=identity, properties=properties, scan_all=by_id,
            )
            if by_id and (
                candidate is None or candidate.method is not MatchMethod.BOOKING_ID
            ):
                return None

            index = candidate.index if candidate else None
            matched_name = candidate.name if candidate else None
            score = candidate.score if candidate else 0.0
            threshold = self._threshold_for(expected, window.city)
            match_info = candidate.info() if candidate else {}

            if candidate is not None and candidate.accepted(threshold):
                cards = page.locator(
                    self.registry.get("booking.property_card").combined
                )

                # _best_match scored card `index` in its OWN locator
                # resolution; `cards` above is a second, independent
                # resolution of the same list. Playwright locators are
                # lazy, so if Booking's results re-sorted or re-rendered
                # (lazy-loaded cards, injected sponsored/ad cards, an A/B
                # re-rank) between those two resolutions, `cards.nth(index)`
                # here can silently be a DIFFERENT property than the one
                # `matched_name` actually names - live-observed: matched_name
                # read "Grand Plaza Marrakech" while the price/URL read from
                # the same index came back as an unrelated Ibis Moussafir
                # property. Re-read the name at this index and refuse to
                # trust the price/URL unless it still agrees, rather than
                # silently attributing one hotel's price to another.
                #
                # NOTE: this must be a FUZZY comparison, not exact string
                # equality. Live logs showed constant false positives like
                # matched_name="Hotel Majestic Opens in new window" vs
                # reread_name="Hotel Majestic" - the exact same property,
                # just with an accessibility-only "Opens in new window"
                # label included in one read and not the other (the
                # card_name selector cascade has several candidates, e.g.
                # a wrapping <a data-testid="title-link"> vs the narrower
                # [data-testid="property-card-name"], and which one wins
                # can differ between two independent reads of the same
                # index). Exact equality treated every one of these as a
                # "wrong hotel" and discarded a good match - which is what
                # actually was causing very slow, retry-heavy runs, not a
                # deeper bug. fuzzy_score already exists precisely to
                # tolerate this kind of incidental padding (it's what the
                # original name match uses), so reuse it here instead.
                name_cascade = self.registry.get("booking.card_name")
                reread_raw = await name_cascade.text(cards.nth(index), timeout=700)
                reread_name = normalise_display(reread_raw) if reread_raw else None
                agreement = fuzzy_score(matched_name or "", reread_name or "")

                if agreement < self._cfg.fuzzy_threshold_strict:
                    log.warning(
                        "[%s] result order shifted between name-match and "
                        "price read (matched %r at index %d, that index now "
                        "shows %r, agreement=%.0f) - discarding this attempt "
                        "rather than risk pricing the wrong hotel",
                        expected[:40], matched_name, index, reread_name, agreement,
                    )
                    return BookingResult(
                        status=MatchStatus.ERROR,
                        matched_name=matched_name,
                        fuzzy_score=score,
                        error=(
                            f"card order shifted: matched {matched_name!r} at "
                            f"index {index}, but that index now shows "
                            f"{reread_name!r} (agreement {agreement:.0f})"
                        ),
                    )

                if candidate.confirmed and identity is not None:
                    self._ids.learn(identity.nuitee_hotel_id, candidate.booking_id)

                if await self._card_is_unavailable(cards.nth(index)):
                    log.debug(
                        "[%s] matched card %r reads unavailable "
                        "(destination resolved=%s)",
                        expected[:40], matched_name, resolved,
                    )
                    outcome = _unavailable_card_outcome(
                        resolved=resolved,
                        matched_name=matched_name,
                        score=score,
                        final_url=final_url,
                    )
                    return dataclasses.replace(outcome, **match_info)

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
                        **match_info,
                    )

                return BookingResult(
                    status=MatchStatus.NO_RESULTS,
                    matched_name=matched_name,
                    fuzzy_score=score,
                    url=final_url,
                    error="card matched but no price element resolved",
                    **match_info,
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
                    + (
                        f", {candidate.distance_m:.0f} m away"
                        if candidate and candidate.distance_m is not None else ""
                    )
                ),
            )

    # ======================================================================
    # URL construction
    # ======================================================================

    def _build_search_url(
        self,
        window: SearchWindow,
        hotel_name: str,
        booking_hotel_id: Optional[str] = None,
    ) -> str:
        """
        Deep link into Booking's results with the exact stay parameters.

        ``selected_currency=MAD`` is what keeps the whole dataset in one
        currency; without it Booking picks by IP geolocation and the output
        silently mixes EUR and MAD rows. With ``booking_hotel_id`` the search
        targets that exact property (``dest_type=hotel``), which Booking
        lists first - live-verified 3/3.
        """
        if booking_hotel_id:
            target = {"dest_id": booking_hotel_id, "dest_type": "hotel"}
        else:
            query = hotel_name
            if not contains_city(hotel_name, window.city):
                query = f"{hotel_name} {window.city}"
            target = {"ss": query}

        params = {
            **target,
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

    async def _wait_for_dest_id(self, page: Page, timeout: Optional[int] = None) -> bool:
        """True once the URL carries a real, server-resolved destination.

        Default timeout is ``settings.timeouts.dest_id_resolve`` (12s), not
        a hardcoded value - this wait races against real Booking.com
        navigation latency, which gets materially worse under concurrent
        load (``BOOKING_CONCURRENCY`` > 1). Too tight here silently falls
        back to reading the stale, unresolved page - see the module
        docstring and ``_resolve_destination``.
        """
        if timeout is None:
            timeout = self._t.dest_id_resolve
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

    async def _card_is_unavailable(self, card) -> bool:
        """
        Same marker scan as ``_is_unavailable``, but scoped to ONE matched
        card rather than the whole page.

        This matters because Booking shows "similar properties" / "you
        might also like" carousels right next to an unavailable primary
        result, all on the same page, each with real prices for OTHER
        hotels. ``_is_unavailable`` alone only guards the fallback path
        taken when no card clears the fuzzy threshold - but the primary
        result's own card (or the property's own name matching a
        carousel card written for a different offer) can still score
        above threshold and reach price extraction, at which point a
        whole-page check never runs at all. Live-diagnosed via FIVE
        Zurich - Luxury City Resort: its card read "This property is
        unavailable on our site for your dates", scored 100 against
        itself as expected, and a price was still read from within/near
        that same unavailable card - producing a Booking "price" nearly
        5x the real total for that stay. Checking THIS card's own text
        before trusting its price closes that gap.
        """
        if not self._no_avail_markers:
            return False
        try:
            text = (await card.inner_text(timeout=2_000)).lower()
        except Exception:
            return False
        return any(marker.lower() in text for marker in self._no_avail_markers)

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
        self,
        page: Page,
        expected: str,
        window: SearchWindow,
        *,
        identity: Optional[HotelIdentity] = None,
        properties: Optional[dict[str, BookingProperty]] = None,
        scan_all: bool = False,
    ) -> Optional["_Candidate"]:
        """
        Score the visible cards and return the best candidate, or None.

        Priority: the card whose Booking id equals the hotel's known id;
        then a card within ``geo_match_m`` with a plausible name; then the
        best fuzzy name. A card farther than ``geo_reject_m`` is never a
        candidate, however well its name scores. Ids/coordinates come from
        ``properties`` (the page's embedded data, keyed by card link slug);
        without them this is the original name-only match. ``scan_all``
        (the by-id search) looks at every card and only returns the id card.
        """
        card_cascade = self.registry.get("booking.property_card")
        name_cascade = self.registry.get("booking.card_name")
        link_cascade = self.registry.get_optional("booking.card_link")

        cards, count = await card_cascade.all_matching(page, timeout=self._t.first_card)
        if cards is None or count == 0:
            return None

        use_props = bool(identity is not None and properties)
        target_id = identity.booking_hotel_id if identity is not None else None
        limit = count if scan_all else min(count, self._cfg.max_cards_to_scan)
        best_geo: Optional[_Candidate] = None
        best_name: Optional[_Candidate] = None

        for index in range(limit):
            card = cards.nth(index)
            raw = await name_cascade.text(card, timeout=700)
            if not raw:
                continue
            name = normalise_display(raw)
            candidate = _Candidate(index, name, fuzzy_score(expected, name))

            if use_props:
                prop = await self._card_property(card, link_cascade, properties)
                if prop is not None:
                    candidate.booking_id = prop.booking_id
                    if (
                        identity.has_location
                        and prop.latitude is not None
                        and prop.longitude is not None
                    ):
                        candidate.distance_m = haversine_m(
                            identity.latitude, identity.longitude,
                            prop.latitude, prop.longitude,
                        )

            if target_id and candidate.booking_id == target_id:
                if self._id_plausible(candidate):
                    candidate.method = MatchMethod.BOOKING_ID
                    return candidate
                log.warning(
                    "[%s] card %r has the expected Booking id %s but fails the "
                    "sanity check (name %.0f, %s m) - not trusting the id",
                    expected[:40], name, target_id, candidate.score,
                    f"{candidate.distance_m:.0f}" if candidate.distance_m is not None else "?",
                )
            if scan_all:
                continue

            if candidate.distance_m is not None:
                if candidate.distance_m > self._cfg.geo_reject_m:
                    continue
                if (
                    candidate.distance_m <= self._cfg.geo_match_m
                    and candidate.score >= self._cfg.geo_name_threshold
                ):
                    candidate.method = MatchMethod.GEO
                    if best_geo is None or candidate.score > best_geo.score:
                        best_geo = candidate

            if best_name is None or candidate.score > best_name.score:
                best_name = candidate

            # An exact normalised hit cannot be beaten - stop scanning,
            # unless an id card is being looked for, or coordinates put
            # this card too far out to be location-confirmed.
            if (
                candidate.score >= 99.5
                and not target_id
                and (candidate.distance_m is None or candidate.method is MatchMethod.GEO)
            ):
                break

        return best_geo or best_name

    def _id_plausible(self, candidate: "_Candidate") -> bool:
        """An id match must not contradict location (or, failing that, name)."""
        if candidate.distance_m is not None:
            return candidate.distance_m <= self._cfg.geo_reject_m
        return candidate.score >= self._cfg.id_name_floor

    async def _card_property(
        self, card, link_cascade, properties: dict[str, BookingProperty]
    ) -> Optional[BookingProperty]:
        """The embedded id/coordinates for ``card``, via its link's slug."""
        try:
            link = await link_cascade.first_attached(card, timeout=500)
            href = await link.get_attribute("href") if link is not None else None
        except Exception:
            return None
        slug = booking_slug_from_url(href)
        return properties.get(slug) if slug else None

    async def _property_index(self, page: Page) -> dict[str, BookingProperty]:
        """Every listed property's id + coordinates, from the page's JSON."""
        try:
            raw = await asyncio.wait_for(page.evaluate(PROPERTY_INDEX_JS), timeout=5)
        except Exception as exc:
            log.debug("Booking property data unavailable: %s", exc)
            return {}
        return parse_property_index(raw)

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
