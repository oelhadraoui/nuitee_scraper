"""
src/utils/browser.py
=============================================================================
Browser lifecycle, anti-blocking, and page-level helpers.

Design notes
------------
* **One browser, many contexts.** Launching Chromium costs ~1.5s and ~120 MB.
  A fresh *context* costs ~40 ms and is a clean cookie/storage jar, so every
  scrape unit gets its own context while the process keeps a single browser.

* **Fingerprint rotation.** User-agent, viewport jitter, and Accept-Language
  are randomised per context. Critically, the UA is paired with matching
  ``sec-ch-ua`` client hints and platform - a Windows UA advertising
  ``platform: "Linux"`` is a trivially detectable mismatch.

* **Resource blocking.** Images/media/fonts are aborted at the route level.
  This roughly triples tunnel throughput and reduces the bandwidth signature.

* **Concurrency governor.** ``ContextPool`` bounds simultaneous contexts so
  memory stays flat and the request rate stays under the blocking threshold.
"""

from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from src.config import BrowserSettings, Settings
from src.utils.logging_setup import get_logger

log = get_logger(__name__)

# --- optional stealth plugin ------------------------------------------------
_STEALTH_KIND: Optional[str] = None
try:  # playwright-stealth >= 2.x
    from playwright_stealth import Stealth as _Stealth  # type: ignore

    _STEALTH_KIND = "class"
except ImportError:
    try:  # playwright-stealth 1.x
        from playwright_stealth import stealth_async as _stealth_async  # type: ignore

        _STEALTH_KIND = "function"
    except ImportError:
        _STEALTH_KIND = None

HAS_STEALTH = _STEALTH_KIND is not None


# ---------------------------------------------------------------------------
# Fingerprint profiles
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Fingerprint:
    """A self-consistent browser identity."""

    user_agent: str
    platform: str
    sec_ch_ua: str
    sec_ch_ua_platform: str
    accept_language: str

    def headers(self, referer: str = "https://www.google.com/") -> dict[str, str]:
        return {
            "Accept-Language": self.accept_language,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": referer,
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-User": "?1",
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": self.sec_ch_ua_platform,
            "DNT": "1",
        }


_CHROME_UA_VERSIONS = ("124", "125", "126")

FINGERPRINTS: list[Fingerprint] = []
for _v in _CHROME_UA_VERSIONS:
    _brands = (
        f'"Chromium";v="{_v}", "Google Chrome";v="{_v}", '
        f'"Not-A.Brand";v="99"'
    )
    FINGERPRINTS.extend(
        [
            Fingerprint(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{_v}.0.0.0 Safari/537.36"
                ),
                platform="Win32",
                sec_ch_ua=_brands,
                sec_ch_ua_platform='"Windows"',
                accept_language="en-US,en;q=0.9,fr;q=0.8",
            ),
            Fingerprint(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{_v}.0.0.0 Safari/537.36"
                ),
                platform="MacIntel",
                sec_ch_ua=_brands,
                sec_ch_ua_platform='"macOS"',
                accept_language="en-US,en;q=0.9,fr;q=0.8",
            ),
            Fingerprint(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{_v}.0.0.0 Safari/537.36"
                ),
                platform="Linux x86_64",
                sec_ch_ua=_brands,
                sec_ch_ua_platform='"Linux"',
                accept_language="en-GB,en;q=0.9,fr;q=0.8",
            ),
        ]
    )


def random_fingerprint() -> Fingerprint:
    return random.choice(FINGERPRINTS)


# ---------------------------------------------------------------------------
# Init script: patch the classic automation tells
# ---------------------------------------------------------------------------

_STEALTH_INIT_SCRIPT_TEMPLATE = """
(() => {
  const PLATFORM = '__PLATFORM__';

  // navigator.webdriver -> undefined
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

  // Plugins & mimeTypes: headless Chromium reports an empty list.
  const fakePlugin = (name, filename, desc) => ({
    name, filename, description: desc, length: 1,
  });
  Object.defineProperty(navigator, 'plugins', {
    get: () => [
      fakePlugin('Chrome PDF Plugin', 'internal-pdf-viewer', 'Portable Document Format'),
      fakePlugin('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', ''),
      fakePlugin('Native Client', 'internal-nacl-plugin', ''),
    ],
  });
  Object.defineProperty(navigator, 'mimeTypes', { get: () => [1, 2, 3] });

  // Platform must agree with the advertised user-agent.
  Object.defineProperty(navigator, 'platform', { get: () => PLATFORM });

  // Plausible hardware.
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
  Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

  // window.chrome runtime shim
  window.chrome = window.chrome || {};
  window.chrome.runtime = window.chrome.runtime || {};
  window.chrome.csi = window.chrome.csi || function () { return {}; };
  window.chrome.loadTimes = window.chrome.loadTimes || function () { return {}; };

  // Notification permission mismatch is a well-known headless tell.
  if (window.navigator.permissions && window.navigator.permissions.query) {
    const originalQuery = window.navigator.permissions.query.bind(
      window.navigator.permissions
    );
    window.navigator.permissions.query = (params) =>
      params && params.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(params);
  }

  // WebGL vendor/renderer strings (SwiftShader is a dead giveaway).
  const patchGL = (proto) => {
    if (!proto || !proto.getParameter) return;
    const original = proto.getParameter;
    proto.getParameter = function (parameter) {
      if (parameter === 37445) return 'Intel Inc.';
      if (parameter === 37446) return 'Intel Iris OpenGL Engine';
      return original.apply(this, arguments);
    };
  };
  if (window.WebGLRenderingContext) patchGL(WebGLRenderingContext.prototype);
  if (window.WebGL2RenderingContext) patchGL(WebGL2RenderingContext.prototype);

  // Chromium in headless mode exposes no outer dimensions on some builds.
  if (window.outerWidth === 0) {
    Object.defineProperty(window, 'outerWidth', { get: () => window.innerWidth });
    Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight + 85 });
  }
})();
"""


# ---------------------------------------------------------------------------
# Context factory
# ---------------------------------------------------------------------------

async def create_context(
    browser: Browser,
    settings: Settings,
    *,
    referer: str = "https://www.google.com/",
    block_resources: Optional[bool] = None,
    fingerprint: Optional[Fingerprint] = None,
) -> BrowserContext:
    """Create a hardened, fingerprint-rotated browser context."""
    cfg: BrowserSettings = settings.browser
    fp = fingerprint or random_fingerprint()

    jitter = cfg.viewport_jitter
    viewport = {
        "width": cfg.viewport_width + random.randint(-jitter, jitter),
        "height": cfg.viewport_height + random.randint(-jitter, jitter),
    }

    context = await browser.new_context(
        viewport=viewport,
        user_agent=fp.user_agent,
        locale=cfg.locale,
        timezone_id=cfg.timezone_id,
        device_scale_factor=random.choice([1, 1, 2]),
        is_mobile=False,
        has_touch=False,
        java_script_enabled=True,
        bypass_csp=True,
        ignore_https_errors=True,
        extra_http_headers=fp.headers(referer=referer),
    )

    await context.add_init_script(
        _STEALTH_INIT_SCRIPT_TEMPLATE.replace("__PLATFORM__", fp.platform)
    )

    should_block = cfg.block_resources if block_resources is None else block_resources
    if should_block:
        blocked = set(cfg.blocked_resource_types)

        async def _route_handler(route):  # pragma: no cover - network path
            try:
                if route.request.resource_type in blocked:
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                # The context can vanish mid-route during teardown.
                pass

        await context.route("**/*", _route_handler)

    return context


async def apply_stealth(page: Page) -> None:
    """Apply the playwright-stealth plugin if it is installed."""
    if _STEALTH_KIND == "function":
        try:
            await _stealth_async(page)  # type: ignore[name-defined]
        except Exception:
            log.debug("stealth_async failed", exc_info=True)
    elif _STEALTH_KIND == "class":
        try:
            await _Stealth().apply_stealth_async(page)  # type: ignore[name-defined]
        except Exception:
            log.debug("Stealth().apply_stealth_async failed", exc_info=True)


async def new_page(
    browser: Browser,
    settings: Settings,
    *,
    referer: str = "https://www.google.com/",
    block_resources: Optional[bool] = None,
) -> tuple[BrowserContext, Page]:
    """Create a context plus its first page, stealth already applied."""
    context = await create_context(
        browser, settings, referer=referer, block_resources=block_resources
    )
    page = await context.new_page()
    await apply_stealth(page)
    page.set_default_timeout(settings.timeouts.element_visible)
    return context, page


# ---------------------------------------------------------------------------
# Managed browser + bounded context pool
# ---------------------------------------------------------------------------

class BrowserManager:
    """
    Owns the Playwright driver and a single Chromium instance.

    Usage::

        async with BrowserManager(settings) as manager:
            async with manager.page() as (context, page):
                await page.goto(...)
    """

    def __init__(self, settings: Settings, concurrency: int = 4) -> None:
        self.settings = settings
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._contexts_created = 0

    async def __aenter__(self) -> "BrowserManager":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.settings.browser.headless,
            slow_mo=self.settings.browser.slow_mo_ms,
            args=self.settings.browser.launch_args,
        )
        log.info(
            "Chromium launched (headless=%s, stealth=%s, concurrency=%d)",
            self.settings.browser.headless,
            HAS_STEALTH,
            self._semaphore._value,
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                log.debug("browser.close() failed", exc_info=True)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                log.debug("playwright.stop() failed", exc_info=True)
        log.info("Chromium closed (%d contexts created)", self._contexts_created)

    @property
    def browser(self) -> Browser:
        if self._browser is None:
            raise RuntimeError("BrowserManager used outside its context manager")
        return self._browser

    @asynccontextmanager
    async def page(
        self,
        *,
        referer: str = "https://www.google.com/",
        block_resources: Optional[bool] = None,
    ) -> AsyncIterator[tuple[BrowserContext, Page]]:
        """Acquire a concurrency slot, yield a fresh (context, page), clean up."""
        async with self._semaphore:
            context, page_obj = await new_page(
                self.browser,
                self.settings,
                referer=referer,
                block_resources=block_resources,
            )
            self._contexts_created += 1
            try:
                yield context, page_obj
            finally:
                try:
                    await context.close()
                except Exception:
                    log.debug("context.close() failed", exc_info=True)


# ---------------------------------------------------------------------------
# Page-level helpers
# ---------------------------------------------------------------------------

async def safe_text(locator, timeout: int = 2_000) -> Optional[str]:
    """``inner_text`` that returns ``None`` instead of raising."""
    try:
        await locator.wait_for(state="attached", timeout=timeout)
        return (await locator.inner_text()).strip()
    except Exception:
        return None


async def safe_click(locator, timeout: int = 4_000, force: bool = False) -> bool:
    """``click`` that reports success as a bool instead of raising."""
    try:
        await locator.scroll_into_view_if_needed(timeout=min(timeout, 3_000))
    except Exception:
        pass
    try:
        await locator.click(timeout=timeout, force=force)
        return True
    except Exception:
        return False


async def is_visible(locator, timeout: int = 1_000) -> bool:
    try:
        return await locator.is_visible(timeout=timeout)
    except Exception:
        return False


async def first_visible(page: Page, selectors: list[str], timeout: int = 1_500):
    """Return the first locator from ``selectors`` that is visible, else None."""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=timeout):
                return locator
        except Exception:
            continue
    return None


async def wait_for_any(
    page: Page, selectors: list[str], timeout: int = 15_000
) -> Optional[str]:
    """
    Race several selectors; return the one that appeared first.

    Used everywhere in the tunnel, where the next panel could legitimately be
    any of three different screens depending on the property's configuration.
    """
    if not selectors:
        return None
    combined = ", ".join(selectors)
    try:
        await page.wait_for_selector(combined, timeout=timeout, state="visible")
    except Exception:
        return None
    for selector in selectors:
        if await is_visible(page.locator(selector).first, timeout=400):
            return selector
    return combined


async def dismiss_overlays(page: Page, selectors: list[str], timeout: int = 700) -> int:
    """
    Click every visible cookie banner / modal / sign-in nag.

    Returns the number of elements dismissed. Never raises - an overlay that
    refuses to close must not abort the scrape.
    """
    dismissed = 0
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = min(await locator.count(), 5)
        except Exception:
            continue
        for index in range(count):
            try:
                element = locator.nth(index)
                if not await element.is_visible(timeout=300):
                    continue
                await element.click(timeout=timeout, force=True)
                dismissed += 1
                await page.wait_for_timeout(120)
            except Exception:
                continue
    return dismissed


async def install_overlay_autodismiss(page: Page, selectors: list[str]) -> None:
    """
    Register Playwright locator handlers so overlays are cleared automatically
    whenever they interrupt an action - critical inside an SPA tunnel where a
    modal can appear between two clicks.
    """
    for selector in selectors:
        try:
            async def _handler(locator, _sel=selector):
                try:
                    await locator.click(timeout=1_000, force=True)
                except Exception:
                    pass

            await page.add_locator_handler(page.locator(selector).first, _handler)
        except Exception:
            continue


async def human_scroll(page: Page, distance: int, steps: int = 4) -> None:
    """Scroll in several small steps with pauses - mimics a real user."""
    per_step = max(1, distance // steps)
    for _ in range(steps):
        try:
            await page.mouse.wheel(0, per_step)
        except Exception:
            await page.evaluate(f"window.scrollBy(0, {per_step})")
        await page.wait_for_timeout(random.randint(60, 180))
