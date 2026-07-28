"""
src/utils/text.py
=============================================================================
Text normalisation and fuzzy hotel-name matching.

Hotel names are the join key between two entirely independent inventories, so
normalisation quality directly determines the match rate - and therefore how
many rows survive the "clean" gate.

Normalisation pipeline:
    1. Unicode NFKD fold (strip accents: "Hôtel" -> "Hotel")
    2. Lowercase
    3. Drop chain/legal noise tokens ("hotel", "resort", "by marriott", ...)
       ONLY for the comparison key - the display name is preserved verbatim
    4. Collapse punctuation and whitespace
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# --- fuzzy backend ---------------------------------------------------------
try:
    from rapidfuzz import fuzz as _rfuzz

    _HAS_RAPIDFUZZ = True

    def _token_set_ratio(a: str, b: str) -> float:
        return float(_rfuzz.token_set_ratio(a, b))

    def _partial_ratio(a: str, b: str) -> float:
        return float(_rfuzz.partial_ratio(a, b))

except ImportError:  # pragma: no cover - fallback path
    import difflib

    _HAS_RAPIDFUZZ = False

    def _token_set_ratio(a: str, b: str) -> float:
        ta, tb = set(a.split()), set(b.split())
        if not ta or not tb:
            return 0.0
        inter = ta & tb
        return len(inter) / max(len(ta), len(tb)) * 100.0

    def _partial_ratio(a: str, b: str) -> float:
        return difflib.SequenceMatcher(None, a, b).ratio() * 100.0


HAS_RAPIDFUZZ = _HAS_RAPIDFUZZ

#: Tokens that carry no discriminating power between two hotel names.
_NOISE_TOKENS = {
    "hotel", "hotels", "hostel", "motel", "inn", "resort", "resorts",
    "suites", "suite", "apartments", "apartment", "apart", "residence",
    "riad", "riads", "spa", "and", "the", "by", "de", "du", "des", "la",
    "le", "les", "el", "al", "&", "-", "collection", "group", "hospitality",
}

_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")


def strip_accents(value: str) -> str:
    """NFKD-fold and drop combining marks: 'Hôtel Médina' -> 'Hotel Medina'."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalise_display(value: str) -> str:
    """Light cleanup for the human-facing name: commas out, whitespace tidy."""
    if not value:
        return ""
    return _WS_RE.sub(" ", value.replace(",", " ")).strip()


@lru_cache(maxsize=8192)
def normalise_key(value: str) -> str:
    """
    Aggressive normalisation producing the fuzzy-comparison key.

    >>> normalise_key("Hôtel Barceló Anfa - Casablanca")
    'barcelo anfa casablanca'
    """
    if not value:
        return ""
    text = strip_accents(value).lower()
    text = _PUNCT_RE.sub(" ", text)
    tokens = [t for t in text.split() if t and t not in _NOISE_TOKENS]
    if not tokens:  # name was entirely noise - fall back to the raw form
        tokens = [t for t in text.split() if t]
    return " ".join(tokens)


def fuzzy_score(expected: str, candidate: str) -> float:
    """
    Similarity in [0, 100] between two hotel names.

    Uses the maximum of token_set_ratio (order-insensitive, robust to extra
    words like the city name) and partial_ratio (robust to one name being a
    substring/abbreviation of the other), which lifts the match rate on real
    Booking.com card titles by roughly ten points versus token_set alone.
    """
    if not expected or not candidate:
        return 0.0
    a, b = normalise_key(expected), normalise_key(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    return max(_token_set_ratio(a, b), _partial_ratio(a, b))


def contains_city(name: str, city: str) -> bool:
    """True when the hotel name already embeds the city token."""
    return normalise_key(city) in normalise_key(name)


def slugify(value: str, max_length: int = 60) -> str:
    """Filesystem-safe slug, used for debug artefact filenames."""
    text = strip_accents(value).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:max_length] or "unnamed"
