"""
src/utils/retry.py
=============================================================================
Async retry helpers with exponential backoff and full jitter.

Jitter matters more than the backoff itself: without it, N concurrent workers
that fail on the same rate-limit will retry in lockstep and trip the limit
again. Full jitter (``sleep = random(0, base * 2**attempt)``) decorrelates them.
"""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Iterable, Optional, TypeVar

from src.utils.logging_setup import get_logger

log = get_logger(__name__)

T = TypeVar("T")


class RetryExhausted(RuntimeError):
    """Raised when every attempt failed."""

    def __init__(self, label: str, attempts: int, last_error: BaseException | None):
        self.label = label
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"{label!r} failed after {attempts} attempt(s): {last_error!r}"
        )


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    label: str = "operation",
    retry_on: Iterable[type[BaseException]] = (Exception,),
    on_retry: Optional[Callable[[int, BaseException], Awaitable[None]]] = None,
) -> T:
    """
    Call ``func`` up to ``attempts`` times with full-jitter exponential backoff.

    ``on_retry`` runs between attempts - use it to reset page state, rotate a
    browser context, or emit a metric.
    """
    retry_types = tuple(retry_on)
    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except asyncio.CancelledError:
            raise
        except retry_types as exc:
            last_error = exc
            if attempt >= attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            sleep_for = random.uniform(0, delay)
            log.warning(
                "%s failed (attempt %d/%d): %s - retrying in %.1fs",
                label, attempt, attempts, exc, sleep_for,
            )
            if on_retry is not None:
                try:
                    await on_retry(attempt, exc)
                except Exception:
                    log.debug("on_retry hook raised", exc_info=True)
            await asyncio.sleep(sleep_for)

    raise RetryExhausted(label, attempts, last_error)


async def polite_delay(min_seconds: float = 0.4, max_seconds: float = 1.6) -> None:
    """Randomised inter-request pause - flattens the request-rate signature."""
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))
