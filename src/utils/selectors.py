"""
src/utils/selectors.py
=============================================================================
Loader and resolution engine for ``config/selectors.yaml``.

The whole point of externalising selectors is that a site redesign becomes a
YAML edit rather than a Python change. This module provides the runtime that
makes that work:

    registry = SelectorRegistry.load(path)
    cascade  = registry.get("nuitee.tunnel.summary.total_price")
    element  = await cascade.first_visible(page)

``SelectorCascade`` also records which candidate actually matched. Those hit
counts are dumped at the end of a run, so you can see that (say) candidate #4
is doing all the work and promote it to the top of the list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from src.utils.logging_setup import get_logger

log = get_logger(__name__)


class SelectorNotFound(KeyError):
    """Raised when a dotted selector path is missing from the YAML."""


@dataclass
class SelectorCascade:
    """
    An ordered list of candidate selectors for one logical element.

    Resolution semantics: try each candidate in order, return the first that
    is visible/attached. Candidates that never match cost only a short timeout.
    """

    path: str
    candidates: list[str]
    hits: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.candidates)

    def __iter__(self) -> Iterable[str]:
        return iter(self.candidates)

    def format(self, **kwargs: Any) -> "SelectorCascade":
        """Substitute ``{placeholders}`` (e.g. ``{iso}``, ``{n}``)."""
        rendered = []
        for candidate in self.candidates:
            try:
                rendered.append(candidate.format(**kwargs))
            except (KeyError, IndexError, ValueError):
                # A literal brace in a CSS selector - keep it verbatim.
                rendered.append(candidate)
        return SelectorCascade(path=self.path, candidates=rendered, hits=self.hits)

    @property
    def combined(self) -> str:
        """All candidates as one comma-joined selector, for racing waits."""
        return ", ".join(self.candidates)

    def _record(self, candidate: str) -> None:
        self.hits[candidate] = self.hits.get(candidate, 0) + 1

    # -- async resolution ---------------------------------------------------

    async def first_visible(self, scope, timeout: int = 1_200):
        """First candidate locator that is visible within ``scope``."""
        for candidate in self.candidates:
            try:
                locator = scope.locator(candidate).first
                if await locator.is_visible(timeout=timeout):
                    self._record(candidate)
                    return locator
            except Exception:
                continue
        return None

    async def first_attached(self, scope, timeout: int = 1_200):
        """
        First candidate that exists in the DOM, visible or not.

        Needed for summary totals that live inside a collapsed accordion.
        """
        for candidate in self.candidates:
            try:
                locator = scope.locator(candidate).first
                await locator.wait_for(state="attached", timeout=timeout)
                self._record(candidate)
                return locator
            except Exception:
                continue
        return None

    async def all_matching(self, scope, timeout: int = 1_500):
        """
        Locator handle for the first candidate that matches >= 1 element.

        Returns ``(locator, count)`` or ``(None, 0)``.
        """
        for candidate in self.candidates:
            try:
                locator = scope.locator(candidate)
                await locator.first.wait_for(state="attached", timeout=timeout)
                count = await locator.count()
                if count:
                    self._record(candidate)
                    return locator, count
            except Exception:
                continue
        return None, 0

    async def wait_for(self, page, timeout: int = 15_000, state: str = "visible") -> bool:
        """Race every candidate at once; True if any appeared."""
        if not self.candidates:
            return False
        try:
            await page.wait_for_selector(self.combined, timeout=timeout, state=state)
            return True
        except Exception:
            return False

    async def click(self, scope, timeout: int = 5_000, force: bool = False) -> Optional[str]:
        """
        Click the first clickable candidate. Returns the selector used, or None.
        """
        for candidate in self.candidates:
            try:
                locator = scope.locator(candidate).first
                if not await locator.is_visible(timeout=800):
                    continue
                try:
                    await locator.scroll_into_view_if_needed(timeout=2_000)
                except Exception:
                    pass
                await locator.click(timeout=timeout, force=force)
                self._record(candidate)
                return candidate
            except Exception:
                continue
        return None

    async def fill(self, scope, value: str, timeout: int = 4_000) -> bool:
        """Type ``value`` into the first fillable candidate."""
        for candidate in self.candidates:
            try:
                locator = scope.locator(candidate).first
                if not await locator.is_visible(timeout=800):
                    continue
                await locator.fill("", timeout=timeout)
                await locator.type(value, delay=35)
                self._record(candidate)
                return True
            except Exception:
                continue
        return False

    async def text(self, scope, timeout: int = 1_500) -> Optional[str]:
        """Inner text of the first matching candidate."""
        locator = await self.first_attached(scope, timeout=timeout)
        if locator is None:
            return None
        try:
            return (await locator.inner_text()).strip()
        except Exception:
            return None

    async def any_present(self, scope, timeout: int = 800) -> bool:
        """True if at least one candidate is currently visible."""
        return await self.first_visible(scope, timeout=timeout) is not None


class SelectorRegistry:
    """Dotted-path access to the whole selector YAML tree."""

    def __init__(self, data: dict[str, Any], source: Optional[Path] = None) -> None:
        self._data = data
        self._source = source
        self._cache: dict[str, SelectorCascade] = {}

    # -- construction -------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "SelectorRegistry":
        if not path.is_file():
            raise FileNotFoundError(
                f"Selector configuration not found at {path}. "
                "Copy config/selectors.yaml into place."
            )
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        log.info("Loaded selector registry from %s", path)
        return cls(data, source=path)

    # -- access -------------------------------------------------------------

    def raw(self, dotted_path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted_path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def get(self, dotted_path: str) -> SelectorCascade:
        """
        Cascade at ``dotted_path``.

        Raises ``SelectorNotFound`` rather than silently returning an empty
        cascade - a typo in a selector path must fail loudly at start-up, not
        manifest as a zero-yield scrape three hours in.
        """
        if dotted_path in self._cache:
            return self._cache[dotted_path]

        node = self.raw(dotted_path, default=None)
        if node is None:
            raise SelectorNotFound(dotted_path)
        if isinstance(node, str):
            node = [node]
        if not isinstance(node, list):
            raise SelectorNotFound(
                f"{dotted_path} resolves to {type(node).__name__}, expected a list"
            )

        cascade = SelectorCascade(path=dotted_path, candidates=[str(c) for c in node])
        self._cache[dotted_path] = cascade
        return cascade

    def get_optional(self, dotted_path: str) -> SelectorCascade:
        """Like :meth:`get` but returns an empty cascade instead of raising."""
        try:
            return self.get(dotted_path)
        except SelectorNotFound:
            return SelectorCascade(path=dotted_path, candidates=[])

    def strings(self, dotted_path: str) -> list[str]:
        """A plain list of strings (text markers, not CSS selectors)."""
        node = self.raw(dotted_path, default=[])
        if isinstance(node, str):
            return [node]
        return [str(item) for item in (node or [])]

    def validate(self, required_paths: Iterable[str]) -> list[str]:
        """Return the subset of ``required_paths`` that are missing."""
        missing = []
        for path in required_paths:
            try:
                cascade = self.get(path)
                if not cascade.candidates:
                    missing.append(path)
            except SelectorNotFound:
                missing.append(path)
        return missing

    # -- telemetry ----------------------------------------------------------

    def hit_report(self) -> dict[str, dict[str, int]]:
        """
        Which candidate actually matched, per cascade.

        Feed this back into the YAML: promote high-hit candidates, delete
        candidates that never fired.
        """
        return {
            path: dict(cascade.hits)
            for path, cascade in self._cache.items()
            if cascade.hits
        }


#: Paths that must exist for the pipeline to function at all.
REQUIRED_SELECTOR_PATHS: list[str] = [
    "nuitee.destination_input",
    "nuitee.date_picker",
    "nuitee.hotel_card",
    "nuitee.hotel_card_name",
    "nuitee.hotel_card_price",
    "nuitee.hotel_name_filter",
    "nuitee.see_availability_link",
    "nuitee.tunnel.room_select.detect",
    "nuitee.tunnel.room_select.room_card",
    "nuitee.tunnel.room_select.cta",
    "nuitee.tunnel.summary.detect",
    "nuitee.tunnel.summary.total_price",
    "nuitee.tunnel.payment.detect",
    "nuitee.tunnel.payment.hard_block_testids",
    "booking.property_card",
    "booking.card_name",
    "booking.card_price",
]
