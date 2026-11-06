"""
src/utils/hotel_ids.py
=============================================================================
Hotel identity: matching Nuitee hotels to Booking.com by id and location
instead of by name alone.

Why this works (live-verified 2026-11, 9 hotels, 8 cities)
----------------------------------------------------------
* Nuitee is a LiteAPI white-label, and LiteAPI's hotel content comes from
  Booking.com. Every Nuitee detail page (``/hotels/{id}``) loads
  ``/v1/hotels/details/{id}``, whose ``hotel.thirdPartyId`` equalled the
  Booking.com hotel id in 4/4 hotels where it was set (empty for ~half),
  and whose ``latitude``/``longitude`` equalled Booking's to 6 decimals in
  8/8.
* Booking's ``searchresults.html?dest_id={id}&dest_type=hotel`` puts that
  exact property first (3/3), with no autocomplete/destination resolution.
* Booking's results page embeds a JSON payload with every listed property's
  ``basicPropertyData`` (``id``, ``pageName``, ``location.latitude/
  longitude``). ``pageName`` is the slug in the card's own link
  (``/hotel/es/benetusser.html`` -> ``benetusser``), which ties each visible
  card to its id and coordinates without any extra request.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.models import HotelIdentity
from src.utils.logging_setup import get_logger

log = get_logger(__name__)

__all__ = [
    "BookingProperty",
    "HotelIdMap",
    "IdentityCapture",
    "PROPERTY_INDEX_JS",
    "booking_slug_from_url",
    "haversine_m",
    "identity_for",
    "nuitee_id_from_url",
    "parse_nuitee_details",
]

_NUITEE_ID_RE = re.compile(r"/hotels/([A-Za-z0-9_-]+)")
_DETAILS_PATH = "/v1/hotels/details/"
# /hotel/<cc>/<slug>.html, optionally with a language infix (.en-gb.html).
_BOOKING_SLUG_RE = re.compile(
    r"/hotel/[a-z]{2}/([^/?#]+?)(?:\.[a-z]{2}(?:-[a-z]{2})?)?\.html", re.IGNORECASE
)


def nuitee_id_from_url(url: Optional[str]) -> Optional[str]:
    """``https://x.nuitee.link/hotels/lp67c32?...`` -> ``"lp67c32"``."""
    if not url:
        return None
    match = _NUITEE_ID_RE.search(url)
    return match.group(1) if match else None


def booking_slug_from_url(url: Optional[str]) -> Optional[str]:
    """``https://www.booking.com/hotel/pt/inatel-albufeira.html?..`` -> slug."""
    if not url:
        return None
    match = _BOOKING_SLUG_RE.search(url)
    return match.group(1).lower() if match else None


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_id(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text if text and text.lower() not in ("none", "null", "0") else None


def identity_for(
    identity: Optional[HotelIdentity], *urls: Optional[str]
) -> Optional[HotelIdentity]:
    """
    ``identity`` with its Nuitee id filled from the first URL that has one;
    a bare-id identity when the tunnel captured nothing; None if no id at all.
    """
    url_id = next((i for i in (nuitee_id_from_url(u) for u in urls) if i), None)
    if identity is None:
        return HotelIdentity(nuitee_hotel_id=url_id) if url_id else None
    if not identity.nuitee_hotel_id and url_id:
        identity.nuitee_hotel_id = url_id
    return identity


def parse_nuitee_details(
    payload: Any, fallback_id: Optional[str] = None
) -> HotelIdentity:
    """Build a HotelIdentity from a ``/v1/hotels/details/{id}`` body."""
    hotel = payload.get("hotel") if isinstance(payload, dict) else None
    if not isinstance(hotel, dict):
        return HotelIdentity(nuitee_hotel_id=fallback_id)

    lat, lng = _as_float(hotel.get("latitude")), _as_float(hotel.get("longitude"))
    if lat is not None and lng is not None and lat == 0 and lng == 0:
        lat = lng = None  # "null island" placeholder, not a location

    return HotelIdentity(
        nuitee_hotel_id=_as_id(hotel.get("id")) or fallback_id,
        booking_hotel_id=_as_id(hotel.get("thirdPartyId")),
        latitude=lat,
        longitude=lng,
        address=(hotel.get("address") or None),
        stars=_as_float(hotel.get("stars")),
    )


# ---------------------------------------------------------------------------
# Nuitee side: read the identity off the page's own API traffic
# ---------------------------------------------------------------------------

class IdentityCapture:
    """
    Listens on a page for the ``/v1/hotels/details/`` response the SPA
    fetches by itself, so the identity costs no extra request.

    Usage::

        capture = IdentityCapture(page, detail_url)
        ...navigate / walk the tunnel...
        identity = await capture.result()
    """

    def __init__(self, page: Any, detail_url: Optional[str]) -> None:
        self._page = page
        self._fallback_id = nuitee_id_from_url(detail_url)
        self._task: Optional[asyncio.Task] = None
        try:
            page.on("response", self._on_response)
        except Exception:
            self._page = None  # not a real page (tests) - id from URL only

    def _on_response(self, response: Any) -> None:
        if self._task is not None:
            return
        try:
            url, ok = response.url, response.ok
        except Exception:
            return
        if _DETAILS_PATH not in url or not ok:
            return
        if self._fallback_id and f"{_DETAILS_PATH}{self._fallback_id}" not in url:
            return
        self._task = asyncio.ensure_future(self._read(response))

    async def _read(self, response: Any) -> HotelIdentity:
        return parse_nuitee_details(await response.json(), self._fallback_id)

    async def result(self, timeout: float = 5.0) -> HotelIdentity:
        if self._page is not None:
            try:
                self._page.remove_listener("response", self._on_response)
            except Exception:
                pass
        if self._task is not None:
            try:
                return await asyncio.wait_for(self._task, timeout=timeout)
            except Exception as exc:
                log.debug("hotel details response unreadable: %s", exc)
        return HotelIdentity(nuitee_hotel_id=self._fallback_id)


# ---------------------------------------------------------------------------
# Booking side: the embedded per-property data
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BookingProperty:
    booking_id: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None


#: Collects ``{pageName: {id, lat, lng}}`` from every JSON script on a
#: Booking results page that carries ``basicPropertyData``.
PROPERTY_INDEX_JS = """
() => {
  const out = {};
  for (const s of document.querySelectorAll('script[type="application/json"]')) {
    const t = s.textContent;
    if (!t || !t.includes('basicPropertyData')) continue;
    let data;
    try { data = JSON.parse(t); } catch (e) { continue; }
    const stack = [data];
    while (stack.length) {
      const o = stack.pop();
      if (Array.isArray(o)) {
        for (const x of o) if (x && typeof x === 'object') stack.push(x);
        continue;
      }
      const b = o.basicPropertyData;
      if (b && b.pageName && b.id != null) {
        const l = b.location || {};
        out[String(b.pageName).toLowerCase()] = {
          id: String(b.id), lat: l.latitude ?? null, lng: l.longitude ?? null,
        };
      }
      for (const k in o) {
        const v = o[k];
        if (v && typeof v === 'object') stack.push(v);
      }
    }
  }
  return out;
}
"""


def parse_property_index(raw: Any) -> dict[str, BookingProperty]:
    """Turn PROPERTY_INDEX_JS's result into ``{slug: BookingProperty}``."""
    out: dict[str, BookingProperty] = {}
    if not isinstance(raw, dict):
        return out
    for slug, data in raw.items():
        if not isinstance(data, dict):
            continue
        booking_id = _as_id(data.get("id"))
        if booking_id:
            out[str(slug).lower()] = BookingProperty(
                booking_id, _as_float(data.get("lat")), _as_float(data.get("lng"))
            )
    return out


# ---------------------------------------------------------------------------
# Learned id pairs
# ---------------------------------------------------------------------------

class HotelIdMap:
    """
    Nuitee hotel id -> Booking hotel id, learned from confirmed matches and
    kept in a small JSON file next to the store.

    Every hotel recurs in several windows (2 offsets x 2 stay durations), so
    one confirmed match lets every later lookup for it go straight to the
    exact Booking property - including hotels whose ``thirdPartyId`` is
    empty. Only id- or distance-confirmed matches are learned; a name-only
    match is never written here, so a fuzzy mistake can't spread.
    """

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self._pairs: dict[str, str] = {}
        if path is not None and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._pairs = {str(k): str(v) for k, v in data.items() if k and v}
            except (OSError, ValueError, AttributeError) as exc:
                log.warning("Ignoring unreadable %s (%s)", path, exc)

    def __len__(self) -> int:
        return len(self._pairs)

    def get(self, nuitee_hotel_id: Optional[str]) -> Optional[str]:
        return self._pairs.get(nuitee_hotel_id) if nuitee_hotel_id else None

    def learn(self, nuitee_hotel_id: Optional[str], booking_hotel_id: Optional[str]) -> None:
        if not nuitee_hotel_id or not booking_hotel_id:
            return
        if self._pairs.get(nuitee_hotel_id) == booking_hotel_id:
            return
        self._pairs[nuitee_hotel_id] = booking_hotel_id
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._pairs, indent=0, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Could not save %s (%s)", self.path, exc)
