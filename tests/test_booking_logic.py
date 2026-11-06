"""
Tests for BookingScraper's destination-resolution logic, using async fakes in
place of Playwright.

Why this module exists
-----------------------
A live investigation (Claude-in-Chrome against the real site, 2026-07) found
that a deep-linked ``searchresults.html?ss=...`` URL routinely renders a
STALE "Unavailable on our site for your selected dates" card for properties
that have real, bookable inventory - confirmed for three unrelated hotels,
all of which showed a normal price the instant the on-page Search button was
clicked. The previous implementation clicked that button with ``force=True``
and never checked whether the click actually resolved anything, so it
silently scored and "failed to extract a price from" a stale page - which is
why roughly half of an otherwise-healthy run's hotels were coming back
``NO_RESULTS``.

These tests cover the fix (``_resolve_destination`` /
``_wait_for_dest_id``) without a browser: that a resolved URL is trusted
immediately, that a click which doesn't produce a ``dest_id`` is retried,
and that the autocomplete fallback is used (and its own failure handled
cleanly) when the plain click never resolves anything - covering the second,
independently-diagnosed failure mode (a property name Booking's autocomplete
doesn't recognise at all, e.g. "Ennesma Hotel").
"""

from __future__ import annotations

from datetime import date

import pytest

from src.config import Settings
from src.models import BookingResult, MatchStatus, SearchWindow
from src.scrapers.booking import (
    BookingBlockedError,
    BookingScraper,
    _unavailable_card_outcome,
)
from src.utils.selectors import SelectorRegistry
from src.utils.text import fuzzy_score

# ---------------------------------------------------------------------------
# Async Playwright fakes
# ---------------------------------------------------------------------------

class FakeLocator:
    """Implements just enough of Playwright's Locator surface."""

    def __init__(
        self, visible: bool = True, on_click=None, text: str = ""
    ) -> None:
        self._visible = visible
        self._on_click = on_click
        self._text = text
        self.clicked = False
        self.filled: list[str] = []
        self.typed: list[str] = []

    @property
    def first(self) -> "FakeLocator":
        return self

    async def inner_text(self, timeout: int = 0) -> str:
        return self._text

    async def is_visible(self, timeout: int = 0) -> bool:
        return self._visible

    async def is_disabled(self) -> bool:
        return False

    async def scroll_into_view_if_needed(self, timeout: int = 0) -> None:
        return None

    async def click(self, timeout: int = 0, force: bool = False) -> None:
        if not self._visible:
            raise RuntimeError("not visible")
        self.clicked = True
        if self._on_click is not None:
            self._on_click()

    async def fill(self, value: str, timeout: int = 0) -> None:
        self.filled.append(value)

    async def type(self, value: str, delay: int = 0) -> None:
        self.typed.append(value)

    async def wait_for(self, state: str = "visible", timeout: int = 0) -> None:
        if not self._visible:
            raise RuntimeError("not visible")


class FakePage:
    """Maps selector strings to FakeLocators and tracks a mutable URL."""

    def __init__(self, mapping: dict[str, FakeLocator], url: str) -> None:
        self.mapping = mapping
        self.url = url

    def locator(self, selector: str) -> FakeLocator:
        return self.mapping.get(selector, FakeLocator(visible=False))

    async def wait_for_url(self, predicate, timeout: int = 0) -> None:
        if not predicate(self.url):
            raise TimeoutError("url predicate never became true")


@pytest.fixture(scope="module")
def registry() -> SelectorRegistry:
    return SelectorRegistry.load(Settings().selectors_path)


@pytest.fixture
def scraper(registry: SelectorRegistry) -> BookingScraper:
    return BookingScraper(manager=None, settings=Settings(), registry=registry)


STALE_URL = "https://www.booking.com/searchresults.html?ss=Kenzi+Basma"
RESOLVED_URL = STALE_URL + "&dest_id=98239&dest_type=hotel"


# ---------------------------------------------------------------------------
# _wait_for_dest_id
# ---------------------------------------------------------------------------

class TestWaitForDestId:
    async def test_true_when_already_resolved(self, scraper: BookingScraper) -> None:
        page = FakePage({}, url=RESOLVED_URL)
        assert await scraper._wait_for_dest_id(page) is True

    async def test_false_when_never_resolved(self, scraper: BookingScraper) -> None:
        page = FakePage({}, url=STALE_URL)
        assert await scraper._wait_for_dest_id(page) is False


# ---------------------------------------------------------------------------
# _resolve_destination - tier 1: the plain Search click
# ---------------------------------------------------------------------------

class TestResolveDestinationTierOneClick:
    async def test_click_that_resolves_the_url_succeeds_immediately(
        self, scraper: BookingScraper
    ) -> None:
        """
        The common, healthy case: clicking Search attaches dest_id/dest_type
        to the URL, exactly like the live-verified Marriott/Kenzi Basma/Art
        Palace reproductions.
        """
        page = FakePage({}, url=STALE_URL)

        def _resolve() -> None:
            page.url = RESOLVED_URL

        page.mapping = {"button[type=\"submit\"]": FakeLocator(on_click=_resolve)}

        resolved = await scraper._resolve_destination(page, "Kenzi Basma")

        assert resolved is True
        assert page.url == RESOLVED_URL

    async def test_click_that_does_nothing_is_not_trusted(
        self, scraper: BookingScraper
    ) -> None:
        """
        Regression for the actual production bug: a click that "succeeds"
        (doesn't raise) but never changes the URL must NOT be treated as a
        resolved destination - that was the whole failure mode. With no
        autocomplete fallback available either, the overall result is False.
        """
        page = FakePage({"button[type=\"submit\"]": FakeLocator()}, url=STALE_URL)

        resolved = await scraper._resolve_destination(page, "Casablanca Marriott Hotel")

        assert resolved is False
        assert page.url == STALE_URL


# ---------------------------------------------------------------------------
# _resolve_destination - tier 2: the autocomplete fallback
# ---------------------------------------------------------------------------

class TestResolveDestinationAutocompleteFallback:
    async def test_falls_back_to_autocomplete_when_click_never_resolves(
        self, scraper: BookingScraper
    ) -> None:
        destination_field = FakeLocator()

        suggestion = FakeLocator()

        def _pick_suggestion() -> None:
            page.url = RESOLVED_URL

        suggestion._on_click = _pick_suggestion

        page = FakePage(
            {
                # Search click never resolves anything (dead/overlaid button).
                'button[type="submit"]': FakeLocator(),
                'input[name="ss"]': destination_field,
                '[role="option"]': suggestion,
            },
            url=STALE_URL,
        )

        resolved = await scraper._resolve_destination(page, "Ennesma Hotel")

        assert resolved is True
        assert destination_field.typed == ["Ennesma Hotel"]
        assert suggestion.clicked is True

    async def test_no_suggestion_offered_means_not_in_booking_index(
        self, scraper: BookingScraper
    ) -> None:
        """
        The second live-diagnosed failure mode: some property names never
        resolve at all, because Booking's autocomplete has no exact match
        for them (only city/airport-level suggestions). This must fail
        cleanly (False) rather than mis-click a city-wide search and let the
        caller's fuzzy matcher latch onto an unrelated chain hotel.
        """
        page = FakePage(
            {
                'button[type="submit"]': FakeLocator(),
                'input[name="ss"]': FakeLocator(),
                # No '[role="option"]' entry -> FakePage.locator() falls back
                # to an invisible FakeLocator, i.e. no suggestion ever shows.
            },
            url=STALE_URL,
        )

        resolved = await scraper._resolve_destination(page, "Ennesma Hotel")

        assert resolved is False
        assert page.url == STALE_URL


# ---------------------------------------------------------------------------
# lookup() retry policy
# ---------------------------------------------------------------------------

class TestLookupRetryPolicy:
    """
    Live-diagnosed "block point" (2026-07): a 22-hotel window took 13 minutes
    for 3 matches, because a deterministic ``MATCH_FAIL`` (no card cleared
    the fuzzy threshold) was retried exactly like a transient ``ERROR`` -
    tripling the cost of every hotel Booking has no good match for, which is
    most of a real run. Only ``ERROR`` should trigger a retry now.
    """

    @pytest.fixture
    def window(self) -> SearchWindow:
        return SearchWindow(
            city="Casablanca",
            checkin=date(2026, 8, 1),
            checkout=date(2026, 8, 2),
            horizon_label="J+3",
            horizon_offset_days=3,
            execution_date=date(2026, 7, 29),
        )

    @pytest.fixture(autouse=True)
    def _no_real_sleeps(self, monkeypatch) -> None:
        """These tests exercise the retry loop's *count*, not its pacing -
        stub out every sleep so a run of them stays fast and deterministic."""

        async def instant(*_args, **_kwargs) -> None:
            return None

        monkeypatch.setattr("src.scrapers.booking.asyncio.sleep", instant)
        monkeypatch.setattr("src.scrapers.booking.polite_delay", instant)

    async def test_match_fail_is_not_retried(
        self, scraper: BookingScraper, window: SearchWindow
    ) -> None:
        calls = 0

        async def fake_lookup_once(_window, _name, _identity=None):
            nonlocal calls
            calls += 1
            return BookingResult(status=MatchStatus.MATCH_FAIL, error="scored too low")

        scraper._lookup_once = fake_lookup_once

        result = await scraper.lookup(window, "Some Hotel")

        assert calls == 1
        assert result.status is MatchStatus.MATCH_FAIL

    async def test_no_results_is_not_retried(
        self, scraper: BookingScraper, window: SearchWindow
    ) -> None:
        calls = 0

        async def fake_lookup_once(_window, _name, _identity=None):
            nonlocal calls
            calls += 1
            return BookingResult(status=MatchStatus.NO_RESULTS)

        scraper._lookup_once = fake_lookup_once

        await scraper.lookup(window, "Some Hotel")

        assert calls == 1

    async def test_error_is_retried_up_to_max_retries(
        self, scraper: BookingScraper, window: SearchWindow
    ) -> None:
        calls = 0

        async def fake_lookup_once(_window, _name, _identity=None):
            nonlocal calls
            calls += 1
            raise RuntimeError("transient navigation failure")

        scraper._lookup_once = fake_lookup_once

        result = await scraper.lookup(window, "Some Hotel")

        assert calls == scraper._cfg.max_retries + 1
        assert result.status is MatchStatus.ERROR

    async def test_ok_on_first_attempt_stops_immediately(
        self, scraper: BookingScraper, window: SearchWindow
    ) -> None:
        calls = 0

        async def fake_lookup_once(_window, _name, _identity=None):
            nonlocal calls
            calls += 1
            return BookingResult(status=MatchStatus.OK, price=1000.0)

        scraper._lookup_once = fake_lookup_once

        result = await scraper.lookup(window, "Some Hotel")

        assert calls == 1
        assert result.status is MatchStatus.OK

    async def test_error_then_success_recovers(
        self, scraper: BookingScraper, window: SearchWindow
    ) -> None:
        """A single flaky attempt should still resolve to a real match."""
        calls = 0

        async def fake_lookup_once(_window, _name, _identity=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("timeout")
            return BookingResult(status=MatchStatus.OK, price=1000.0)

        scraper._lookup_once = fake_lookup_once

        result = await scraper.lookup(window, "Some Hotel")

        assert calls == 2
        assert result.status is MatchStatus.OK

    async def test_blocked_error_is_retried(
        self, scraper: BookingScraper, window: SearchWindow
    ) -> None:
        calls = 0

        async def fake_lookup_once(_window, _name, _identity=None):
            nonlocal calls
            calls += 1
            raise BookingBlockedError("captcha / bot-check page served")

        scraper._lookup_once = fake_lookup_once

        result = await scraper.lookup(window, "Some Hotel")

        assert calls == scraper._cfg.max_retries + 1
        assert result.status is MatchStatus.ERROR


# ---------------------------------------------------------------------------
# Card-order-shift guard: must be fuzzy, not exact-string, comparison
# ---------------------------------------------------------------------------

class TestCardOrderShiftGuardIsFuzzy:
    """
    _lookup_once re-reads the matched card's name right before trusting its
    price/URL, to catch the result list re-sorting between _best_match's
    scan and the price read (see the Palm Plaza Marrakech / Ibis Moussafir
    live mismatch this guard was built for).

    That comparison MUST tolerate incidental text differences between the
    two reads - live logs (2026-07-30 overnight run) showed hundreds of
    false positives like matched_name="Hotel Majestic Opens in new window"
    vs reread_name="Hotel Majestic": the exact same property, just with an
    accessibility-only "Opens in new window" label present in one read and
    not the other. An earlier version of this guard used exact string
    equality and discarded (and retried) every single one of these good
    matches, which is what was actually causing multi-hour, retry-heavy
    runs that never reached the long-offset windows - not a deeper bug.
    fuzzy_score is the right tool since it's exactly what the original
    name match already uses to tolerate this kind of padding.
    """

    def test_accessibility_label_noise_is_not_a_mismatch(self) -> None:
        agreement = fuzzy_score(
            "Hotel Majestic Opens in new window", "Hotel Majestic"
        )
        assert agreement >= Settings().booking.fuzzy_threshold_strict

    def test_unavailable_banner_noise_is_not_a_mismatch(self) -> None:
        agreement = fuzzy_score(
            "Gray Hotel Casablanca Opens in new window This property is "
            "unavailable on our site for your dates",
            "Gray Hotel Casablanca",
        )
        assert agreement >= Settings().booking.fuzzy_threshold_strict

    def test_genuinely_different_hotel_is_still_caught(self) -> None:
        # The real bug this guard exists for: Palm Plaza Marrakech's price
        # was attributed from a card that had shifted to Ibis Moussafir.
        agreement = fuzzy_score(
            "Grand Plaza Marrakech", "Ibis Moussafir Marrakech Centre Gare"
        )
        assert agreement < Settings().booking.fuzzy_threshold_strict

    def test_reread_none_is_still_caught(self) -> None:
        agreement = fuzzy_score("Hotel Majestic", "")
        assert agreement < Settings().booking.fuzzy_threshold_strict


# ---------------------------------------------------------------------------
# Per-card unavailability guard
# ---------------------------------------------------------------------------

class TestCardIsUnavailable:
    """
    _card_is_unavailable must catch a matched card that is itself marked
    unavailable, BEFORE a price is read from it.

    Live-diagnosed via FIVE Zurich - Luxury City Resort: the search-results
    page showed the property's own card reading "This property is
    unavailable on our site for your dates", with a "similar properties"
    carousel of OTHER real hotels immediately below it on the same page.
    _best_match still scored the property's own card 100 (exact name
    match) and price extraction proceeded anyway, because the only
    unavailability check that existed (_is_unavailable) is a whole-page
    scan that only runs on the "nothing cleared the threshold" fallback
    path - which this case never reached. Two things had to be fixed:
    the marker list didn't even contain today's actual wording ("unavailable
    on our site"), and the check needs to run against the matched card
    itself, unconditionally, before trusting its price.
    """

    async def test_catches_live_wording_property_unavailable(
        self, scraper: BookingScraper
    ) -> None:
        card = FakeLocator(
            text="This property is unavailable on our site for your dates"
        )
        assert await scraper._card_is_unavailable(card) is True

    async def test_catches_live_wording_expanded_panel(
        self, scraper: BookingScraper
    ) -> None:
        card = FakeLocator(text="Unavailable on our site for your selected dates")
        assert await scraper._card_is_unavailable(card) is True

    async def test_normal_available_card_is_not_flagged(
        self, scraper: BookingScraper
    ) -> None:
        card = FakeLocator(
            text="FIVE Zurich - Luxury City Resort\n3 nights, 2 adults\nMAD 8,340"
        )
        assert await scraper._card_is_unavailable(card) is False

    async def test_no_configured_markers_never_flags_anything(
        self, scraper: BookingScraper
    ) -> None:
        scraper._no_avail_markers = []
        card = FakeLocator(text="anything at all")
        assert await scraper._card_is_unavailable(card) is False

# ---------------------------------------------------------------------------
# Deciding what an "unavailable" card means when destination resolution
# itself was never confirmed
# ---------------------------------------------------------------------------

class TestUnavailableCardOutcomeTrustsOnlyResolvedPages:
    """
    A matched card reading "unavailable" is only real, terminal NO_RESULTS
    evidence if we actually confirmed Booking resolved the destination
    (dest_id present). Live-diagnosed 2026-07: a production run showed
    ~60% of lookups coming back NO_RESULTS at a perfect fuzzy_score=100 -
    including Marriott/Fairmont/Hilton/Barceló properties confirmed live to
    be genuinely bookable. Root cause was _wait_for_dest_id's timeout
    (6s, hardcoded) losing a race against real navigation latency under
    BOOKING_CONCURRENCY > 1, silently falling back to the cold, unresolved,
    always-"unavailable" placeholder page. This is the second layer of the
    fix: even if that race is lost again, the reading is no longer trusted
    as final.
    """

    def test_unresolved_destination_is_retried_not_trusted(self) -> None:
        result = _unavailable_card_outcome(
            resolved=False,
            matched_name="Rabat Marriott Hotel",
            score=100.0,
            final_url="https://www.booking.com/searchresults.html?ss=Rabat+Marriott+Hotel",
        )
        assert result.status is MatchStatus.ERROR
        assert result.matched_name == "Rabat Marriott Hotel"
        assert result.fuzzy_score == 100.0
        assert "never confirmed resolved" in (result.error or "")

    def test_resolved_destination_unavailable_is_accepted_as_final(self) -> None:
        result = _unavailable_card_outcome(
            resolved=True,
            matched_name="FIVE Zurich - Luxury City Resort",
            score=100.0,
            final_url="https://www.booking.com/searchresults.html?dest_id=123&dest_type=hotel",
        )
        assert result.status is MatchStatus.NO_RESULTS
        assert result.url == "https://www.booking.com/searchresults.html?dest_id=123&dest_type=hotel"
        assert "marked unavailable" in (result.error or "")

