"""
Tests for src/utils/browser.py's text-extraction helpers.

``safe_text_no_strike`` is the fix for a real, live-confirmed bug: Nuitee's
checkout page (like Booking.com's result cards) can render a crossed-out
"was" price next to the real payable total, and a naive ``inner_text()`` read
returns both numbers with no marker distinguishing them. These tests exercise
the Python-side contract (timeout/exception handling, text stripping) using a
fake locator; the actual DOM tree-walk was verified live against the real
Nuitee booking page via the browser extension (see the tunnel.py docstring
this pairs with).
"""

from __future__ import annotations

from src.utils.browser import safe_text, safe_text_no_strike


class FakeLocator:
    """Minimal stand-in for a Playwright Locator's wait_for/evaluate surface."""

    def __init__(self, evaluate_result: str = "", raise_on_wait: bool = False) -> None:
        self._evaluate_result = evaluate_result
        self._raise_on_wait = raise_on_wait

    async def wait_for(self, state: str = "attached", timeout: int = 0) -> None:
        if self._raise_on_wait:
            raise TimeoutError("never attached")

    async def evaluate(self, script: str) -> str:
        return self._evaluate_result


class TestSafeTextNoStrike:
    async def test_returns_stripped_text(self) -> None:
        locator = FakeLocator(evaluate_result="  Total MAD 1,899.36  ")
        assert await safe_text_no_strike(locator) == "Total MAD 1,899.36"

    async def test_struck_through_text_already_excluded_by_the_page_evaluate(
        self,
    ) -> None:
        """
        The actual exclusion happens inside the injected JS (a TreeWalker that
        skips any text node whose ancestor has a line-through computed
        style) - confirmed live. This test locks in that whatever the JS
        hands back is passed through untouched (just trimmed), so e.g. a
        struck "MAD 3,798.72" that the JS already dropped never reappears
        here as a smuggled-in candidate.
        """
        # Simulates what the browser call returns once the JS has removed
        # the struck-through "was" price - only the real total survives.
        locator = FakeLocator(evaluate_result="Total MAD 1,899.36 Includes taxes and fees")
        result = await safe_text_no_strike(locator)
        assert result == "Total MAD 1,899.36 Includes taxes and fees"
        assert "3,798" not in result

    async def test_empty_result_becomes_none(self) -> None:
        locator = FakeLocator(evaluate_result="   ")
        assert await safe_text_no_strike(locator) is None

    async def test_exception_returns_none(self) -> None:
        locator = FakeLocator(raise_on_wait=True)
        assert await safe_text_no_strike(locator) is None

    async def test_behaves_like_safe_text_for_the_happy_path(self) -> None:
        """Both helpers should agree when there is nothing struck-through to hide."""
        locator = FakeLocator(evaluate_result="MAD 980.00")

        class PlainLocator(FakeLocator):
            async def inner_text(self) -> str:
                return self._evaluate_result

        plain = PlainLocator(evaluate_result="MAD 980.00")
        assert await safe_text_no_strike(locator) == await safe_text(plain)
