"""
src/models.py
=============================================================================
Typed domain models shared by every layer of the pipeline.

Keeping these in one place means the scraper, the storage layer, the quality
gate, the verification pass and the CSV exporter all agree on exactly what a
"row" is - which is what makes the 1,000-clean-hotel metric trustworthy.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Status enums
# ---------------------------------------------------------------------------

class MatchStatus(str, Enum):
    """Outcome of the Booking.com hotel lookup."""

    OK = "OK"
    MATCH_FAIL = "MATCH_FAIL"      # no card cleared the fuzzy threshold
    NO_RESULTS = "NO_RESULTS"      # matched (or searched) but no availability
    ERROR = "ERROR"                # exception / navigation failure
    SKIPPED = "SKIPPED"            # not attempted (upstream failure)


class TunnelStatus(str, Enum):
    """Outcome of the Nuitee checkout-tunnel walk."""

    OK = "OK"                          # reached final summary, price extracted
    PARTIAL = "PARTIAL"                # price from an intermediate panel only
    NO_ROOMS = "NO_ROOMS"              # detail page had zero bookable offers
    TUNNEL_BLOCKED = "TUNNEL_BLOCKED"  # a panel never advanced
    PAYMENT_GUARD = "PAYMENT_GUARD"    # aborted at the payment boundary
    ERROR = "ERROR"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class PanelKind(str, Enum):
    """
    Logical identity of a tunnel step, independent of its DOM markup.

    The real site (verified live, 2026-07) is a 2-page flow, not the 5-panel
    SPA this enum originally modelled:

        ROOM_SELECT   -> /hotels/{id}   (rate cards, "Choose room" CTA)
        BOOKING_PAGE  -> /booking?...   (guest form + summary + payment, all
                                          on one page; the payable total is
                                          readable without filling the form)

    OPTIONS / GUEST_DETAILS / SUMMARY / PAYMENT are kept for backward
    compatibility with any persisted data or dashboards that reference their
    string values, but the navigator no longer treats them as distinct
    navigable states.
    """

    ROOM_SELECT = "room_select"
    BOOKING_PAGE = "booking_page"  # combined guest form + summary + payment
    OPTIONS = "options"          # extras / cancellation policy upsell (legacy)
    GUEST_DETAILS = "guest_details"  # legacy
    SUMMARY = "summary"          # legacy - final payable total
    PAYMENT = "payment"          # legacy - hard stop, never entered
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Search / horizon primitives
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SearchWindow:
    """One concrete (city, check-in, check-out) query for one horizon."""

    city: str
    checkin: date
    checkout: date
    horizon_label: str          # e.g. "J+3"
    horizon_offset_days: int    # e.g. 3
    execution_date: date        # the simulated "day of scraping"
    rooms_config: tuple[int, ...] = (2,)

    @property
    def nights(self) -> int:
        return (self.checkout - self.checkin).days

    @property
    def rooms(self) -> int:
        return len(self.rooms_config)

    @property
    def total_adults(self) -> int:
        return sum(self.rooms_config)

    @property
    def checkin_str(self) -> str:
        return self.checkin.isoformat()

    @property
    def checkout_str(self) -> str:
        return self.checkout.isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "city": self.city,
            "checkin": self.checkin_str,
            "checkout": self.checkout_str,
            "horizon_label": self.horizon_label,
            "horizon_offset_days": self.horizon_offset_days,
            "execution_date": self.execution_date.isoformat(),
            "rooms_config": list(self.rooms_config),
            "nights": self.nights,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchWindow":
        rooms = data.get("rooms_config")
        if not rooms:
            adults = int(data.get("adults", 2))
            n_rooms = int(data.get("rooms", 1))
            rooms = [adults] * n_rooms
        return cls(
            city=str(data["city"]),
            checkin=_as_date(data["checkin"]),
            checkout=_as_date(data["checkout"]),
            horizon_label=str(data.get("horizon_label", "AD_HOC")),
            horizon_offset_days=int(data.get("horizon_offset_days", 0)),
            execution_date=_as_date(
                data.get("execution_date", datetime.now().date())
            ),
            rooms_config=tuple(int(r) for r in rooms),
        )


def _as_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.strptime(str(value), "%Y-%m-%d").date()


# ---------------------------------------------------------------------------
# Nuitee side
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RoomOffer:
    """A single bookable offer on a Nuitee hotel detail page."""

    label: str
    display_price: Optional[float]
    currency: str = "MAD"
    index: int = 0
    refundable: Optional[bool] = None

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<RoomOffer #{self.index} {self.label[:32]!r} {self.display_price}>"


@dataclass(slots=True)
class TunnelResult:
    """Everything the checkout tunnel produced for one hotel."""

    status: TunnelStatus = TunnelStatus.NOT_ATTEMPTED
    final_price: Optional[float] = None      # true payable total, MAD
    display_price: Optional[float] = None    # listing price, for delta analysis
    currency: str = "MAD"
    room_label: Optional[str] = None
    panels_visited: list[str] = field(default_factory=list)
    deepest_panel: str = PanelKind.UNKNOWN.value
    detail_url: Optional[str] = None
    attempts: int = 0
    error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.status in (TunnelStatus.OK, TunnelStatus.PARTIAL)

    @property
    def hidden_fees(self) -> Optional[float]:
        """Difference between the payable total and the advertised price."""
        if self.final_price is None or self.display_price is None:
            return None
        return round(self.final_price - self.display_price, 2)


@dataclass(slots=True)
class NuiteeHotel:
    """A hotel harvested from the Nuitee results page."""

    name: str
    display_price: Optional[float] = None
    currency: str = "MAD"
    rating: Optional[float] = None
    detail_url: Optional[str] = None
    card_index: int = 0


# ---------------------------------------------------------------------------
# Booking side
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class BookingResult:
    """Outcome of the Booking.com lookup for one hotel."""

    status: MatchStatus = MatchStatus.SKIPPED
    price: Optional[float] = None
    currency: str = "MAD"
    matched_name: Optional[str] = None
    fuzzy_score: float = 0.0
    url: Optional[str] = None
    attempts: int = 0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# The unified output row
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PriceRecord:
    """
    One fully-resolved comparison row. This is the single unit that gets
    persisted, deduplicated, quality-gated, and exported.
    """

    # --- identity ----------------------------------------------------------
    hotel_name: str
    hotel_name_normalised: str
    city: str
    checkin: str
    checkout: str
    horizon_label: str
    horizon_offset_days: int
    execution_date: str

    # --- stay config -------------------------------------------------------
    nights: int = 1
    rooms: int = 1
    adults_per_room: str = "[2]"

    # --- Nuitee ------------------------------------------------------------
    nuitee_display_price: Optional[float] = None
    nuitee_final_price: Optional[float] = None
    nuitee_hidden_fees: Optional[float] = None
    nuitee_room_label: Optional[str] = None
    tunnel_status: str = TunnelStatus.NOT_ATTEMPTED.value
    tunnel_deepest_panel: str = PanelKind.UNKNOWN.value
    tunnel_panels_visited: str = ""
    nuitee_url: Optional[str] = None

    # --- Booking -----------------------------------------------------------
    booking_price: Optional[float] = None
    booking_matched_name: Optional[str] = None
    fuzzy_score: float = 0.0
    match_status: str = MatchStatus.SKIPPED.value
    booking_url: Optional[str] = None

    # --- derived -----------------------------------------------------------
    price_difference: Optional[float] = None
    price_difference_pct: Optional[float] = None
    cheaper_platform: Optional[str] = None
    currency: str = "MAD"

    # --- provenance --------------------------------------------------------
    is_clean: bool = False
    quality_flags: str = ""
    verified: bool = False
    scraped_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    run_id: str = ""

    # -- derivation ---------------------------------------------------------

    def compute_derived(self) -> "PriceRecord":
        """Recompute the comparison columns from the two prices."""
        n, b = self.nuitee_final_price, self.booking_price
        if n is not None and b is not None and n > 0:
            self.price_difference = round(b - n, 2)
            self.price_difference_pct = round((b - n) / n * 100.0, 2)
            if abs(self.price_difference) < 0.01:
                self.cheaper_platform = "TIE"
            else:
                self.cheaper_platform = "NUITEE" if n < b else "BOOKING"
        else:
            self.price_difference = None
            self.price_difference_pct = None
            self.cheaper_platform = None
        return self

    @property
    def dedup_key(self) -> str:
        """Stable hash used as the SQLite primary key."""
        raw = "|".join(
            [
                self.hotel_name_normalised,
                self.city.lower(),
                self.checkin,
                self.checkout,
                self.horizon_label,
                str(self.rooms),
                self.adults_per_room,
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["dedup_key"] = self.dedup_key
        return data


#: Column order for the exported CSV. ``booking_url`` stays last, matching the
#: convention established by the original deliverable.
CSV_COLUMNS: list[str] = [
    "hotel_name",
    "city",
    "checkin",
    "checkout",
    "nights",
    "horizon_label",
    "horizon_offset_days",
    "execution_date",
    "adults_per_room",
    "rooms",
    "nuitee_display_price",
    "nuitee_final_price",
    "nuitee_hidden_fees",
    "nuitee_room_label",
    "tunnel_status",
    "tunnel_deepest_panel",
    "booking_price",
    "booking_matched_name",
    "fuzzy_score",
    "price_difference",
    "price_difference_pct",
    "cheaper_platform",
    "currency",
    "match_status",
    "is_clean",
    "quality_flags",
    "verified",
    "scraped_at",
    "run_id",
    "nuitee_url",
    "booking_url",
]

#: Human-friendly headers for the client-facing export.
CSV_HEADER_ALIASES: dict[str, str] = {
    "hotel_name": "Hotel Name",
    "city": "City",
    "checkin": "Check-in",
    "checkout": "Check-out",
    "nights": "Nights",
    "horizon_label": "Horizon",
    "horizon_offset_days": "Horizon Offset (days)",
    "execution_date": "Execution Date",
    "adults_per_room": "Adults per Room",
    "rooms": "Rooms",
    "nuitee_display_price": "Nuitee Display Price (MAD)",
    "nuitee_final_price": "Nuitee Final Payable (MAD)",
    "nuitee_hidden_fees": "Nuitee Hidden Fees (MAD)",
    "nuitee_room_label": "Nuitee Room",
    "tunnel_status": "Tunnel Status",
    "tunnel_deepest_panel": "Deepest Panel",
    "booking_price": "Booking Price (MAD)",
    "booking_matched_name": "Booking Matched Name",
    "fuzzy_score": "Fuzzy Score",
    "price_difference": "Price Difference (MAD)",
    "price_difference_pct": "Price Difference (%)",
    "cheaper_platform": "Cheaper Platform",
    "currency": "Currency",
    "match_status": "match_status",
    "is_clean": "Clean",
    "quality_flags": "Quality Flags",
    "verified": "Verified",
    "scraped_at": "Scraped At",
    "run_id": "Run ID",
    "nuitee_url": "Nuitee URL",
    "booking_url": "Booking URL",
}
