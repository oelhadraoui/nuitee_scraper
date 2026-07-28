"""
Tests for environment-variable configuration loading.

Regression coverage for a real bug: pydantic-settings auto-detects "complex"
field types (list, dict, set) and attempts to json.loads() the raw env string
BEFORE any custom validator runs. A field declared as `list[str]` with a
comma-separated env value like `image,media,font` therefore crashed at the
settings-source layer with a JSONDecodeError, never reaching the CSV-splitting
validator. `NoDecode` (src/config.py's `CsvList` / `CsvIntList` aliases) fixes
this by opting those fields out of the automatic decode.

These tests set the exact environment variables shipped in `.env.example` -
i.e. what `docker-compose`'s `env_file: .env` actually injects into the
container - and must never raise.
"""

from __future__ import annotations

import pytest

from src.config import reload_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets a fresh settings cache so env changes actually apply."""
    yield
    reload_settings()


class TestCsvEnvFields:
    """Every list-typed setting must accept a plain comma-separated string."""

    def test_blocked_resource_types_from_csv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BROWSER_BLOCKED_RESOURCE_TYPES", "image,media,font")
        settings = reload_settings()
        assert settings.browser.blocked_resource_types == ["image", "media", "font"]

    def test_horizon_durations_from_csv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HORIZON_STAY_DURATIONS_NIGHTS", "3,10")
        settings = reload_settings()
        assert settings.horizon.stay_durations_nights == [3, 10]

    def test_payment_guard_tokens_from_csv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "TUNNEL_PAYMENT_GUARD_TOKENS", "pay now,payer maintenant"
        )
        settings = reload_settings()
        assert settings.tunnel.payment_guard_tokens == [
            "pay now", "payer maintenant",
        ]

    def test_extra_args_from_csv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BROWSER_EXTRA_ARGS", "--foo,--bar")
        settings = reload_settings()
        assert settings.browser.extra_args == ["--foo", "--bar"]

    def test_json_array_form_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both syntaxes must work: plain CSV and an explicit JSON array."""
        monkeypatch.setenv("HORIZON_STAY_DURATIONS_NIGHTS", "[3, 10, 14]")
        settings = reload_settings()
        assert settings.horizon.stay_durations_nights == [3, 10, 14]

    def test_unset_field_keeps_python_default(self) -> None:
        """No env var present must not touch the NoDecode-aliased field at all."""
        settings = reload_settings()
        assert settings.browser.blocked_resource_types == ["image", "media", "font"]
        assert settings.horizon.stay_durations_nights == [3, 10]


class TestFullEnvExampleFile:
    """
    The exact scenario that crashed: every variable in `.env.example` set as a
    real process environment variable, exactly as docker-compose's
    `env_file: .env` injects them into the container.
    """

    def test_env_example_values_do_not_crash_settings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("BROWSER_BLOCKED_RESOURCE_TYPES", "image,media,font")
        monkeypatch.setenv("HORIZON_STAY_DURATIONS_NIGHTS", "3,10")
        monkeypatch.setenv("STORAGE_OUTPUT_DIR", str(tmp_path))

        settings = reload_settings()

        assert settings.browser.blocked_resource_types == ["image", "media", "font"]
        assert settings.horizon.stay_durations_nights == [3, 10]
