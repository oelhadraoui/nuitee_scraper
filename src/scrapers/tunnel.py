"""
src/scrapers/tunnel.py
=============================================================================
The Nuitee checkout-tunnel navigator.

This is the module that satisfies Requirement A: walk from a hotel detail
page to the booking page and read the TRUE PAYABLE PRICE - the figure that
includes taxes, city tax, service fees and resort fees, none of which appear
on the results-list card.

Real site architecture (verified live via the Claude-in-Chrome extension,
2026-07 - see the investigation that replaced the original 5-panel design)
------------------------------------------------------------------------
Nuitee's site is a Vue 3 / PrimeVue SPA backed by LiteAPI. It is NOT the
generic multi-panel reservation flow (ROOM_SELECT -> OPTIONS -> GUEST_DETAILS
-> SUMMARY -> PAYMENT) this module originally modelled. It is exactly two
pages:

  1. ``/hotels/{id}`` - the detail page. Rate cards (``.rate-card``) list
     bookable offers; each has a "Choose room" CTA
     (``[data-testid="hotel-choose-room-button"]``).
  2. ``/booking?...`` - reached via a client-side route push after clicking a
     rate card's CTA. This single page combines the guest form, the price
     summary, AND the payment form. Critically, the payable total
     (``[data-testid="checkout-total-price"]``) renders immediately, with NO
     guest-form field filled - confirmed on a live, untouched tab. Filling
     the form is therefore an optional fallback, not a precondition.

So the navigator is a fixed two-step walk, not a classify-and-loop state
machine: there is nothing to classify. The only genuine uncertainty is
whether a given rate card's CTA is actually clickable (inventory can change
mid-page), which is why the cheapest N offers are tried in order rather than
just the first one.

Safety
------
The submit control on the booking page (``[data-testid="checkout-submit-button"]``,
"Complete Booking") is never clicked - the navigator's booking-page code path
only ever *reads* the total and *fills* text inputs, it performs zero clicks
on that page at all, which structurally rules out an accidental submission.
As defense in depth, ``_is_payment_trigger`` still hard-blocks any click
(anywhere in the tunnel) against an exact ``data-testid`` in
``tunnel.payment.hard_block_testids``, plus the original accent-folded,
whitespace-normalised text-token guard (``tunnel.payment_guard_tokens``).
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

from playwright.async_api import Page

from src.config import Settings
from src.models import PanelKind, RoomOffer, TunnelResult, TunnelStatus
from src.utils.browser import (
    dismiss_overlays,
    human_scroll,
    is_visible,
    safe_text,
    safe_text_no_strike,
)
from src.utils.logging_setup import get_logger
from src.utils.pricing import parse_price
from src.utils.selectors import SelectorRegistry
from src.utils.text import normalise_display, slugify, strip_accents

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Room-card "taxes and fees" suffix parsing
# ---------------------------------------------------------------------------
#
# Verified live on the detail page ("Choose your room" section): every rate
# card prints its own fee figure as a parenthetical suffix right next to the
# per-night rate, e.g.:
#
#   "MAD 452 / night   10 nights, 1 room (+MAD 149 taxes and fees)"
#
# This is a much safer source for the fees figure than the checkout page's
# label-row scan (_extract_by_label / _extract_local_fees below): the amount
# is directly adjacent to its own label with nothing else nearby that could
# be mistaken for it, whereas on the checkout page "taxes and fees" can
# appear as a bare caption (e.g. "Includes taxes and fees") with no number of
# its own, which previously caused the row-scan to walk up to the nearest
# ancestor that DID have a number - the grand total - producing local_fees
# values that were silently identical to final_price on every single row.
_FEES_SUFFIX_RE_CACHE: dict[tuple[str, ...], "re.Pattern[str]"] = {}


def _fees_suffix_pattern(labels: list[str]) -> Optional["re.Pattern[str]"]:
    if not labels:
        return None
    key = tuple(labels)
    cached = _FEES_SUFFIX_RE_CACHE.get(key)
    if cached is not None:
        return cached
    alternation = "|".join(re.escape(label) for label in labels)
    # "+" then an optional currency token then the amount, immediately
    # followed (allowing punctuation/whitespace) by one of the fee labels.
    pattern = re.compile(
        r"\+\s*([A-Za-z]{2,4}\.?\s*[\d][\d,. ]*\d|\d[\d,. ]*\d|\d+)"
        r"\s*(?:" + alternation + r")",
        re.IGNORECASE,
    )
    _FEES_SUFFIX_RE_CACHE[key] = pattern
    return pattern


def _parse_card_fees_hint(
    card_text: str,
    labels: list[str],
    *,
    default_currency: str,
) -> Optional[float]:
    """Pull the "(+MAD 149 taxes and fees)" style suffix off a rate card."""
    if not card_text or not labels:
        return None
    pattern = _fees_suffix_pattern(labels)
    if pattern is None:
        return None
    match = pattern.search(card_text)
    if not match:
        return None
    parsed = parse_price(
        match.group(1),
        min_value=1.0,
        max_value=200_000.0,
        default_currency=default_currency,
        strategy="first",
    )
    return parsed.amount_mad if parsed.ok else None


def _resolve_local_fees(
    *,
    card_hint: Optional[float],
    checkout_scan_value: Optional[float],
    total_price: Optional[float],
    log=None,
) -> Optional[float]:
    """
    Decide the final ``local_fees`` value for a completed walk.

    Priority: the room-card hint (unambiguous - see _parse_card_fees_hint)
    always wins when present. Otherwise fall back to the checkout-page label
    scan (_extract_local_fees), but reject it outright if it comes back
    suspiciously close to the whole-stay total - a real "local fees" amount
    can never legitimately equal (or nearly equal) the total, so a match
    that close means the label scan latched onto the total's own container
    rather than an actual fees line (the exact bug that made local_fees ==
    final_price on every row before this fix).
    """
    if card_hint is not None:
        return card_hint
    if checkout_scan_value is None:
        return None
    if total_price and total_price > 0 and (checkout_scan_value / total_price) >= 0.9:
        if log is not None:
            log.debug(
                "discarding local_fees %.2f - too close to total %.2f "
                "(checkout-page label scan likely matched the total itself, "
                "not a fees line)",
                checkout_scan_value, total_price,
            )
        return None
    return checkout_scan_value


# ---------------------------------------------------------------------------
# Payment-guard text normalisation
# ---------------------------------------------------------------------------

def _normalise_guard_text(value: str) -> str:
    """
    Fold text to a canonical form for payment-token matching.

    Accents are stripped ("Réserver" -> "reserver"), every non-letter becomes a
    space, and runs of whitespace collapse to one. This makes the guard immune
    to markup indentation, non-breaking spaces, punctuation, and accents - all
    of which appear in real checkout buttons.
    """
    folded = strip_accents(value or "").lower()
    folded = re.sub(r"[^a-z]+", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()


# ---------------------------------------------------------------------------
# Navigator
# ---------------------------------------------------------------------------

class TunnelNavigator:
    """
    Walks one hotel through the 2-page reservation flow and extracts the
    final payable price.

    One navigator instance handles one hotel on one page. It is deliberately
    stateless between hotels so a poisoned SPA state can never leak across.
    """

    def __init__(
        self,
        page: Page,
        settings: Settings,
        registry: SelectorRegistry,
        *,
        hotel_name: str = "",
    ) -> None:
        self.page = page
        self.settings = settings
        self.registry = registry
        self.hotel_name = hotel_name
        self.log = get_logger(f"{__name__}.{slugify(hotel_name or 'hotel', 24)}")

        self._t = settings.timeouts
        self._cfg = settings.tunnel

        self._overlays = registry.get_optional("nuitee.overlays").candidates
        self._guard_tokens = [
            _normalise_guard_text(t) for t in self._cfg.payment_guard_tokens
        ]
        self._hard_block_testids = set(
            self.registry.strings("nuitee.tunnel.payment.hard_block_testids")
        )

    # ======================================================================
    # Public entry point
    # ======================================================================

    async def run(self, detail_url: str, display_price: Optional[float] = None) -> TunnelResult:
        """
        Walk the tunnel with retries.

        A fresh navigation is performed on every attempt: the SPA keeps
        reservation state in memory, so re-clicking from a half-broken state
        is far less reliable than restarting from the detail page.
        """
        result = TunnelResult(
            display_price=display_price,
            detail_url=detail_url,
            currency=self.settings.nuitee.currency,
        )

        attempts = self._cfg.max_retries + 1
        for attempt in range(1, attempts + 1):
            result.attempts = attempt
            try:
                await self._walk(detail_url, result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result.status = TunnelStatus.ERROR
                result.error = f"{type(exc).__name__}: {exc}"
                self.log.warning("tunnel attempt %d raised: %s", attempt, exc)

            if result.status in (
                TunnelStatus.OK,
                TunnelStatus.NO_ROOMS,
                TunnelStatus.PAYMENT_GUARD,
            ):
                break

            if attempt < attempts:
                backoff = self._cfg.retry_backoff_seconds * attempt
                self.log.info(
                    "tunnel attempt %d/%d -> %s, retrying in %.1fs",
                    attempt, attempts, result.status.value, backoff,
                )
                await asyncio.sleep(backoff)
                result.panels_visited = []

        # Partial credit: a total was read but the walk did not end in OK
        # (e.g. it was salvaged despite a stray error banner) and config
        # allows accepting it.
        if (
            result.status not in (TunnelStatus.OK,)
            and result.final_price is not None
            and self._cfg.allow_partial_tunnel
        ):
            result.status = TunnelStatus.PARTIAL

        if self._cfg.debug_artifacts and not result.succeeded:
            await self._dump_debug_artifacts(result)

        return result

    # ======================================================================
    # One full walk: detail page -> pick cheapest offer -> booking page
    # ======================================================================

    async def _walk(self, detail_url: str, result: TunnelResult) -> None:
        page = self.page

        await page.goto(
            detail_url, wait_until="domcontentloaded", timeout=self._t.page_load
        )
        await self._settle()

        result.panels_visited.append(PanelKind.ROOM_SELECT.value)
        result.deepest_panel = PanelKind.ROOM_SELECT.value

        fatal = await self._check_error_banner("nuitee.tunnel.fatal_errors")
        if fatal:
            result.status = TunnelStatus.NO_ROOMS
            result.error = f"fatal banner: {fatal}"
            return

        if await self.registry.get_optional(
            "nuitee.tunnel.room_select.sold_out"
        ).any_present(page, timeout=800):
            result.status = TunnelStatus.NO_ROOMS
            result.error = "sold-out marker on detail page"
            return

        offers = await self._collect_room_offers()
        if not offers:
            result.status = TunnelStatus.NO_ROOMS
            result.error = "no room offers parsed"
            return

        # Cheapest first; offers with no readable price go last (they are
        # usable but unrankable).
        offers.sort(
            key=lambda o: (o.display_price is None, o.display_price or float("inf"))
        )
        self.log.debug(
            "%d offers, cheapest=%s @ %s",
            len(offers), offers[0].label[:40], offers[0].display_price,
        )

        clicked_offer = await self._select_and_advance(offers)
        if clicked_offer is None:
            result.status = TunnelStatus.TUNNEL_BLOCKED
            result.error = "no room CTA advanced to the booking page"
            return

        result.room_label = clicked_offer.label
        if result.display_price is None:
            result.display_price = clicked_offer.display_price

        result.panels_visited.append(PanelKind.BOOKING_PAGE.value)
        result.deepest_panel = PanelKind.BOOKING_PAGE.value

        await self._settle()

        fatal = await self._check_error_banner("nuitee.tunnel.fatal_errors")
        if fatal:
            result.status = TunnelStatus.TUNNEL_BLOCKED
            result.error = f"fatal banner on booking page: {fatal}"
            return

        # The payable total renders on the booking page WITHOUT any
        # guest-form field being filled (verified live on an untouched tab).
        # Try that first - it is both the fastest path and the one least
        # likely to trip any anti-bot heuristic tied to form interaction.
        price = await self._extract_total_price()

        if price is None and self._cfg.fill_guest_details:
            filled = await self._fill_guest_form_if_present()
            if filled:
                price = await self._extract_total_price()

        if price is not None:
            result.final_price = price

            # Prefer the fee figure read straight off the room card on the
            # detail page (unambiguous - see _parse_card_fees_hint above).
            # Only fall back to the checkout-page label scan when the card
            # didn't expose one, and even then, reject it outright if it
            # comes back suspiciously close to the whole-stay total: a
            # "local fees" figure can never legitimately equal the total,
            # so a match that does is a mis-scrape (the row-scan latching
            # onto the total's own container), not a real answer.
            checkout_scan_value = (
                None
                if clicked_offer.local_fees_hint is not None
                else await self._extract_local_fees()
            )
            result.local_fees = _resolve_local_fees(
                card_hint=clicked_offer.local_fees_hint,
                checkout_scan_value=checkout_scan_value,
                total_price=price,
                log=self.log,
            )

            result.status = TunnelStatus.OK
            self.log.info(
                "final payable price %.2f %s (room=%s, local fees=%s)",
                price, result.currency, (result.room_label or "n/a")[:40],
                f"{result.local_fees:.2f}" if result.local_fees is not None else "n/a",
            )
            return

        result.status = TunnelStatus.TUNNEL_BLOCKED
        result.error = "booking page reached but no total price extracted"

    async def _select_and_advance(
        self, offers: list[RoomOffer]
    ) -> Optional[RoomOffer]:
        """
        Click the cheapest offer's CTA and wait for the client-side route
        push to ``/booking``. Falls through to the next-cheapest offer if a
        click lands but nothing advances (a race when inventory changes
        mid-page), up to ``room_candidates`` attempts.
        """
        page = self.page
        card_cascade = self.registry.get("nuitee.tunnel.room_select.room_card")
        cta_cascade = self.registry.get("nuitee.tunnel.room_select.cta")
        cards, _ = await card_cascade.all_matching(page, timeout=self._t.room_list)

        for offer in offers[: self._cfg.room_candidates]:
            card = cards.nth(offer.index) if cards is not None else None
            clicked: Optional[str] = None

            if card is not None:
                try:
                    await card.scroll_into_view_if_needed(timeout=2_500)
                except Exception:
                    pass
                clicked = await self._guarded_click_in(card, cta_cascade)

            if clicked is None:
                # CTA might live outside the card (a sticky footer bar).
                clicked = await self._guarded_click(cta_cascade)

            if clicked is None:
                continue

            try:
                await page.wait_for_url(
                    lambda url: "/booking" in url, timeout=self._t.tunnel_cta
                )
            except Exception:
                self.log.debug(
                    "clicked %r for offer %r but URL never reached /booking",
                    clicked, offer.label[:40],
                )
                continue

            self.log.info(
                "selected room %r @ %s -> booking page",
                offer.label[:48], offer.display_price,
            )
            return offer

        return None

    async def _settle(self) -> None:
        """Dismiss overlays and wait for spinners to clear."""
        if self._overlays:
            await dismiss_overlays(self.page, self._overlays, timeout=600)

        loading = self.registry.get_optional("nuitee.tunnel.loading")
        if loading.candidates:
            try:
                await self.page.wait_for_selector(
                    loading.combined, state="hidden", timeout=self._t.tunnel_transition
                )
            except Exception:
                pass

        try:
            await self.page.wait_for_load_state(
                "networkidle", timeout=self._t.tunnel_transition
            )
        except Exception:
            pass

    async def _check_error_banner(self, path: str) -> Optional[str]:
        """Return the matched error text, if such a banner is displayed."""
        cascade = self.registry.get_optional(path)
        for candidate in cascade.candidates:
            try:
                locator = self.page.locator(candidate).first
                if await locator.is_visible(timeout=350):
                    text = await safe_text(locator, timeout=800)
                    return (text or candidate)[:160]
            except Exception:
                continue
        return None

    # ======================================================================
    # Payment guard
    # ======================================================================

    async def _is_payment_trigger(self, locator) -> bool:
        """
        SAFETY RAIL.

        True when the element about to be clicked looks like it submits a
        payment. Two independent checks, either of which is sufficient:

        1. **Exact testid hard block** - the element's ``data-testid`` is in
           ``tunnel.payment.hard_block_testids`` (e.g. "checkout-submit-button").
           No fuzziness: this is a literal set-membership check.
        2. **Text-token guard** - visible text / aria-label / name / id /
           value, folded through ``_normalise_guard_text``, contains one of
           ``tunnel.payment_guard_tokens`` ("pay now", "confirm and pay", ...).

        Checks visible text and several attributes because the actual submit
        control is sometimes an icon button whose only identifying text is
        its aria-label.
        """
        try:
            testid = await locator.get_attribute("data-testid")
        except Exception:
            testid = None
        if testid and testid in self._hard_block_testids:
            return True

        fragments: list[str] = []
        try:
            fragments.append((await locator.inner_text()) or "")
        except Exception:
            pass
        for attribute in ("aria-label", "name", "id", "value", "data-testid"):
            try:
                value = await locator.get_attribute(attribute)
                if value:
                    fragments.append(value)
            except Exception:
                continue

        # Whitespace MUST be collapsed. Rendered HTML routinely puts newlines
        # and indentation inside a <button>, so the inner text of a real
        # "Pay now" control is often "\n      Pay\n      now\n    ".
        # Without normalisation the guard silently fails open - the single
        # most dangerous bug this codebase could have.
        haystack = _normalise_guard_text(" ".join(fragments))
        return any(token in haystack for token in self._guard_tokens)

    async def _guarded_click(self, cascade, timeout: Optional[int] = None) -> Optional[str]:
        """
        Click the first safe, visible candidate.

        Any candidate that trips the payment guard is skipped, not clicked.
        """
        timeout = timeout or self._t.tunnel_cta
        for candidate in cascade.candidates:
            try:
                locator = self.page.locator(candidate).first
                if not await locator.is_visible(timeout=600):
                    continue
                if await self._is_payment_trigger(locator):
                    self.log.warning(
                        "PAYMENT GUARD: refusing to click %r on %s",
                        candidate, self.hotel_name[:40],
                    )
                    continue
                if await locator.is_disabled():
                    continue
                try:
                    await locator.scroll_into_view_if_needed(timeout=2_000)
                except Exception:
                    pass
                await locator.click(timeout=timeout)
                return candidate
            except Exception:
                continue
        return None

    async def _guarded_click_in(self, scope, cascade) -> Optional[str]:
        """Payment-guarded click restricted to a sub-tree (one room card)."""
        for candidate in cascade.candidates:
            try:
                locator = scope.locator(candidate).first
                if not await locator.is_visible(timeout=500):
                    continue
                if await self._is_payment_trigger(locator):
                    continue
                await locator.click(timeout=self._t.tunnel_cta)
                return candidate
            except Exception:
                continue
        return None

    # ======================================================================
    # Room offer collection (detail page)
    # ======================================================================

    async def _collect_room_offers(self) -> list[RoomOffer]:
        """Parse every offer card on the detail page into a RoomOffer."""
        card_cascade = self.registry.get("nuitee.tunnel.room_select.room_card")
        name_cascade = self.registry.get("nuitee.tunnel.room_select.room_name")
        price_cascade = self.registry.get("nuitee.tunnel.room_select.room_price")

        cards, count = await card_cascade.all_matching(
            self.page, timeout=self._t.room_list
        )
        if cards is None or count == 0:
            return []

        # Lazy-loaded room lists need a nudge.
        if count < 3:
            await human_scroll(self.page, 900, steps=3)
            await self.page.wait_for_timeout(500)
            count = await cards.count()

        fee_labels = self.registry.strings("nuitee.tunnel.summary.local_fees_row_labels")

        offers: list[RoomOffer] = []
        for index in range(min(count, 40)):
            card = cards.nth(index)
            try:
                label = await name_cascade.text(card, timeout=700)
                raw_price = await price_cascade.text(card, timeout=700)

                # Always read the full card text too: this is what carries
                # the "(+MAD 149 taxes and fees)" suffix (verified live -
                # every rate card on the detail page prints its own taxes
                # figure right next to the per-night rate, well before the
                # checkout tunnel is entered). The dedicated price element
                # matched above is usually scoped to just the per-night
                # number and won't include this suffix.
                full_card_text = await safe_text(card, timeout=700)

                if raw_price is None:
                    # Fall back to the whole card's text.
                    raw_price = full_card_text

                parsed = parse_price(
                    raw_price or "",
                    min_value=self.settings.quality.min_price_mad,
                    max_value=self.settings.quality.max_price_mad,
                    default_currency=self.settings.nuitee.currency,
                    strategy="min",   # cheapest figure inside the offer card
                )

                fees_hint = _parse_card_fees_hint(
                    full_card_text or "",
                    fee_labels,
                    default_currency=self.settings.nuitee.currency,
                )

                offers.append(
                    RoomOffer(
                        label=normalise_display(label or f"Offer #{index + 1}"),
                        display_price=parsed.amount_mad,
                        currency=parsed.currency,
                        index=index,
                        local_fees_hint=fees_hint,
                    )
                )
            except Exception:
                continue

        return offers

    # ======================================================================
    # Guest form (booking page) - fill only, never a click
    # ======================================================================

    async def _fill_guest_form_if_present(self) -> int:
        """
        Fill the guest form with synthetic data, if the fields are present.

        No real personal data is submitted. Critically, this method never
        clicks anything - the booking page's total is a read, and its form
        is a fill; there is no CTA on this page the navigator is allowed to
        press. That is what makes "never submit a booking" a structural
        property of this code path rather than something the guard has to
        catch at the last second.
        """
        base = "nuitee.tunnel.booking_page"
        fields = [
            (f"{base}.first_name", self._cfg.guest_first_name),
            (f"{base}.last_name", self._cfg.guest_last_name),
            (f"{base}.email", self._cfg.guest_email),
            (f"{base}.phone", self._cfg.guest_phone),
        ]

        filled = 0
        for path, value in fields:
            cascade = self.registry.get_optional(path)
            if not cascade.candidates:
                continue
            if await cascade.fill(self.page, value, timeout=self._t.element_visible):
                filled += 1

        if filled:
            self.log.debug("booking page: filled %d field(s)", filled)
            await self.page.wait_for_timeout(self._t.tunnel_transition)
        return filled

    # ======================================================================
    # Final price extraction
    # ======================================================================

    async def _extract_total_price(self, quiet: bool = False) -> Optional[float]:
        """
        Read the true payable total, using three strategies in order.

        1. **Dedicated total element.** ``[data-testid="checkout-total-price"]
           .price.total`` - the verified live selector. Highest confidence.
        2. **Label/value row scan.** Find a row whose label matches one of
           ``total_row_labels`` ("Total price", "Prix total", "Amount due")
           and read the number from the same row. Handles the very common
           table layout where the total has no distinguishing test id.
        3. **Panel-wide maximum.** Sweep the summary container for numbers and
           take the largest. The payable total is, by construction, the
           largest figure in a summary that itemises base rate + taxes + fees.
           Lowest confidence, so it runs last.
        """
        min_price = self.settings.quality.min_price_mad
        max_price = self.settings.quality.max_price_mad
        currency = self.settings.nuitee.currency

        # The booking page's total can take a beat to hydrate after the
        # route push - wait for the dedicated element to attach before
        # trying to read it, rather than a single short probe.
        detect = self.registry.get_optional("nuitee.tunnel.summary.detect")
        if detect.candidates:
            try:
                await self.page.wait_for_selector(
                    detect.combined, timeout=self._t.tunnel_panel, state="attached"
                )
            except Exception:
                pass

        await self.page.wait_for_timeout(self._t.tunnel_price_settle)

        # -- strategy 1: dedicated element ----------------------------------
        total_cascade = self.registry.get_optional("nuitee.tunnel.summary.total_price")
        for candidate in total_cascade.candidates:
            try:
                locator = self.page.locator(candidate).first
                if not await is_visible(locator, timeout=500):
                    continue
                raw = await safe_text_no_strike(locator, timeout=900)
                if not raw:
                    continue
                parsed = parse_price(
                    raw,
                    min_value=min_price,
                    max_value=max_price,
                    default_currency=currency,
                    strategy="first",
                )
                if parsed.ok:
                    if not quiet:
                        self.log.debug(
                            "total via element %r -> %.2f", candidate, parsed.amount_mad
                        )
                    return parsed.amount_mad
            except Exception:
                continue

        # -- strategy 2: labelled row ---------------------------------------
        labels = self.registry.strings("nuitee.tunnel.summary.total_row_labels")
        if labels:
            price = await self._extract_by_label(labels, min_price, max_price, currency)
            if price is not None:
                if not quiet:
                    self.log.debug("total via label row -> %.2f", price)
                return price

        # -- strategy 3: container sweep ------------------------------------
        # "max" is deliberately kept here (unlike strategy 1's dedicated
        # element, which uses "first"): this sweep spans a much wider region
        # - the whole cancellation-policy panel plus room/fees breakdown, or
        # the full page body as a last resort - where the grand total is not
        # necessarily the first number in document order (a cancellation
        # refund figure often reads earlier). It IS reliably the largest
        # number, provided struck-through "was" prices and bare calendar
        # years are excluded first - which safe_text_no_strike() and the
        # date-aware noise patterns in pricing.py now both guarantee.
        container_cascade = self.registry.get_optional("nuitee.tunnel.summary.container")
        container = await container_cascade.first_attached(self.page, timeout=800)
        scope_text = None
        if container is not None:
            scope_text = await safe_text_no_strike(container, timeout=1_500)
        if not scope_text:
            scope_text = await safe_text_no_strike(self.page.locator("body").first, timeout=2_000)

        if scope_text:
            parsed = parse_price(
                scope_text,
                min_value=min_price,
                max_value=max_price,
                default_currency=currency,
                strategy="max",
            )
            if parsed.ok:
                if not quiet:
                    self.log.debug("total via container sweep -> %.2f", parsed.amount_mad)
                return parsed.amount_mad

        return None

    async def _extract_local_fees(self) -> Optional[float]:
        """
        Read the "Local fees" / "taxes and fees" line directly off the
        booking page's summary - the site's own figure, not an estimate.

        Earlier versions only had ``compute_hidden_fees()``, which infers
        this number from ``final_price - display_price * nights``. That
        estimate is reasonable when both inputs are correct, but it silently
        inherits any error in either one (exactly what happened when a
        struck-through "was" price briefly inflated ``final_price`` in
        production - see the ``safe_text_no_strike`` fix above). The site
        prints the real fee amount in plain text right next to the total
        ("Local fees MAD 145.25"), so it is read directly, the same way the
        total itself is - reusing the struck-through-safe label-row scan.

        Returns ``None`` (not 0) when the line can't be found, so callers can
        tell "no fees" apart from "we didn't find the fees line" and fall
        back to the estimate accordingly.
        """
        labels = self.registry.strings("nuitee.tunnel.summary.local_fees_row_labels")
        if not labels:
            return None
        max_price = self.settings.quality.max_price_mad
        currency = self.settings.nuitee.currency
        # Fees are usually small (tens to low hundreds of MAD) - use a low
        # floor rather than the whole-stay total's min_price, or a cheap
        # property's real fee could be filtered out as "too small to be a
        # price".
        return await self._extract_by_label(labels, 1.0, max_price, currency)

    async def _extract_by_label(
        self,
        labels: list[str],
        min_price: float,
        max_price: float,
        currency: str,
    ) -> Optional[float]:
        """
        Find the summary row whose label matches, and read its amount.

        Implemented in the page context because walking up from a text node to
        its row container is far cheaper in one JS round-trip than in dozens
        of Playwright locator calls.

        Struck-through ("was" / pre-discount) text is excluded from every
        candidate row before it is returned, mirroring the same guard used in
        ``_extract_total_price``'s dedicated-element strategy: a crossed-out
        anchor price must never become a numeric candidate.
        """
        try:
            raw_row = await self.page.evaluate(
                """
                (labels) => {
                  const norm = (s) => (s || '')
                    .toLowerCase()
                    .normalize('NFKD')
                    .replace(/[\\u0300-\\u036f]/g, '')
                    .replace(/\\s+/g, ' ')
                    .trim();

                  const isStruck = (node) => {
                    for (let n = node; n; n = n.parentElement) {
                      const cs = getComputedStyle(n);
                      if (cs && cs.textDecorationLine &&
                          cs.textDecorationLine.includes('line-through')) {
                        return true;
                      }
                    }
                    return false;
                  };
                  const textExcludingStruck = (root) => {
                    const walker = document.createTreeWalker(
                      root, NodeFilter.SHOW_TEXT, null
                    );
                    const parts = [];
                    let n;
                    while ((n = walker.nextNode())) {
                      if (n.parentElement && isStruck(n.parentElement)) continue;
                      if (n.textContent) parts.push(n.textContent);
                    }
                    return parts.join(' ');
                  };

                  const wanted = labels.map(norm);
                  const nodes = Array.from(
                    document.querySelectorAll('div,span,td,th,p,li,dt,dd,strong,b')
                  );

                  const matches = [];
                  for (const node of nodes) {
                    // Only consider leaf-ish nodes: a wrapper containing the
                    // whole page would match everything.
                    if (node.children.length > 2) continue;
                    const text = norm(node.textContent);
                    if (!text || text.length > 60) continue;
                    if (!wanted.some((w) => text.includes(w))) continue;

                    // Walk up to a container that also holds a number,
                    // reading its text with struck-through content removed.
                    let scope = node;
                    for (let depth = 0; depth < 4 && scope; depth += 1) {
                      const scopeText = textExcludingStruck(scope);
                      if (/\\d/.test(scopeText.replace(text, ''))) {
                        matches.push(scopeText.trim().slice(0, 200));
                        break;
                      }
                      scope = scope.parentElement;
                    }
                  }
                  return matches;
                }
                """,
                labels,
            )
        except Exception:
            return None

        if not raw_row:
            return None

        candidates: list[float] = []
        for row_text in raw_row:
            parsed = parse_price(
                row_text,
                min_value=min_price,
                max_value=max_price,
                default_currency=currency,
                strategy="first",
            )
            if parsed.ok and parsed.amount_mad is not None:
                candidates.append(parsed.amount_mad)

        # Several distinct rows can match ("Total price" and "Total to pay");
        # the payable figure among genuinely different rows is the largest.
        # (The anchor-vs-payable ambiguity *within* one row was already
        # resolved above by stripping struck-through text and using "first".)
        return max(candidates) if candidates else None


    # ======================================================================
    # Debugging
    # ======================================================================

    async def _dump_debug_artifacts(self, result: TunnelResult) -> None:
        """Screenshot + HTML dump, so a tunnel failure is diagnosable offline."""
        try:
            debug_dir = self.settings.storage.output_dir / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            stem = f"{slugify(self.hotel_name or 'hotel')}-{result.status.value}"
            await self.page.screenshot(
                path=str(debug_dir / f"{stem}.png"), full_page=True
            )
            html = await self.page.content()
            (debug_dir / f"{stem}.html").write_text(html, encoding="utf-8")
            self.log.info("debug artifacts written to %s", debug_dir / stem)
        except Exception:
            self.log.debug("failed to write debug artifacts", exc_info=True)
