"""
Tests for src/utils/logging_setup.py's permission-error handling.

Why this exists
----------------
The classic first-run Docker issue: ``docker compose run --rm scraper
doctor`` against a freshly-created (or wrongly-owned) ``./output`` bind
mount fails with a bare ``PermissionError`` traceback from deep inside
``logging.FileHandler.__init__`` - not obviously about file permissions to
someone seeing it for the first time. ``log_file.parent.mkdir(parents=True,
exist_ok=True)`` doesn't catch this: it's a no-op requiring no write
permission once the directory already exists, so ``FileHandler`` opening
the actual log file is the first place that really needs write access and
therefore the first place that fails. These tests lock in that the failure
now raises a clear, actionable message instead of a bare traceback, and
that normal (writable) setup is unaffected.
"""

from __future__ import annotations

import logging
import stat
from pathlib import Path

import pytest

from src.utils import logging_setup


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """setup_logging() only configures once per process - reset between tests."""
    logging_setup._CONFIGURED = False
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    for handler in original_handlers:
        root.removeHandler(handler)
    yield
    logging_setup._CONFIGURED = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in original_handlers:
        root.addHandler(handler)


class TestSetupLoggingPermissionError:
    def test_unwritable_log_dir_raises_actionable_message(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "output"
        log_dir.mkdir()
        log_dir.chmod(stat.S_IREAD | stat.S_IEXEC)  # read+execute, no write

        try:
            with pytest.raises(PermissionError) as exc_info:
                logging_setup.setup_logging(log_file=log_dir / "scraper.log")
            message = str(exc_info.value)
            assert "chmod -R 777 output" in message
            assert "make doctor" in message
        finally:
            # restore so pytest can clean up tmp_path afterwards
            log_dir.chmod(stat.S_IRWXU)

    def test_writable_log_dir_succeeds_normally(self, tmp_path: Path) -> None:
        log_file = tmp_path / "output" / "scraper.log"
        logging_setup.setup_logging(log_file=log_file)
        assert log_file.parent.is_dir()

        logging.getLogger("test").info("hello")
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert log_file.exists()
        assert "hello" in log_file.read_text(encoding="utf-8")
