"""
src/config.py
=============================================================================
Centralised, validated configuration for the Nuitee -> Booking.com price
competitiveness pipeline.

Everything is driven by environment variables (loaded from ``.env``) and
validated through Pydantic Settings, so a misconfigured deployment fails fast
at start-up instead of half-way through a six-hour scraping run.

Configuration is grouped into cohesive sub-models:

    BrowserSettings   - Playwright launch / context / anti-blocking knobs
    TimeoutSettings   - every millisecond timeout used anywhere in the code
    TunnelSettings    - Nuitee checkout-tunnel behaviour and the payment guard
    BookingSettings   - Booking.com matching + extraction behaviour
    HorizonSettings   - J+3 / J+7 rolling-window generation
    QualitySettings   - the "1,000 clean hotels" acceptance gate
    StorageSettings   - SQLite / CSV output locations

Usage
-----
    from src.config import get_settings

    settings = get_settings()          # cached singleton
    settings.timeouts.page_load        # -> 60000
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# ---------------------------------------------------------------------------
# CSV-from-env fields
# ---------------------------------------------------------------------------
# pydantic-settings auto-detects "complex" field types (list, dict, set, ...)
# and tries to json.loads() the raw env string BEFORE any of our validators
# ever run. That means `BROWSER_BLOCKED_RESOURCE_TYPES=image,media,font` -
# a perfectly good comma-separated value - blows up at the settings-source
# layer with "Expecting value: line 1 column 1", never reaching the
# CSV-splitting `field_validator(mode="before")` defined further down.
#
# `NoDecode` opts a field out of that automatic JSON attempt, so the raw
# string reaches our validator untouched. Every list-typed field meant to
# accept a comma-separated environment value uses one of these aliases.
CsvList = Annotated[list[str], NoDecode]
CsvIntList = Annotated[list[int], NoDecode]

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_OUTPUT_DIR = (
    Path("/app/output") if Path("/app/output").is_dir() else PROJECT_ROOT / "output"
)


# ---------------------------------------------------------------------------
# Browser / anti-blocking
# ---------------------------------------------------------------------------

class BrowserSettings(BaseSettings):
    """Playwright launch and browser-context configuration."""

    model_config = SettingsConfigDict(env_prefix="BROWSER_", extra="ignore")

    headless: bool = True
    slow_mo_ms: int = Field(default=0, ge=0, le=5_000)

    #: Viewport is randomised +/- jitter per context to break fingerprint reuse.
    viewport_width: int = Field(default=1280, ge=800, le=2560)
    viewport_height: int = Field(default=800, ge=600, le=1600)
    viewport_jitter: int = Field(default=40, ge=0, le=200)

    locale: str = "en-US"
    timezone_id: str = "Africa/Casablanca"

    #: Blocking images/fonts/media cuts bandwidth ~70% and speeds up the tunnel.
    block_resources: bool = True
    blocked_resource_types: CsvList = Field(
        default_factory=lambda: ["image", "media", "font"]
    )

    #: Extra Chromium flags appended to the hardened defaults.
    extra_args: CsvList = Field(default_factory=list)

    @field_validator("blocked_resource_types", "extra_args", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        """Allow ``BROWSER_EXTRA_ARGS="--foo,--bar"`` in the environment."""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            if value.startswith("["):
                return json.loads(value)
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def launch_args(self) -> list[str]:
        """Hardened Chromium flags: stability in Docker + automation masking."""
        base = [
            # --- automation masking -------------------------------------
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-infobars",
            "--disable-notifications",
            # --- container stability ------------------------------------
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-zygote",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            # --- performance --------------------------------------------
            "--disable-extensions",
            "--mute-audio",
            f"--window-size={self.viewport_width},{self.viewport_height}",
        ]
        return base + list(self.extra_args)


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------

class TimeoutSettings(BaseSettings):
    """Every timeout in milliseconds. Named, not magic numbers."""

    model_config = SettingsConfigDict(env_prefix="TIMEOUT_", extra="ignore")

    # --- navigation --------------------------------------------------------
    page_load: int = 60_000
    network_idle: int = 45_000
    url_change: int = 45_000

    # --- search / results --------------------------------------------------
    selector_wait: int = 45_000
    first_card: int = 30_000
    suggestion_click: int = 10_000
    element_visible: int = 5_000

    # --- Nuitee search form ------------------------------------------------
    after_goto: int = 2_000
    after_destination: int = 1_500
    calendar_open: int = 1_500
    after_day_click: int = 800
    after_guests_open: int = 1_000
    after_guests_apply: int = 800
    adult_btn_click: int = 150
    add_room: int = 800
    next_month_btn: int = 500
    scroll_settle: int = 800

    # --- Nuitee checkout tunnel -------------------------------------------
    tunnel_panel: int = 25_000
    tunnel_transition: int = 1_200
    tunnel_cta: int = 12_000
    tunnel_price_settle: int = 1_500
    room_list: int = 20_000

    # --- Booking.com -------------------------------------------------------
    cookie_btn: int = 5_000
    popup_dismiss: int = 3_000
    price_selector: int = 5_000
    card_name: int = 3_000
    search_btn_visible: int = 4_000
    after_search_click: int = 2_500
    post_search: int = 8_000


# ---------------------------------------------------------------------------
# Nuitee checkout tunnel
# ---------------------------------------------------------------------------

class TunnelSettings(BaseSettings):
    """
    Behaviour of the multi-panel Nuitee reservation tunnel.

    The tunnel is a single-page-application flow:

        results -> hotel detail -> room selection -> [guest form]
                -> final summary (payable total)  ||  PAYMENT (never entered)

    ``payment_guard_tokens`` is a hard safety rail: if any of these strings
    appear on an element the navigator is about to click, the click is aborted
    and the run for that hotel is failed rather than risk submitting a booking.
    """

    model_config = SettingsConfigDict(env_prefix="TUNNEL_", extra="ignore")

    enabled: bool = True

    #: Max panels to traverse before declaring the tunnel structure unknown.
    max_panels: int = Field(default=8, ge=1, le=20)

    #: Retries for the whole per-hotel tunnel walk.
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_backoff_seconds: float = Field(default=3.0, ge=0.0, le=60.0)

    #: How many of the cheapest rooms to try if the first one fails to proceed.
    room_candidates: int = Field(default=3, ge=1, le=10)

    #: Fill the guest form with synthetic data to unlock the summary panel.
    fill_guest_details: bool = True
    guest_first_name: str = "Amine"
    guest_last_name: str = "Benali"
    guest_email: str = "reservations.audit@example.com"
    guest_phone: str = "+212600000000"
    guest_country: str = "Morocco"

    #: Accept the price found on an earlier panel if the final one is unreachable.
    allow_partial_tunnel: bool = True

    #: Save a screenshot + HTML dump whenever the tunnel fails (debugging aid).
    debug_artifacts: bool = False

    #: SAFETY RAIL - never click anything matching these tokens.
    payment_guard_tokens: CsvList = Field(
        default_factory=lambda: [
            "pay now",
            "payer maintenant",
            "confirm and pay",
            "confirmer et payer",
            "complete booking",
            "finaliser la reservation",
            "submit payment",
            "place order",
        ]
    )

    @field_validator("payment_guard_tokens", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            if value.strip().startswith("["):
                return json.loads(value)
            return [v.strip().lower() for v in value.split(",") if v.strip()]
        return [str(v).lower() for v in value]


# ---------------------------------------------------------------------------
# Booking.com
# ---------------------------------------------------------------------------

class BookingSettings(BaseSettings):
    """Booking.com lookup, matching, and extraction behaviour."""

    model_config = SettingsConfigDict(env_prefix="BOOKING_", extra="ignore")

    base_url: str = "https://www.booking.com/searchresults.html"
    currency: str = "MAD"
    language: str = "en-us"

    #: Minimum rapidfuzz token_set_ratio to accept a property card as a match.
    fuzzy_threshold: float = Field(default=85.0, ge=0.0, le=100.0)

    #: Secondary, stricter threshold used when the city token is absent.
    fuzzy_threshold_strict: float = Field(default=92.0, ge=0.0, le=100.0)

    #: How many result cards to consider when picking the best match.
    max_cards_to_scan: int = Field(default=10, ge=1, le=25)

    concurrency: int = Field(default=4, ge=1, le=32)
    max_retries: int = Field(default=2, ge=0, le=5)


# ---------------------------------------------------------------------------
# Nuitee search / harvest
# ---------------------------------------------------------------------------

class NuiteeSettings(BaseSettings):
    """Nuitee results-page harvesting behaviour."""

    model_config = SettingsConfigDict(env_prefix="NUITEE_", extra="ignore")

    url: str = "https://amine-sadik-2911w.nuitee.link/"
    currency: str = "MAD"

    #: Hotels to take per city per horizon date (cheapest-first after harvest).
    hotels_per_city: int = Field(default=25, ge=1, le=500)

    #: Parallel tunnel walks. Kept low - the tunnel is stateful and heavy.
    tunnel_concurrency: int = Field(default=3, ge=1, le=16)

    max_scroll_passes: int = Field(default=400, ge=5, le=5_000)
    max_scroll_seconds: int = Field(default=420, ge=10, le=7_200)
    scroll_stall_limit: int = Field(default=4, ge=1, le=20)


# ---------------------------------------------------------------------------
# Time horizons
# ---------------------------------------------------------------------------

class HorizonSettings(BaseSettings):
    """
    Rolling multi-window analysis configuration.

    Horizon 1 = check-in at J+3 (short term)
    Horizon 2 = check-in at J+7 (one week out)

    Both are swept across a rolling execution window of ``months`` months,
    with an automatic fallback to ``fallback_months`` when inventory or
    availability is insufficient.
    """

    model_config = SettingsConfigDict(env_prefix="HORIZON_", extra="ignore")

    offsets_days: CsvIntList = Field(default_factory=lambda: [3, 7])
    months: int = Field(default=3, ge=1, le=12)
    fallback_months: int = Field(default=2, ge=1, le=12)

    #: Nights per stay (check-out = check-in + nights).
    nights: int = Field(default=1, ge=1, le=30)

    #: Sample every N days inside the window (1 = every day, 7 = weekly).
    #:
    #: Daily sampling over a 3-month window produces ~92 execution dates; at
    #: 2 horizons x 1 city that is already 184 windows, and every window costs
    #: several minutes (harvest + per-hotel checkout tunnel + Booking lookup).
    #: A live run at the daily default took ~20 minutes for 2 windows -
    #: multi-day for a full plan, and nowhere near what "repeat the analysis
    #: over a 2-3 month window" requires: the client asked for the horizon
    #: comparison to hold across that whole window, not for literally every
    #: calendar day inside it to be scraped. Weekly sampling still spans the
    #: full window (about 13 execution dates per month) at a runtime that
    #: actually finishes.
    step_days: int = Field(default=7, ge=1, le=30)

    #: Minimum clean rows a window must yield before the fallback kicks in.
    #: Must be achievable given ``NUITEE_HOTELS_PER_CITY`` (a window can never
    #: yield more clean rows than hotels it harvested) - the previous default
    #: of 50 was higher than the 25-hotel-per-city ceiling, so the fallback
    #: condition was unsatisfiable-early-exit's opposite: it fired on nearly
    #: every run regardless of real inventory.
    min_rows_per_window: int = Field(default=10, ge=0)

    @field_validator("offsets_days", mode="before")
    @classmethod
    def _parse_offsets(cls, value: Any) -> Any:
        if isinstance(value, str):
            if value.strip().startswith("["):
                return json.loads(value)
            return [int(v.strip()) for v in value.split(",") if v.strip()]
        return value

    @model_validator(mode="after")
    def _check_fallback(self) -> "HorizonSettings":
        """
        The fallback window can never be wider than the primary window.

        Clamped rather than rejected: a user who sets ``HORIZON_MONTHS=1`` for
        a quick run should get a 1-month fallback, not a start-up crash.
        """
        if self.fallback_months > self.months:
            object.__setattr__(self, "fallback_months", self.months)
        return self


# ---------------------------------------------------------------------------
# Data quality gate
# ---------------------------------------------------------------------------

class QualitySettings(BaseSettings):
    """
    The client-facing acceptance criteria (Requirement B).

    A row is "clean" when ALL of the following hold:
      * hotel matched on Booking.com (status OK)
      * Nuitee final payable price present and within sane bounds
      * Booking price present and within sane bounds
      * currency normalised to MAD
      * no error status of any kind
    """

    model_config = SettingsConfigDict(env_prefix="QUALITY_", extra="ignore")

    target_clean_hotels: int = Field(default=1_000, ge=1)

    #: The client's second acceptance criterion: every crossing of the two
    #: analysis variables (city x horizon) must independently reach this many
    #: clean hotels - hitting 1,000 clean rows all from one city/horizon pair
    #: would satisfy ``target_clean_hotels`` but not be a statistically sound
    #: sample across the comparison the client actually asked for.
    min_clean_hotels_per_crossing: int = Field(default=200, ge=1)

    min_price_mad: float = Field(default=150.0, ge=0.0)
    max_price_mad: float = Field(default=200_000.0, gt=0.0)

    #: Reject a comparison when the two sites disagree by more than this ratio;
    #: almost always signals a mis-match rather than a real price gap.
    max_price_ratio: float = Field(default=12.0, gt=1.0)

    #: Deduplication key components.
    dedup_keys: CsvList = Field(
        default_factory=lambda: [
            "hotel_name_normalised",
            "city",
            "checkin",
            "checkout",
            "horizon_label",
        ]
    )

    #: Fail the process with a non-zero exit code if the target is not met.
    enforce_target: bool = False


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class StorageSettings(BaseSettings):
    """SQLite-backed storage with CSV/Parquet export."""

    model_config = SettingsConfigDict(env_prefix="STORAGE_", extra="ignore")

    output_dir: Path = DEFAULT_OUTPUT_DIR
    database_name: str = "hotel_prices.db"
    csv_name: str = "prices.csv"
    clean_csv_name: str = "prices_clean.csv"
    report_name: str = "quality_report.json"

    #: Rows buffered before a batch commit.
    batch_size: int = Field(default=25, ge=1, le=1_000)

    #: Skip a (hotel, city, dates, horizon) tuple already stored OK - makes
    #: re-running an interrupted batch idempotent and cheap.
    resume: bool = True

    @property
    def database_path(self) -> Path:
        return self.output_dir / self.database_name

    @property
    def csv_path(self) -> Path:
        return self.output_dir / self.csv_name

    @property
    def clean_csv_path(self) -> Path:
        return self.output_dir / self.clean_csv_name

    @property
    def report_path(self) -> Path:
        return self.output_dir / self.report_name


# ---------------------------------------------------------------------------
# Root settings
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """Root configuration object - compose of all sub-settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False

    selectors_path: Path = DEFAULT_CONFIG_DIR / "selectors.yaml"

    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    timeouts: TimeoutSettings = Field(default_factory=TimeoutSettings)
    tunnel: TunnelSettings = Field(default_factory=TunnelSettings)
    booking: BookingSettings = Field(default_factory=BookingSettings)
    nuitee: NuiteeSettings = Field(default_factory=NuiteeSettings)
    horizon: HorizonSettings = Field(default_factory=HorizonSettings)
    quality: QualitySettings = Field(default_factory=QualitySettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)

    @model_validator(mode="after")
    def _prepare_dirs(self) -> "Settings":
        self.storage.output_dir.mkdir(parents=True, exist_ok=True)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached singleton settings object."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the cache and re-read the environment (useful in tests)."""
    get_settings.cache_clear()
    return get_settings()
