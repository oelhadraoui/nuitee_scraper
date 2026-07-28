"""
src/utils/pricing.py
=============================================================================
Price parsing and currency normalisation.

Two distinct problems are solved here.

1. **Extraction from noisy DOM text.** A Booking.com property card contains
   review counts ("1,204 reviews"), scores ("8.7"), distances ("2.3 km") and
   sometimes several prices (struck-through original + discounted). Naive
   regex scraping produces silent garbage, which is precisely the failure mode
   that poisons a 100k-row dataset.

2. **Currency normalisation to MAD.** The tunnel and Booking may render EUR,
   USD or GBP depending on the session. Rates are read from the environment so
   they can be refreshed without a code change, and every conversion is
   recorded so a row can be audited later.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Currency handling
# ---------------------------------------------------------------------------

#: Symbol / ISO-code -> canonical ISO code.
CURRENCY_TOKENS: dict[str, str] = {
    "mad": "MAD", "dh": "MAD", "dhs": "MAD", "dirham": "MAD", "درهم": "MAD",
    "eur": "EUR", "€": "EUR", "euro": "EUR", "euros": "EUR",
    "usd": "USD", "$": "USD", "us$": "USD", "dollar": "USD",
    "gbp": "GBP", "£": "GBP", "pound": "GBP",
    "chf": "CHF", "aed": "AED", "sar": "SAR", "cad": "CAD", "aud": "AUD",
}

#: Fallback conversion rates -> MAD. Override via ``FX_RATE_<CODE>`` env vars.
_DEFAULT_FX_TO_MAD: dict[str, float] = {
    "MAD": 1.0,
    "EUR": 10.90,
    "USD": 10.05,
    "GBP": 12.75,
    "CHF": 11.40,
    "AED": 2.74,
    "SAR": 2.68,
    "CAD": 7.30,
    "AUD": 6.55,
}


def get_fx_rate(currency: str) -> Optional[float]:
    """
    Conversion rate from ``currency`` to MAD.

    Environment override wins: ``FX_RATE_EUR=11.02`` beats the built-in table.
    Returns ``None`` for an unknown currency so the caller can flag the row
    rather than silently inventing a number.
    """
    code = (currency or "").strip().upper()
    if not code:
        return None
    env_value = os.getenv(f"FX_RATE_{code}")
    if env_value:
        try:
            rate = float(env_value)
            if rate > 0:
                return rate
        except ValueError:
            pass
    return _DEFAULT_FX_TO_MAD.get(code)


def detect_currency(text: str, default: str = "MAD") -> str:
    """Identify the currency mentioned in a price string."""
    if not text:
        return default
    lowered = text.lower()
    # Longest tokens first so "us$" wins over "$".
    for token in sorted(CURRENCY_TOKENS, key=len, reverse=True):
        if token in lowered:
            return CURRENCY_TOKENS[token]
    return default


@dataclass(slots=True)
class ParsedPrice:
    """A price with full provenance, so any row can be audited."""

    amount_mad: Optional[float]
    raw_amount: Optional[float]
    currency: str
    converted: bool
    source_text: str

    @property
    def ok(self) -> bool:
        return self.amount_mad is not None


# ---------------------------------------------------------------------------
# Numeric extraction
# ---------------------------------------------------------------------------

#: Patterns whose numbers are never prices.
_NOISE_PATTERNS = [
    r"\d[\d,.\s]*\s*(?:reviews?|avis|ratings?|notes?|commentaires?)",
    r"\d[\d,.]*\s*(?:stars?|etoiles?|étoiles?)",
    r"\d[\d,.]*\s*(?:km|m|miles?|mi)\b",
    r"\b\d\s*(?:nights?|nuits?|adults?|adultes?|guests?|rooms?|chambres?)\b",
    r"\b\d{1,2}[:h]\d{2}\b",              # times: 14:00, 14h00
    r"\b\d{4}-\d{2}-\d{2}\b",             # ISO dates
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",       # slash dates
    r"\b\d\.\d\b",                        # lone review scores: 8.7, 4.5
]

#: Separators that are ALWAYS thousands separators, never decimal points.
#: Plain ASCII space is deliberately excluded - deleting it would merge two
#: unrelated numbers ("890 1250" -> "8901250"), which is exactly how silent
#: garbage enters the dataset. It is handled by guarded patterns instead.
_INVISIBLE_SEPARATORS = (
    "\xa0",    # no-break space
    "\u202f",  # narrow no-break space
    "\u2009",  # thin space
    "\u2007",  # figure space
    "\u2060",  # word joiner
    "'",       # Swiss/German grouping apostrophe
)

_NUMBER_RE = re.compile(r"\d+(?:\.\d{1,2})?")

#: "1 234,56" / "1 234.56" - space grouping followed by a decimal part.
_SPACE_GROUP_DECIMAL_RE = re.compile(r"(\d) (\d{3})(?=[.,]\d{1,2}(?!\d))")

#: "1 234 MAD" / "1 234" at end of string - space grouping followed by a
#: currency token or the end of the text, so the merge is unambiguous.
_CURRENCY_SUFFIX = (
    r"(?:\s*(?:MAD|DH|DHS|EUR|USD|GBP|CHF|AED|SAR|CAD|AUD|"
    r"\u20ac|\$|\u00a3)\b|\s*$)"
)
_SPACE_GROUP_SUFFIX_RE = re.compile(
    r"(\d) (\d{3})(?=" + _CURRENCY_SUFFIX + r")", re.IGNORECASE
)


def _strip_noise(text: str) -> str:
    cleaned = text
    for pattern in _NOISE_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    return cleaned


def _merge_space_thousands(text: str) -> str:
    """
    Collapse space-grouped thousands only where it is provably safe.

    Applied repeatedly so "1 234 567,89" folds fully.
    """
    previous = None
    current = text
    for _ in range(4):
        if current == previous:
            break
        previous = current
        current = _SPACE_GROUP_DECIMAL_RE.sub(r"\1\2", current)
        current = _SPACE_GROUP_SUFFIX_RE.sub(r"\1\2", current)
    return current


def _normalise_separators(text: str) -> str:
    """
    Turn locale-specific number formatting into plain ``1234.56``.

    Handles: ``1 234,56`` (fr) / ``1,234.56`` (en) / ``1.234,56`` (de) /
    ``1\xa0234`` (nbsp-grouped) / ``1'234`` (ch).
    """
    cleaned = text
    for char in _INVISIBLE_SEPARATORS:
        cleaned = cleaned.replace(char, "")

    cleaned = _merge_space_thousands(cleaned)

    # "1.234,56" -> "1234,56"  (dot thousands)
    cleaned = re.sub(r"(\d)\.(\d{3})(?=\D|$)", r"\1\2", cleaned)
    # "1,234" -> "1234"  (comma thousands) - must run before comma-decimal
    cleaned = re.sub(r"(\d),(\d{3})(?!\d)", r"\1\2", cleaned)
    # "1234,56" -> "1234.56"  (comma decimal)
    cleaned = re.sub(r"(\d),(\d{1,2})(?!\d)", r"\1.\2", cleaned)
    return cleaned


def extract_amounts(text: str, min_value: float = 0.0) -> list[float]:
    """All plausible numeric amounts in ``text``, noise removed, in order."""
    if not text:
        return []
    cleaned = _normalise_separators(_strip_noise(text))
    amounts: list[float] = []
    for match in _NUMBER_RE.finditer(cleaned):
        try:
            value = float(match.group())
        except ValueError:
            continue
        if value >= min_value:
            amounts.append(value)
    return amounts


def parse_price(
    text: str,
    *,
    min_value: float = 150.0,
    max_value: float = 200_000.0,
    default_currency: str = "MAD",
    strategy: str = "max",
) -> ParsedPrice:
    """
    Parse a price out of raw DOM text and normalise it to MAD.

    Parameters
    ----------
    strategy:
        ``"max"``   - largest candidate. Correct for a results card, where the
                      total dominates per-night fragments.
        ``"min"``   - smallest candidate. Correct when hunting the cheapest
                      room among several offers.
        ``"first"`` - first candidate in document order. Correct for a checkout
                      summary panel, where the total is the headline figure.

    Notes
    -----
    ``min_value`` is expressed in the *source* currency to avoid rejecting a
    legitimate 95 EUR (~1,035 MAD) room. It is therefore scaled down by the FX
    rate before the filter is applied.
    """
    if not text:
        return ParsedPrice(None, None, default_currency, False, "")

    currency = detect_currency(text, default_currency)
    rate = get_fx_rate(currency)

    # Scale the acceptance floor into the source currency.
    source_min = min_value / rate if rate and rate > 0 else min_value

    candidates = extract_amounts(text, min_value=source_min)
    if not candidates:
        return ParsedPrice(None, None, currency, False, text[:200])

    if strategy == "min":
        raw = min(candidates)
    elif strategy == "first":
        raw = candidates[0]
    else:
        raw = max(candidates)

    if rate is None:
        # Unknown currency: surface the raw amount but refuse to fake MAD.
        return ParsedPrice(None, raw, currency, False, text[:200])

    amount_mad = round(raw * rate, 2)
    if not (min_value <= amount_mad <= max_value):
        return ParsedPrice(None, raw, currency, rate != 1.0, text[:200])

    return ParsedPrice(amount_mad, raw, currency, rate != 1.0, text[:200])


def to_mad(amount: float, currency: str) -> Optional[float]:
    """Convert a known amount into MAD, or ``None`` if the currency is unknown."""
    rate = get_fx_rate(currency)
    if rate is None:
        return None
    return round(amount * rate, 2)


def format_mad(value: Optional[float]) -> str:
    """Render a price for logs / CSV; empty string when missing."""
    return "" if value is None else f"{value:.2f}"
