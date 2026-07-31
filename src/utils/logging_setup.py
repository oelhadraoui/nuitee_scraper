"""
src/utils/logging_setup.py
=============================================================================
Single place where logging is configured.

Supports plain human-readable output (default, best for `docker logs -f`) and
line-delimited JSON (`LOG_JSON=true`) for ingestion into a log aggregator.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Optional

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("city", "hotel", "horizon", "run_id", "panel"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _CompactFormatter(logging.Formatter):
    """`HH:MM:SS LEVEL [logger] message` - dense but readable."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )


def setup_logging(
    level: str = "INFO",
    json_output: bool = False,
    log_file: Optional[Path] = None,
) -> None:
    """Configure the root logger exactly once per process."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    formatter: logging.Formatter = (
        _JsonFormatter() if json_output else _CompactFormatter()
    )

    handlers: list[logging.Handler] = []

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    handlers.append(stream)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
        except PermissionError as exc:
            # The classic first-run Docker issue: log_file.parent.mkdir()
            # above succeeds even when the directory ISN'T writable (it's
            # a no-op via exist_ok=True once the directory already exists,
            # which needs no write permission), so this is the first place
            # that actually tries to write into it and fails. Almost always
            # a bind-mounted ./output owned by a UID that doesn't match the
            # container user - see the Makefile's _ensure_output target,
            # which chmods the mount specifically to prevent this. Fail
            # with a message that says what to do, not a bare traceback.
            raise PermissionError(
                f"Cannot write to {log_file} ({exc}). This almost always "
                f"means the mounted output directory's permissions don't "
                f"match the container's user. Fix: on the host, run "
                f"`mkdir -p output && chmod -R 777 output` (or just "
                f"`make doctor`/any `make run-*` target, which now does "
                f"this automatically), then retry."
            ) from exc
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)

    # Playwright is extremely chatty at DEBUG level.
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Convenience wrapper so modules never touch `logging` directly."""
    return logging.getLogger(name)
