"""
Tests for BookingScraper's destination-resolution logic, using async fakes in
place of Playwright.

Why this module exists
-----------------------
A live investigation (real browser against the real site, 2026-07) found
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

import pytest

from src.config import Settings
from src.scrapers.booking import BookingScraper
from src.utils.selectors import SelectorRegistry

# ---------------------------------------------------------------------------
# Async Playwright fakes
# ---------------------------------------------------------------------------

class FakeLocator:
    """Implements just enough of Playwright's Locator surface."""

    def __init__(self, visible: bool = True, on_click=None) -> None:
        self._visible = visible
        self._on_click = on_click
        self.clicked = False
        self.filled: list[str] = []
        self.typed: list[str] = []

    @property
    def first(self) -> "FakeLocator":
        return self

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
