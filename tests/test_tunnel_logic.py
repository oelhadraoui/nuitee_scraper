"""
Tests for the tunnel navigator's decision logic, using async fakes in place of
Playwright.

The tunnel is the highest-risk module in the codebase: it is the only place
that clicks buttons on a live booking flow, and a mistake there is the
difference between reading a price and making a reservation. These tests cover
the parts that can be verified without a browser: the payment guard, room
ranking, panel classification priority, and the price-extraction cascade.
"""

from __future__ import annotations

import pytest

from src.config import Settings
from src.models import PanelKind, RoomOffer, TunnelStatus
from src.scrapers.tunnel import TunnelNavigator
from src.utils.selectors import SelectorRegistry

# ---------------------------------------------------------------------------
# Async Playwright fakes
# ---------------------------------------------------------------------------

class FakeLocator:
    """Implements just enough of Playwright's Locator surface."""

    def __init__(
        self,
        text: str = "",
        attributes: dict[str, str] | None = None,
        visible: bool = True,
        disabled: bool = False,
    ) -> None:
        self._text = text
        self._attributes = attributes or {}
        self._visible = visible
        self._disabled = disabled
        self.clicked = False

    @property
    def first(self) -> "FakeLocator":
        return self

    async def inner_text(self, timeout: int = 0) -> str:
        return self._text

    async def get_attribute(self, name: str) -> str | None:
        return self._attributes.get(name)

    async def is_visible(self, timeout: int = 0) -> bool:
        return self._visible

    async def is_disabled(self) -> bool:
        return self._disabled

    async def scroll_into_view_if_needed(self, timeout: int = 0) -> None:
        return None

    async def click(self, timeout: int = 0, force: bool = False) -> None:
        if self._disabled:
            raise RuntimeError("element is disabled")
        self.clicked = True

    async def wait_for(self, state: str = "visible", timeout: int = 0) -> None:
        if not self._visible:
            raise RuntimeError("not visible")


class FakePage:
    """Maps selector strings to FakeLocators."""

    def __init__(self, mapping: dict[str, FakeLocator] | None = None) -> None:
        self.mapping = mapping or {}
        self.evaluate_result: list[str] = []

    def locator(self, selector: str) -> FakeLocator:
        return self.mapping.get(selector, FakeLocator(visible=False))

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def evaluate(self, script: str, *args) -> list[str]:
        return self.evaluate_result


@pytest.fixture(scope="module")
def registry() -> SelectorRegistry:
    return SelectorRegistry.load(Settings().selectors_path)


@pytest.fixture
def navigator(registry: SelectorRegistry) -> TunnelNavigator:
    return TunnelNavigator(
        FakePage(), Settings(), registry, hotel_name="Test Hotel"
    )


# ---------------------------------------------------------------------------
# The payment guard rail
# ---------------------------------------------------------------------------

class TestPaymentGuard:
    @pytest.mark.parametrize(
        "text",
        [
            "Pay now",
            "PAY NOW",
            "Payer maintenant",
            "Confirm and pay",
            "Complete booking",
            "Submit payment",
            "  Pay   now  ",
        ],
    )
    async def test_payment_text_is_blocked(
        self, navigator: TunnelNavigator, text: str
    ) -> None:
        assert await navigator._is_payment_trigger(FakeLocator(text)) is True

    @pytest.mark.parametrize(
        "text", ["Continue", "Next", "Reserve", "Select room", "Continuer"]
    )
    async def test_safe_text_is_allowed(
        self, navigator: TunnelNavigator, text: str
    ) -> None:
        assert await navigator._is_payment_trigger(FakeLocator(text)) is False

    @pytest.mark.parametrize(
        "text",
        [
            "\n      Pay\n      now\n    ",   # real rendered-HTML indentation
            "Pay\u00a0now",                    # non-breaking space
            "Pay now!",                        # trailing punctuation
            "P\u00e0yer maintenant",            # accented
            "PAY   NOW",
        ],
    )
    async def test_guard_survives_markup_whitespace_and_accents(
        self, navigator: TunnelNavigator, text: str
    ) -> None:
        """
        Regression: rendered HTML puts newlines and indentation inside a
        <button>. Before normalisation the guard failed open on exactly this.
        """
        assert await navigator._is_payment_trigger(FakeLocator(text)) is True

    async def test_guard_inspects_aria_label(
        self, navigator: TunnelNavigator
    ) -> None:
        """An icon-only submit button's only tell is its aria-label."""
        icon_button = FakeLocator("", {"aria-label": "Pay now"})
        assert await navigator._is_payment_trigger(icon_button) is True

    @pytest.mark.parametrize(
        "attribute", ["name", "id", "value", "data-testid"]
    )
    async def test_guard_inspects_every_identifying_attribute(
        self, navigator: TunnelNavigator, attribute: str
    ) -> None:
        locator = FakeLocator("", {attribute: "complete-booking"})
        assert await navigator._is_payment_trigger(locator) is True

    async def test_guarded_click_skips_payment_and_takes_the_safe_option(
        self, navigator: TunnelNavigator, registry: SelectorRegistry
    ) -> None:
        danger = FakeLocator("Pay now")
        safe = FakeLocator("Continue")
        navigator.page = FakePage({"#pay": danger, "#continue": safe})

        from src.utils.selectors import SelectorCascade

        cascade = SelectorCascade("test", ["#pay", "#continue"])
        clicked = await navigator._guarded_click(cascade)

        assert clicked == "#continue"
        assert danger.clicked is False
        assert safe.clicked is True

    async def test_guarded_click_returns_none_when_all_are_payment(
        self, navigator: TunnelNavigator
    ) -> None:
        from src.utils.selectors import SelectorCascade

        danger = FakeLocator("Confirm and pay")
        navigator.page = FakePage({"#pay": danger})
        assert await navigator._guarded_click(SelectorCascade("t", ["#pay"])) is None
        assert danger.clicked is False

    async def test_disabled_buttons_are_skipped(
        self, navigator: TunnelNavigator
    ) -> None:
        from src.utils.selectors import SelectorCascade

        navigator.page = FakePage({"#c": FakeLocator("Continue", disabled=True)})
        assert await navigator._guarded_click(SelectorCascade("t", ["#c"])) is None


# ---------------------------------------------------------------------------
# Room ranking
# ---------------------------------------------------------------------------

class TestRoomRanking:
    def test_cheapest_first(self) -> None:
        offers = [
            RoomOffer("Suite", 2400.0, index=0),
            RoomOffer("Standard", 1150.0, index=1),
            RoomOffer("Deluxe", 1800.0, index=2),
        ]
        offers.sort(
            key=lambda o: (o.display_price is None, o.display_price or float("inf"))
        )
        assert [o.label for o in offers] == ["Standard", "Deluxe", "Suite"]

    def test_unpriced_offers_sort_last(self) -> None:
        """An unrankable offer is still usable, but must not win by default."""
        offers = [
            RoomOffer("Unknown", None, index=0),
            RoomOffer("Standard", 1150.0, index=1),
        ]
        offers.sort(
            key=lambda o: (o.display_price is None, o.display_price or float("inf"))
        )
        assert offers[0].label == "Standard"


# ---------------------------------------------------------------------------
# Real-site architecture: 2-page flow (room_select -> booking_page)
# ---------------------------------------------------------------------------
#
# The site was live-verified (Claude-in-Chrome, 2026-07) to be exactly two
# pages, not a 5-panel SPA: /hotels/{id} (room_select) then /booking (a
# single page combining guest form + summary + payment). There is no
# "classify which panel we're on" step any more - these tests instead cover
# what replaced it: the hard exact-testid payment block, and that every
# selector path the new two-step walk depends on actually resolves.

class TestRealSiteSelectors:
    def test_booking_page_and_summary_paths_resolve(
        self, registry: SelectorRegistry
    ) -> None:
        for path in (
            "nuitee.tunnel.room_select.detect",
            "nuitee.tunnel.room_select.room_card",
            "nuitee.tunnel.room_select.cta",
            "nuitee.tunnel.summary.total_price",
            "nuitee.tunnel.booking_page.first_name",
            "nuitee.tunnel.booking_page.email",
        ):
            assert registry.get_optional(path).candidates, path

    def test_hard_block_testids_configured(self, registry: SelectorRegistry) -> None:
        testids = registry.strings("nuitee.tunnel.payment.hard_block_testids")
        assert "checkout-submit-button" in testids

    def test_panel_kind_distinguishes_the_two_real_pages(self) -> None:
        assert PanelKind.ROOM_SELECT != PanelKind.BOOKING_PAGE
        assert PanelKind.ROOM_SELECT.value == "room_select"
        assert PanelKind.BOOKING_PAGE.value == "booking_page"


class TestPaymentGuardHardBlock:
    """
    The exact-testid hard block is a second, independent line of defense
    alongside the text-token guard: it catches a submit control even if its
    visible text/aria-label never matches any configured phrase.
    """

    async def test_exact_testid_blocks_regardless_of_innocuous_text(
        self, navigator: TunnelNavigator
    ) -> None:
        # Text alone ("Continue") would pass the token guard; the testid
        # must still block it.
        locator = FakeLocator("Continue", {"data-testid": "checkout-submit-button"})
        assert await navigator._is_payment_trigger(locator) is True

    async def test_unrelated_testid_is_not_blocked_by_hard_list(
        self, navigator: TunnelNavigator
    ) -> None:
        locator = FakeLocator("Continue", {"data-testid": "hotel-choose-room-button"})
        assert await navigator._is_payment_trigger(locator) is False


# ---------------------------------------------------------------------------
# Final price extraction
# ---------------------------------------------------------------------------

class TestPriceExtraction:
    async def test_label_row_strategy_picks_the_total(
        self, navigator: TunnelNavigator
    ) -> None:
        """
        The realistic case: the summary has no dedicated total element, so the
        label-row scan must find "Total price" and read 1 380 MAD - not the
        1 150 room rate and not the 230 tax line.
        """
        page = FakePage()
        page.evaluate_result = ["Total price 1 380 MAD"]
        navigator.page = page

        price = await navigator._extract_by_label(
            ["total price", "prix total"], 150.0, 200_000.0, "MAD"
        )
        assert price == pytest.approx(1380.0)

    async def test_label_row_picks_the_largest_of_several_matches(
        self, navigator: TunnelNavigator
    ) -> None:
        page = FakePage()
        page.evaluate_result = [
            "Total price 1 380 MAD",
            "Total to pay today 1 500 MAD",
        ]
        navigator.page = page

        price = await navigator._extract_by_label(
            ["total price", "total to pay"], 150.0, 200_000.0, "MAD"
        )
        assert price == pytest.approx(1500.0)

    async def test_no_match_returns_none(
        self, navigator: TunnelNavigator
    ) -> None:
        navigator.page = FakePage()
        assert await navigator._extract_by_label(
            ["total price"], 150.0, 200_000.0, "MAD"
        ) is None

    async def test_french_labels_are_matched(
        self, navigator: TunnelNavigator
    ) -> None:
        page = FakePage()
        page.evaluate_result = ["Prix total 1 380 MAD"]
        navigator.page = page

        price = await navigator._extract_by_label(
            ["prix total"], 150.0, 200_000.0, "MAD"
        )
        assert price == pytest.approx(1380.0)


# ---------------------------------------------------------------------------
# Result semantics
# ---------------------------------------------------------------------------

class TestTunnelResultSemantics:
    def test_ok_and_partial_both_count_as_success(self) -> None:
        from src.models import TunnelResult

        assert TunnelResult(status=TunnelStatus.OK).succeeded is True
        assert TunnelResult(status=TunnelStatus.PARTIAL).succeeded is True

    @pytest.mark.parametrize(
        "status",
        [
            TunnelStatus.TUNNEL_BLOCKED,
            TunnelStatus.NO_ROOMS,
            TunnelStatus.PAYMENT_GUARD,
            TunnelStatus.ERROR,
            TunnelStatus.NOT_ATTEMPTED,
        ],
    )
    def test_failure_states_are_not_successes(self, status: TunnelStatus) -> None:
        from src.models import TunnelResult

        assert TunnelResult(status=status).succeeded is False

    def test_hidden_fees_require_both_prices(self) -> None:
        from src.models import TunnelResult

        assert TunnelResult(final_price=1380.0, display_price=1150.0).hidden_fees == (
            pytest.approx(230.0)
        )
        assert TunnelResult(final_price=1380.0).hidden_fees is None
