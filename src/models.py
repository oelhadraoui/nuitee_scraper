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


def format_rooms_config(rooms_config: tuple[int, ...]) -> str:
    """``(2,)`` -> ``"2"``; ``(2, 2)`` -> ``"2+2"`` - readable in a CSV cell."""
    return "+".join(str(r) for r in rooms_config) or "2"


def compute_hidden_fees(
    final_price: Optional[float],
    display_price: Optional[float],
    nights: int,
) -> Optional[float]:
    """
    NOT used to populate the CSV (see ``PriceRecord.nuitee_local_fees``,
    which is scraped-or-null, never estimated - a prior "nuitee_hidden_fees"
    column used this function as a fallback when the real figure couldn't
    be scraped, but that estimate silently inherited any upstream price
    error and the column was removed rather than kept as a fallback).
    Retained as a documented, tested utility for ad-hoc/offline sanity
    cross-checks only.

    The true stay-level fees Nuitee's per-night listing price doesn't show.

    ``display_price`` is a PER-NIGHT rate; ``final_price`` is the TOTAL
    payable for the whole stay. A naive ``final_price - display_price``
    (what an earlier version of this pipeline computed) overstates the gap
    by roughly ``(nights - 1) * display_price`` for anything longer than a
    single night, since it's comparing a multi-night total against a single
    night's rate.

    Live-verified 2026-07: a Nuitee rate card reading "MAD 515 / night" for
    a 3-night stay led to a booking-page breakdown of "MAD 514.77 average
    per night x 3 nights = MAD 1,544.30" plus a single stay-level "Local
    fees: MAD 194.30" line, for a "Total: MAD 1,738.60". The correct
    comparison multiplies the per-night rate by nights BEFORE subtracting:
    ``1738.60 - (514.77 * 3) = 194.29`` - matching the site's own "Local
    fees" figure almost exactly, unlike the naive ``1738.60 - 514.77 =
    1223.83`` a straight subtraction would have reported.
    """
    if final_price is None or display_price is None or not nights:
        return None
    return round(final_price - (display_price * nights), 2)


def parse_rooms_config(text: str) -> tuple[int, ...]:
    """
    Inverse of :func:`format_rooms_config`.

    Also accepts the legacy bracketed/comma form (``"[2, 2]"``) so old
    exports and databases keep loading correctly.
    """
    cleaned = (text or "").strip().strip("[]")
    if not cleaned:
        return (2,)
    separator = "+" if "+" in cleaned else ","
    try:
        rooms = tuple(
            int(part.strip()) for part in cleaned.split(separator) if part.strip()
        )
    except ValueError:
        return (2,)
    return rooms or (2,)


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
    #: "Taxes and fees" figure read straight off THIS offer card's own text
    #: (e.g. "10 nights, 1 room (+MAD 149 taxes and fees)") - present on the
    #: detail page, before the checkout tunnel is even entered. Preferred
    #: over the checkout-page label scan (TunnelNavigator._extract_local_fees)
    #: because it is unambiguous: the amount sits right next to its own
    #: label with no risk of a nearby unrelated total being picked up instead.
    local_fees_hint: Optional[float] = None

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<RoomOffer #{self.index} {self.label[:32]!r} {self.display_price}>"


@dataclass(slots=True)
class TunnelResult:
    """
    Everything the checkout tunnel produced for one hotel.

    ``display_price`` is a PER-NIGHT rate (as shown on the Nuitee results
    card and the detail page's rate cards - live-verified 2026-07: a card
    reading "MAD 515 / night" for a 3-night stay led to a booking-page total
    of "MAD 1,738.60", built from "MAD 514.77 average per night x 3 = MAD
    1,544.30" plus a single stay-level "Local fees: MAD 194.30" line).
    ``final_price`` is the TOTAL for the whole stay. The two are NOT on the
    same basis - see :func:`compute_hidden_fees` for the correct comparison.
    """

    status: TunnelStatus = TunnelStatus.NOT_ATTEMPTED
    #: The hotel's IDs and location, read off the detail page's own API
    #: response while the tunnel walks it (see HotelIdentity).
    identity: Optional["HotelIdentity"] = None
    final_price: Optional[float] = None      # true payable total for the stay, MAD
    display_price: Optional[float] = None    # per-night listing price, MAD
    local_fees: Optional[float] = None       # "Local fees" line, READ off the
                                              # booking page directly - not
                                              # derived from final_price and
                                              # display_price. See
                                              # TunnelNavigator._extract_local_fees.
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
        """
        Naive gap between the stay total and the per-night listing price.

        NOT nights-adjusted - for a multi-night stay this is NOT the true
        fee amount (it also bundles in every extra night's room cost). Kept
        for diagnostics only. The CSV's "Nuitee Local Fees" column is the
        real figure - scraped directly off the booking page (``local_fees``
        above), never derived from this property.
        """
        if self.final_price is None or self.display_price is None:
            return None
        return round(self.final_price - self.display_price, 2)


@dataclass(slots=True)
class HotelIdentity:
    """
    Who a Nuitee hotel IS, independent of how its name is spelled.

    Nuitee runs on LiteAPI, whose hotel content is sourced from Booking.com.
    The detail page loads ``/v1/hotels/details/{id}``, and live-verified
    2026-11 on 9 hotels across 8 cities: its ``latitude``/``longitude``
    equal Booking's to 6 decimals (8/8), and its ``thirdPartyId`` IS the
    Booking.com hotel id (4/4 where present; it is empty for roughly half
    of hotels). That gives Booking matching two signals far stronger than a
    fuzzy name score: an exact id, and a distance.
    """

    nuitee_hotel_id: Optional[str] = None     # LiteAPI id, e.g. "lp67c32"
    booking_hotel_id: Optional[str] = None    # Booking.com id ("thirdPartyId")
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    address: Optional[str] = None
    stars: Optional[float] = None

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class MatchMethod(str, Enum):
    """How a Booking.com card was identified as the same hotel."""

    BOOKING_ID = "booking_id"   # card's Booking id == the hotel's known id
    GEO = "geo"                 # within geo_match_m, with a plausible name
    NAME = "name"               # fuzzy name score only (no usable ids/coords)


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
    #: How the card was matched (MatchMethod value), None when no match.
    match_method: Optional[str] = None
    #: The matched card's Booking.com hotel id, when the page exposed it.
    booking_hotel_id: Optional[str] = None
    #: Metres between the Nuitee hotel and the matched card, when both known.
    distance_m: Optional[float] = None


# ---------------------------------------------------------------------------
# National (Morocco) vs. International classification
# ---------------------------------------------------------------------------

#: Cities treated as domestic Morocco ("National") market vs. everything
#: else ("International"). Mirrors scripts/generate_input.py's "morocco"
#: preset city list exactly. Kept as its own constant here (rather than
#: importing that script) so scripts/generate_input.py stays a
#: zero-dependency standalone script that runs the same whether invoked on
#: the host (``make input``) or inside the container - if you add/remove a
#: Moroccan city in one place, update the other.
MOROCCO_CITIES: frozenset[str] = frozenset(
    c.lower()
    for c in (
        "Casablanca", "Marrakech", "Rabat", "Tangier", "Agadir", "Fes",
        "Essaouira", "Meknes", "Chefchaouen", "Ouarzazate", "Dakhla",
        "El Jadida", "Ifrane", "Oujda", "Tetouan", "Nador", "Saidia",
        "Merzouga", "Taghazout", "Asilah",
    )
)


def market_scope_for(city: str) -> str:
    """"National (Morocco)" for a Morocco city, else "International"."""
    if (city or "").strip().lower() in MOROCCO_CITIES:
        return "National (Morocco)"
    return "International"


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
    adults_per_room: str = "2"

    # --- Nuitee ------------------------------------------------------------
    nuitee_display_price: Optional[float] = None
    nuitee_final_price: Optional[float] = None
    #: The site's own "Local fees" / "taxes and fees" figure, scraped
    #: directly (TunnelNavigator._extract_local_fees / the room-card fee
    #: hint) - None when it couldn't be found, never an estimate. An
    #: earlier "nuitee_hidden_fees" column filled that gap with
    #: final_price - display_price*nights when scraping failed, but that
    #: estimate was unreliable (it silently inherited any upstream price
    #: error) and the column was removed rather than kept as a fallback.
    nuitee_local_fees: Optional[float] = None
    nuitee_room_label: Optional[str] = None
    tunnel_status: str = TunnelStatus.NOT_ATTEMPTED.value
    tunnel_deepest_panel: str = PanelKind.UNKNOWN.value
    tunnel_panels_visited: str = ""
    nuitee_url: Optional[str] = None
    nuitee_hotel_id: Optional[str] = None

    # --- Booking -----------------------------------------------------------
    booking_price: Optional[float] = None
    booking_matched_name: Optional[str] = None
    fuzzy_score: float = 0.0
    match_status: str = MatchStatus.SKIPPED.value
    booking_url: Optional[str] = None
    booking_hotel_id: Optional[str] = None
    match_method: Optional[str] = None
    match_distance_m: Optional[float] = None

    # --- derived -----------------------------------------------------------
    #: "National (Morocco)" or "International" - recomputed from `city`
    #: by compute_derived(), never set explicitly.
    market_scope: str = ""
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
        self.market_scope = market_scope_for(self.city)
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


#: Column order for the exported CSV - the user-facing view of a price row.
#: A handful of internal/operational fields (``horizon_offset_days``,
#: ``tunnel_deepest_panel``, ``run_id``, ``verified``) are tracked in the
#: SQLite store for diagnostics but deliberately left out of the export:
#: they're plumbing, not something a reader comparing prices needs.
#: ``booking_url`` stays last, matching the convention established by the
#: original deliverable.
CSV_COLUMNS: list[str] = [
    "hotel_name",
    "city",
    "market_scope",
    "checkin",
    "checkout",
    "nights",
    "horizon_label",
    "adults_per_room",
    "rooms",
    "nuitee_display_price",
    "nuitee_final_price",
    "nuitee_local_fees",
    "nuitee_room_label",
    "tunnel_status",
    "booking_price",
    "booking_matched_name",
    "fuzzy_score",
    "match_method",
    "match_distance_m",
    "price_difference",
    "price_difference_pct",
    "cheaper_platform",
    "currency",
    "match_status",
    "is_clean",
    "quality_flags",
    "scraped_at",
    "nuitee_hotel_id",
    "booking_hotel_id",
    "nuitee_url",
    "booking_url",
]

#: Human-friendly headers for the exported CSV.
CSV_HEADER_ALIASES: dict[str, str] = {
    "hotel_name": "Hotel Name",
    "city": "City",
    "market_scope": "Market Scope",
    "checkin": "Check-in",
    "checkout": "Check-out",
    "nights": "Nights",
    "horizon_label": "Offset",
    "adults_per_room": "Adults per Room",
    "rooms": "Rooms",
    "nuitee_display_price": "Nuitee Display Price (MAD, per night)",
    "nuitee_final_price": "Nuitee Final Payable (MAD, whole stay)",
    "nuitee_local_fees": "Nuitee Local Fees (MAD, scraped)",
    "nuitee_room_label": "Nuitee Room",
    "tunnel_status": "Tunnel Status",
    "booking_price": "Booking Price (MAD, whole stay)",
    "booking_matched_name": "Booking Matched Name",
    "fuzzy_score": "Fuzzy Score",
    "match_method": "Match Method",
    "match_distance_m": "Match Distance (m)",
    "price_difference": "Price Difference (MAD)",
    "price_difference_pct": "Price Difference (%)",
    "cheaper_platform": "Cheaper Platform",
    "currency": "Currency",
    "match_status": "Match Status",
    "is_clean": "Clean",
    "quality_flags": "Quality Flags",
    "scraped_at": "Scraped At",
    "nuitee_hotel_id": "Nuitee Hotel ID",
    "booking_hotel_id": "Booking Hotel ID",
    "nuitee_url": "Nuitee URL",
    "booking_url": "Booking URL",
}
