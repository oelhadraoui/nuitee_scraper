"""
Identity-based Booking matching (src/utils/hotel_ids.py + BookingScraper).

The payload shapes below are trimmed copies of what the live sites returned
on 2026-11-04: Nuitee's ``/v1/hotels/details/lp67c32`` (INATEL Albufeira,
thirdPartyId 171596 == Booking's b_hotel_id 171596, identical coordinates)
and Booking's embedded ``basicPropertyData`` entries.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from src.config import Settings
from src.models import (
    BookingResult,
    HotelIdentity,
    MatchMethod,
    MatchStatus,
    PriceRecord,
    TunnelStatus,
)
from src.pipeline.horizon_generator import HorizonGenerator
from src.pipeline.storage import PriceStore
from src.scrapers.booking import BookingScraper
from src.utils.hotel_ids import (
    BookingProperty,
    HotelIdMap,
    IdentityCapture,
    booking_slug_from_url,
    haversine_m,
    identity_for,
    nuitee_id_from_url,
    parse_nuitee_details,
    parse_property_index,
)
from src.utils.selectors import SelectorRegistry
from src.utils.text import normalise_key

INATEL = {
    "hotel": {
        "id": "lp67c32", "name": "INATEL Albufeira Hotel", "city": "Albufeira",
        "latitude": 37.087193, "longitude": -8.242745,
        "address": "Av. Infante D.Henrique", "stars": 3, "thirdPartyId": 171596,
    }
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestParsing:
    def test_nuitee_details(self) -> None:
        identity = parse_nuitee_details(INATEL)
        assert identity.nuitee_hotel_id == "lp67c32"
        assert identity.booking_hotel_id == "171596"
        assert (identity.latitude, identity.longitude) == (37.087193, -8.242745)
        assert identity.address == "Av. Infante D.Henrique"
        assert identity.stars == 3.0

    def test_missing_third_party_id_and_null_island(self) -> None:
        payload = {"hotel": {"id": "lpx", "thirdPartyId": None, "latitude": 0, "longitude": 0}}
        identity = parse_nuitee_details(payload)
        assert identity.booking_hotel_id is None
        assert not identity.has_location

    def test_unexpected_payload_keeps_the_url_id(self) -> None:
        assert parse_nuitee_details({"error": 1}, "lp1").nuitee_hotel_id == "lp1"

    def test_ids_from_urls(self) -> None:
        assert nuitee_id_from_url(
            "https://a.nuitee.link/hotels/lp655b3e86?checkin=2026-11-09"
        ) == "lp655b3e86"
        assert nuitee_id_from_url("https://a.nuitee.link/booking?x=1") is None
        assert booking_slug_from_url(
            "https://www.booking.com/hotel/pt/inatel-albufeira.html?aid=1"
        ) == "inatel-albufeira"
        assert booking_slug_from_url(
            "/hotel/ma/suite-casa-diamond.en-gb.html"
        ) == "suite-casa-diamond"
        assert booking_slug_from_url("https://www.booking.com/searchresults.html") is None

    def test_haversine(self) -> None:
        assert haversine_m(37.087193, -8.242745, 37.087193, -8.242745) == 0
        # Casablanca -> Rabat is ~87 km as the crow flies.
        assert 80_000 < haversine_m(33.5731, -7.5898, 34.0209, -6.8416) < 95_000

    def test_property_index(self) -> None:
        raw = {
            "Benetusser": {"id": "247840", "lat": 39.42496, "lng": -0.39829},
            "no-id": {"id": None, "lat": 1, "lng": 2},
            "bad": "x",
        }
        assert parse_property_index(raw) == {
            "benetusser": BookingProperty("247840", 39.42496, -0.39829)
        }

    def test_identity_for(self) -> None:
        assert identity_for(None, None, "https://x/hotels/lp9").nuitee_hotel_id == "lp9"
        assert identity_for(None, "https://x/booking") is None
        captured = HotelIdentity(booking_hotel_id="1")
        assert identity_for(captured, "https://x/hotels/lp9").nuitee_hotel_id == "lp9"


class TestHotelIdMap:
    def test_learn_persist_reload(self, tmp_path: Path) -> None:
        path = tmp_path / "hotel_id_map.json"
        ids = HotelIdMap(path)
        ids.learn("lp67c32", "171596")
        ids.learn(None, "1")
        ids.learn("lp2", None)

        reloaded = HotelIdMap(path)
        assert reloaded.get("lp67c32") == "171596"
        assert len(reloaded) == 1

    def test_corrupt_file_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "hotel_id_map.json"
        path.write_text("{not json", encoding="utf-8")
        assert len(HotelIdMap(path)) == 0

    def test_disabled_map_never_writes(self, tmp_path: Path) -> None:
        ids = HotelIdMap(None)
        ids.learn("a", "b")
        assert ids.get("a") == "b"
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Nuitee side: reading the page's own API response
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, url: str, payload, ok: bool = True) -> None:
        self.url, self.ok, self._payload = url, ok, payload

    async def json(self):
        return self._payload


class _Page:
    def __init__(self) -> None:
        self.handlers = []

    def on(self, _event, handler) -> None:
        self.handlers.append(handler)

    def remove_listener(self, _event, handler) -> None:
        self.handlers.remove(handler)

    def emit(self, response) -> None:
        for handler in list(self.handlers):
            handler(response)


class TestIdentityCapture:
    async def test_reads_the_details_response(self) -> None:
        page = _Page()
        capture = IdentityCapture(page, "https://a.nuitee.link/hotels/lp67c32?x=1")
        page.emit(_Response("https://a.nuitee.link/v1/hotels/rooms/lp67c32", {}))
        page.emit(_Response("https://a.nuitee.link/v1/hotels/details/lp67c32?language=en", INATEL))

        identity = await capture.result()

        assert identity.booking_hotel_id == "171596"
        assert page.handlers == []

    async def test_another_hotels_details_are_ignored(self) -> None:
        page = _Page()
        capture = IdentityCapture(page, "https://a.nuitee.link/hotels/lp1")
        page.emit(_Response("https://a.nuitee.link/v1/hotels/details/lp67c32", INATEL))

        identity = await capture.result()

        assert identity.nuitee_hotel_id == "lp1"
        assert identity.booking_hotel_id is None

    async def test_non_page_falls_back_to_the_url_id(self) -> None:
        identity = await IdentityCapture(object(), "https://x/hotels/lp5").result()
        assert identity.nuitee_hotel_id == "lp5"


# ---------------------------------------------------------------------------
# Booking side: candidate selection
# ---------------------------------------------------------------------------

class _Card:
    def __init__(self, name: str, slug: str) -> None:
        self.name, self.slug = name, slug


class _Link:
    def __init__(self, href: str) -> None:
        self.href = href

    async def get_attribute(self, _name):
        return self.href


class _Cards:
    def __init__(self, cards) -> None:
        self.cards = cards

    def nth(self, index):
        return self.cards[index]


class _Cascade:
    def __init__(self, cards=None) -> None:
        self._cards = cards

    async def all_matching(self, _page, timeout=0):
        return _Cards(self._cards), len(self._cards)

    async def text(self, card, timeout=0):
        return card.name

    async def first_attached(self, card, timeout=0):
        return _Link(f"https://www.booking.com/hotel/pt/{card.slug}.html?aid=1")


class _Registry:
    def __init__(self, cards) -> None:
        self._cascades = {
            "booking.property_card": _Cascade(cards),
            "booking.card_name": _Cascade(),
            "booking.card_link": _Cascade(),
        }

    def get(self, path):
        return self._cascades[path]

    get_optional = get


HERE = (37.087193, -8.242745)
NEAR = (37.087300, -8.242700)      # ~12 m
ACROSS_TOWN = (37.120000, -8.242745)  # ~3.6 km


def _prop(booking_id, at):
    return BookingProperty(booking_id, at[0], at[1])


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.storage.output_dir = tmp_path
    return s


@pytest.fixture
def window():
    return HorizonGenerator(Settings().horizon).build_plan(
        ["Albufeira"], reference_date=date(2026, 11, 4), only="short", durations=[3]
    ).windows[0]


def _scraper(settings, cards):
    scraper = BookingScraper(
        manager=None, settings=settings,
        registry=SelectorRegistry.load(settings.selectors_path),
    )
    scraper.registry = _Registry(cards)
    return scraper


class TestBestMatch:
    async def test_booking_id_beats_a_better_name(self, settings, window) -> None:
        cards = [_Card("INATEL Albufeira Hotel", "decoy"), _Card("Hotel INATEL", "real")]
        props = {"decoy": _prop("999", ACROSS_TOWN), "real": _prop("171596", HERE)}
        identity = HotelIdentity("lp67c32", "171596", *HERE)

        best = await _scraper(settings, cards)._best_match(
            None, "INATEL Albufeira Hotel", window, identity=identity, properties=props
        )

        assert (best.index, best.method, best.booking_id) == (1, MatchMethod.BOOKING_ID, "171596")

    async def test_location_accepts_a_renamed_hotel(self, settings, window) -> None:
        expected = "Hotel Sol e Mar Albufeira - Adults Only"
        cards = [_Card("SolMar Albufeira Beachfront", "sol-e-mar")]  # scores 75
        props = {"sol-e-mar": _prop("38603", NEAR)}
        scraper = _scraper(settings, cards)

        best = await scraper._best_match(
            None, expected, window,
            identity=HotelIdentity("lp3bf09", None, *HERE), properties=props,
        )

        assert best.method is MatchMethod.GEO
        assert best.distance_m < 20
        threshold = scraper._threshold_for(expected, window.city)
        assert best.score < threshold     # name alone would have failed
        assert best.accepted(threshold)

    async def test_same_name_across_town_is_rejected(self, settings, window) -> None:
        cards = [_Card("Holiday Inn Express", "far")]
        props = {"far": _prop("1", ACROSS_TOWN)}

        best = await _scraper(settings, cards)._best_match(
            None, "Holiday Inn Express", window,
            identity=HotelIdentity("lp1", None, *HERE), properties=props,
        )

        assert best is None

    async def test_id_contradicted_by_location_is_not_trusted(self, settings, window) -> None:
        cards = [_Card("Totally Other Hotel", "x")]
        props = {"x": _prop("171596", ACROSS_TOWN)}

        best = await _scraper(settings, cards)._best_match(
            None, "INATEL Albufeira Hotel", window,
            identity=HotelIdentity("lp67c32", "171596", *HERE), properties=props,
        )

        assert best is None

    async def test_without_identity_it_is_the_original_name_match(self, settings, window) -> None:
        cards = [_Card("Some Other Hotel", "a"), _Card("INATEL Albufeira Hotel", "b")]

        best = await _scraper(settings, cards)._best_match(
            None, "INATEL Albufeira Hotel", window
        )

        assert (best.index, best.method, best.booking_id) == (1, MatchMethod.NAME, None)

    async def test_by_id_scan_only_returns_the_id_card(self, settings, window) -> None:
        cards = [_Card("INATEL Albufeira Hotel", "a")]
        props = {"a": _prop("1", HERE)}

        best = await _scraper(settings, cards)._best_match(
            None, "INATEL Albufeira Hotel", window,
            identity=HotelIdentity("lp67c32", "171596", *HERE),
            properties=props, scan_all=True,
        )

        assert best is None


class TestLookupFlow:
    async def test_by_id_first_then_name_fallback(self, settings, window, monkeypatch) -> None:
        scraper = _scraper(settings, [])
        calls = []

        async def search(_window, _name, identity, *, by_id):
            calls.append((by_id, identity.booking_hotel_id if identity else None))
            return None if by_id else BookingResult(status=MatchStatus.MATCH_FAIL)

        monkeypatch.setattr(scraper, "_search", search)
        await scraper._lookup_once(window, "X", HotelIdentity("lp1", "171596"))

        assert calls == [(True, "171596"), (False, "171596")]

    async def test_learned_id_is_used_when_nuitee_has_none(self, settings, window, monkeypatch) -> None:
        scraper = _scraper(settings, [])
        scraper._ids.learn("lp1", "555")
        calls = []

        async def search(_window, _name, identity, *, by_id):
            calls.append((by_id, identity.booking_hotel_id))
            return BookingResult(status=MatchStatus.OK)

        monkeypatch.setattr(scraper, "_search", search)
        await scraper._lookup_once(window, "X", HotelIdentity("lp1", None))

        assert calls == [(True, "555")]
        assert HotelIdMap(settings.storage.id_map_path).get("lp1") == "555"

    async def test_ids_disabled_means_name_only(self, settings, window, monkeypatch) -> None:
        settings.booking.use_hotel_ids = False
        scraper = _scraper(settings, [])
        calls = []

        async def search(_window, _name, identity, *, by_id):
            calls.append((by_id, identity))
            return BookingResult(status=MatchStatus.OK)

        monkeypatch.setattr(scraper, "_search", search)
        await scraper._lookup_once(window, "X", HotelIdentity("lp1", "171596"))

        assert calls == [(False, None)]

    def test_search_url_by_id(self, settings, window) -> None:
        url = _scraper(settings, [])._build_search_url(window, "INATEL", booking_hotel_id="171596")
        assert "dest_id=171596" in url and "dest_type=hotel" in url
        assert "ss=" not in url
        assert "selected_currency=MAD" in url


class TestStorage:
    async def test_identity_columns_survive_a_csv_roundtrip(self, settings) -> None:
        record = PriceRecord(
            hotel_name="INATEL Albufeira Hotel",
            hotel_name_normalised=normalise_key("INATEL Albufeira Hotel"),
            city="Albufeira", checkin="2026-11-11", checkout="2026-11-14",
            horizon_label="1W", horizon_offset_days=7, execution_date="2026-11-04",
            nuitee_final_price=1200.0, booking_price=1350.0, fuzzy_score=100.0,
            match_status=MatchStatus.OK.value, tunnel_status=TunnelStatus.OK.value,
            nuitee_hotel_id="lp67c32", booking_hotel_id="171596",
            match_method=MatchMethod.BOOKING_ID.value, match_distance_m=0.0,
        ).compute_derived()
        store = PriceStore(settings)
        await store.start_run({})
        await store.add(record)
        await store.flush()

        reloaded = PriceStore(settings)
        await asyncio.to_thread(reloaded.init_schema)
        [row] = await reloaded.fetch("1=1", order_by="")

        assert row["nuitee_hotel_id"] == "lp67c32"
        assert row["booking_hotel_id"] == "171596"
        assert row["match_method"] == "booking_id"
        assert row["match_distance_m"] == 0.0
