"""Price and title normalisation.

The correctness of this module decides whether the whole product works,
because every figure SokoScout quotes is an aggregate over what happens here.
A price parsed wrong does not raise an error anywhere. It just quietly moves
the median, and a vendor commits stock against it.

Two rules govern everything below:

  1. Return None rather than a best guess. A missing price is a smaller
     problem than a wrong one, because a missing price is visible in the
     observation count and a wrong price is not.
  2. Never silently mangle. "1.5k" is 1500 or it is nothing; it is never 1.5.

See config/taxonomy.yaml for the price vocabulary this implements.
"""

from __future__ import annotations

import functools
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

# Below this, a "price" on a Kenyan marketplace is a data error, not a bargain.
# Nothing physical ships for one shilling; a 1 is a parse artefact from a
# rating, a page number or a stripped thousands separator.
MIN_PLAUSIBLE_KES = Decimal("10")

# Above this, it is a car, a plot of land, or a decimal point that went
# missing. Neither belongs in a phone accessory median.
MAX_PLAUSIBLE_KES = Decimal("10000000")


@functools.lru_cache(maxsize=1)
def taxonomy() -> dict[str, Any]:
    with open(CONFIG_DIR / "taxonomy.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------

# Matches the number itself, with optional thousands separators and decimals,
# plus an optional k/K suffix. The currency prefix is stripped before this runs
# rather than being part of the pattern, because listings put it before the
# number, after the number, or nowhere at all.
_NUMBER = re.compile(
    r"""
    (?<![\d.])            # not mid-number
    (\d{1,3}(?:,\d{3})+   # 1,299 or 1,299,000  (grouped form)
    |\d+(?:\.\d+)?)       # 1299 or 1.5         (plain or decimal form)
    \s*([kK])?            # optional thousands suffix
    (?![\d])
    """,
    re.VERBOSE,
)

_CURRENCY_NOISE = re.compile(
    r"(ksh?s?|kes|/=|/-|shillings?|bob)\.?",
    re.IGNORECASE,
)


def parse_price(raw: str | None) -> Decimal | None:
    """One price string to a Decimal in Kenyan shillings.

    Handles the forms that actually appear on these marketplaces:

        "KSh 1,299"      -> 1299
        "Ksh1299"        -> 1299
        "1,299/="        -> 1299
        "1.5k"           -> 1500      not 1.5
        "2500 bob"       -> 2500
        "KSh 1,299 - KSh 2,400"  -> 1299, the low end of a range

    Returns None for anything it cannot read confidently, including
    implausible values. Decimal rather than float throughout: these figures
    are money, they get aggregated, and binary floating point drift in a
    median that a vendor prices against is not acceptable.
    """
    if not raw:
        return None

    text = str(raw).strip()
    if not text:
        return None

    # A range means two prices. Take the low end, which is the conservative
    # read for a vendor working out whether they can compete on price.
    for separator in taxonomy()["price_forms"]["range_separators"]:
        if separator in text:
            text = text.split(separator, 1)[0]
            break

    text = _CURRENCY_NOISE.sub(" ", text)

    match = _NUMBER.search(text)
    if not match:
        return None

    digits, suffix = match.group(1), match.group(2)

    # A grouped number and a decimal number look alike until you count the
    # digits after the separator. "1,299" is one thousand two hundred and
    # ninety nine; "1.299" in a Kenyan listing is the same number written by
    # somebody using a European separator, not one and a bit shillings.
    digits = digits.replace(",", "")

    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None

    if suffix:
        value *= 1000

    # A price with a fractional part below one shilling is a parse artefact.
    # Kenyan marketplaces do not price in cents.
    value = value.quantize(Decimal("1")) if value == value.to_integral_value() else value

    if value < MIN_PLAUSIBLE_KES or value > MAX_PLAUSIBLE_KES:
        return None

    return value


def parse_price_range(raw: str | None) -> tuple[Decimal, Decimal] | None:
    """Both ends of a category level price range.

    Kilimall gives us category pages rather than product pages, so a range is
    the most precise thing available there. Returning both ends honestly is
    better than returning a midpoint that looks like a product median and is
    not one.
    """
    if not raw:
        return None

    text = str(raw).strip()
    for separator in taxonomy()["price_forms"]["range_separators"]:
        if separator in text:
            low_raw, high_raw = text.split(separator, 1)
            low, high = parse_price(low_raw), parse_price(high_raw)
            if low is None or high is None:
                return None
            return (low, high) if low <= high else (high, low)

    single = parse_price(text)
    return (single, single) if single is not None else None


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

# Marketplace titles are padded with terms that carry no product identity.
# They defeat both classification and any future title-based deduplication.
_TITLE_NOISE = re.compile(
    r"\b(brand new|new arrival|hot sale|best seller|bestseller|free shipping"
    r"|fast delivery|original|genuine|high quality|top quality|wholesale"
    r"|in stock|limited offer|special offer|discount|promo|offer)\b",
    re.IGNORECASE,
)

_BRACKETED = re.compile(r"[\[\(]([^\]\)]*)[\]\)]")
_WHITESPACE = re.compile(r"\s+")


def clean_title(raw: str | None) -> str | None:
    """A listing title reduced to the words that identify the product.

    "BRAND NEW Original 20000mAh Power Bank Fast Charging [FREE SHIPPING]"
    becomes "20000mah power bank fast charging".

    Bracketed segments go entirely: on these marketplaces they are almost
    always promotional rather than descriptive.
    """
    if not raw:
        return None

    text = _BRACKETED.sub(" ", str(raw))
    text = _TITLE_NOISE.sub(" ", text)
    text = text.replace("|", " ").replace("_", " ")
    text = _WHITESPACE.sub(" ", text).strip(" -,.|").lower()

    return text or None


# Capacity, size and count are the specification that separates a 10000mAh
# power bank from a 30000mAh one. They are the difference between two
# legitimately different prices, so they are extracted rather than discarded.
_SPEC_PATTERNS = {
    "capacity_mah": re.compile(r"(\d{3,6})\s*mah\b", re.IGNORECASE),
    "storage_gb": re.compile(r"(\d{1,4})\s*gb\b", re.IGNORECASE),
    "memory_gb": re.compile(r"(\d{1,3})\s*gb\s*ram\b", re.IGNORECASE),
    "screen_inch": re.compile(r"(\d{1,3}(?:\.\d)?)\s*(?:inch|inches|\")", re.IGNORECASE),
    "volume_litre": re.compile(r"(\d{1,3}(?:\.\d)?)\s*(?:litre|liter|l)\b", re.IGNORECASE),
    "weight_kg": re.compile(r"(\d{1,3}(?:\.\d)?)\s*kg\b", re.IGNORECASE),
    "pack_count": re.compile(r"\b(\d{1,3})\s*(?:pcs|pieces|pack)\b", re.IGNORECASE),
}


def extract_specs(title: str | None) -> dict[str, float]:
    """Numeric specifications from a title.

    Two power banks at wildly different prices are usually two different
    capacities rather than one seller being expensive. Without this, that
    difference reads as price dispersion and inflates the saturation figure.
    """
    if not title:
        return {}

    specs: dict[str, float] = {}
    for key, pattern in _SPEC_PATTERNS.items():
        match = pattern.search(title)
        if match:
            try:
                specs[key] = float(match.group(1))
            except ValueError:
                continue
    return specs


def normalise_sku(platform_code: str, raw_key: str | None) -> str | None:
    """A stable per platform product key.

    This is what makes a rerun idempotent: the same product on the same day
    must produce the same key, or the price series grows a duplicate arm and
    the median drifts toward whichever product got counted twice.
    """
    if not raw_key:
        return None
    key = str(raw_key).strip().lower()
    key = re.sub(r"[^a-z0-9]+", "-", key).strip("-")
    if not key:
        return None
    return f"{platform_code}:{key}"
