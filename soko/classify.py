"""What a listing actually is, in our category language.

Every median, every seller count and every saturation figure is an aggregate
over a set of listings that this module decided belong together. Get it wrong
and the number is confidently precise about the wrong thing, which is the
worst failure this product has, because it is invisible.

Rules, not a model. The reasoning is in config/taxonomy.yaml: a wrong tag is
fixed by editing one line, by somebody who is not an engineer, and the same
input always produces the same output. Reproducibility is the thing a vendor
is paying for.
"""

from __future__ import annotations

import functools
import re
from typing import Any

from soko.normalize import clean_title, taxonomy


@functools.lru_cache(maxsize=1)
def _compiled() -> list[tuple[str, re.Pattern[str]]]:
    """(category_code, pattern) for every category in the taxonomy.

    One alternation per category rather than one per keyword, so a title is
    scanned a dozen times instead of several hundred. Keywords are sorted
    longest first so "power bank" wins over a bare "bank" if both were ever
    present in one category.

    Word boundaries are load bearing. Without them "bale" matches "balearic"
    and "l" in the volume pattern matches every word containing an l.
    """
    compiled = []
    for entry in taxonomy()["categories"]:
        keywords = sorted(entry["keywords"], key=len, reverse=True)
        alternation = "|".join(re.escape(k) for k in keywords)
        compiled.append((entry["code"], re.compile(rf"\b({alternation})\b", re.IGNORECASE)))
    return compiled


@functools.lru_cache(maxsize=1)
def _service_pattern() -> re.Pattern[str]:
    markers = taxonomy().get("service_markers", [])
    alternation = "|".join(re.escape(m) for m in markers)
    return re.compile(rf"\b({alternation})\b", re.IGNORECASE)


@functools.lru_cache(maxsize=1)
def category_index() -> dict[str, dict[str, Any]]:
    """Category metadata by code, including the published commission rate."""
    return {entry["code"]: entry for entry in taxonomy()["categories"]}


@functools.lru_cache(maxsize=1)
def _accessory_markers() -> list[tuple[re.Pattern[str], str]]:
    """(pattern, owning category) for each unambiguous accessory marker.

    Longest marker first, so "charging cable" is tested before "charger" and
    the more specific reading wins.

    Both ends are word-bounded. Without a trailing boundary "charger" matches
    inside "chargers" harmlessly but also inside words like "supercharged",
    and a marker that fires on the wrong word silently moves a median.
    """
    markers = taxonomy().get("accessory_markers") or {}
    ordered = sorted(markers.items(), key=lambda kv: len(kv[0]), reverse=True)
    return [
        # Trailing \w* permits the plural and nothing else meaningful, while
        # the leading \b stops a match mid-word.
        (re.compile(rf"\b{re.escape(word)}s?\b", re.IGNORECASE), owner)
        for word, owner in ordered
    ]


@functools.lru_cache(maxsize=1)
def _qualified_markers() -> list[tuple[re.Pattern[str], re.Pattern[str], str]]:
    """(marker, required-context, owner) for markers that need corroboration.

    "case for" is not enough to place a listing: a case can be for a phone, a
    guitar, a camera or a laptop. Review found that assuming phone put guitar
    cases and sofa covers into the phone accessories median.

    So these fire only when the title ALSO names something the accessory
    plausibly belongs to. The ambiguity is resolved by evidence in the title
    rather than by an assumption in the code.
    """
    compiled = []
    for entry in taxonomy().get("qualified_accessory_markers") or []:
        marker = re.compile(rf"\b{re.escape(entry['marker'])}\b", re.IGNORECASE)
        alternation = "|".join(re.escape(w) for w in entry["requires"])
        context = re.compile(rf"\b({alternation})\b", re.IGNORECASE)
        compiled.append((marker, context, entry["owner"]))
    return compiled


@functools.lru_cache(maxsize=1)
def _guard_exempt() -> frozenset[str]:
    return frozenset(taxonomy().get("accessory_guard_exempt") or [])


@functools.lru_cache(maxsize=1)
def _condition_overrides() -> frozenset[str]:
    """Categories describing condition, which beat product type.

    Thrift is the case: a bale of second hand dresses is priced as thrift and
    not as dresses, and those two medians are far apart.
    """
    return frozenset(taxonomy().get("condition_overrides") or [])


def _apply_accessory_guard(
    winner: str | None,
    blob: str,
    winner_hits: int = 0,
) -> tuple[str | None, bool]:
    """Redirect an accessory that matched the category of the thing it fits.

    "Universal Charging Plug For Kids Toy Cars" matches the toys keyword
    "kids toy", because that phrase appears inside "Kids Toy Cars". It is a
    charger. Left alone it puts a 2,499 shilling charger into the toys median.

    Keyword matching alone cannot separate an item from an accessory for that
    item, so this is a second, explicit pass.

    Three restraints, all of which review showed were necessary. The first
    draft of this guard had none of them and classified a sofa cover and a
    guitar case as phone accessories, which is exactly the corrupt-median
    failure the product exists to prevent:

      1. Exempt categories are never redirected. A category whose main
         product IS the accessory would otherwise be redirected onto itself
         or away from a correct answer.

      2. A confident keyword winner is not overridden. Two or more keyword
         hits means the title said what it was more than once, and a single
         accessory word should not outvote that. "Dog Bed Washable Cover" is
         a pet product that happens to mention a cover.

      3. Ambiguous markers need corroboration. "case for" only redirects when
         the title also names a phone brand, because a case is equally likely
         to be for a guitar.
    """
    if winner is not None and winner in _guard_exempt():
        return winner, False

    # An unambiguous marker: a charger is a charger whatever it charges.
    for pattern, owner in _accessory_markers():
        if pattern.search(blob):
            if owner == winner:
                return winner, False
            # Do not overturn a title that named its category repeatedly.
            if winner is not None and winner_hits >= 2:
                return winner, False
            return owner, True

    # An ambiguous marker, which fires only with corroborating context.
    for marker, context, owner in _qualified_markers():
        if marker.search(blob) and context.search(blob):
            if owner == winner:
                return winner, False
            if winner is not None and winner_hits >= 2:
                return winner, False
            return owner, True

    return winner, False


def classify(title: str | None, breadcrumb: str | None = None) -> dict[str, Any]:
    """The category one listing belongs to.

    Returns the winning category, every category that matched, how many
    keywords fired, and a confidence band. The full match list is kept rather
    than discarded so that a listing matching two categories is diagnosable
    later: a "phone case power bank combo" is a real listing and somebody will
    eventually ask why it landed where it did.

    A breadcrumb from the platform's own navigation is a stronger signal than
    a seller written title, so it is scanned too, but it never overrides a
    title match on its own. Platform taxonomies are broad, and "Electronics"
    tells us much less than the word "earbuds" in the title does.
    """
    cleaned = clean_title(title)
    if not cleaned and not breadcrumb:
        return _unclassified()

    blob = " ".join(part for part in (cleaned, (breadcrumb or "").lower()) if part)

    matches: list[tuple[str, int]] = []
    for code, pattern in _compiled():
        found = pattern.findall(blob)
        if found:
            matches.append((code, len(found)))

    if not matches:
        # No keyword matched. Before giving up, check whether the title names
        # an accessory outright: "12V Charger For Kids Ride On Car Battery"
        # matches no category keyword but is unambiguously a charger.
        #
        # This is not a fallback to a default category, which the module
        # deliberately does not have. It is a positive identification from a
        # different rule, and it only fires when an accessory marker is
        # actually present.
        rescued, found = _apply_accessory_guard(None, blob)
        if found:
            return {
                "category_code": rescued,
                "all_matches": [],
                "hit_count": 1,
                "is_service": False,
                "confidence": "medium",
                "classified_by": "accessory_guard",
            }

        # Deliberately no fallback to a default category. An unclassified
        # listing is excluded from every aggregate, which is correct: a
        # listing we cannot identify contributes nothing but noise to a
        # median, and a wrong bucket is worse than a smaller sample.
        return _unclassified()

    # Most keyword hits wins. On a tie, the longer category code is arbitrary
    # but deterministic, which is the property that matters: the same listing
    # must not move between categories from one run to the next.
    matches.sort(key=lambda pair: (-pair[1], pair[0]))
    winner, hit_count = matches[0]

    # A category describing CONDITION beats one describing product type,
    # however many keywords the latter matched. A thrift listing names its
    # garments repeatedly and its condition once, so hit counting alone put
    # wholesale thrift bales into the retail womenswear median, where the
    # prices are nothing alike. See condition_overrides in taxonomy.yaml.
    for code, hits in matches:
        if code in _condition_overrides():
            winner, hit_count = code, hits
            break

    # Second pass: an accessory FOR a thing is not the thing. See
    # _apply_accessory_guard for the live failure that made this necessary.
    winner, redirected = _apply_accessory_guard(winner, blob, hit_count)

    is_service = bool(_service_pattern().search(blob))
    contested = len(matches) > 1 and matches[0][1] == matches[1][1]

    confidence = _confidence(hit_count, contested, is_service)
    if redirected:
        # A redirect is a correction, not a confident classification. Cap it
        # at medium so it still aggregates but never claims high confidence
        # on the strength of a keyword that matched the wrong product.
        confidence = "low" if is_service else "medium"

    return {
        "category_code": winner,
        "all_matches": [code for code, _ in matches],
        "hit_count": hit_count,
        "is_service": is_service,
        "confidence": confidence,
        "classified_by": "accessory_guard" if redirected else "keyword",
    }


def _unclassified() -> dict[str, Any]:
    return {
        "category_code": None,
        "all_matches": [],
        "hit_count": 0,
        "is_service": False,
        "confidence": "none",
        "classified_by": None,
    }


def _confidence(hit_count: int, contested: bool, is_service: bool) -> str:
    """How much weight an aggregate should give this classification.

    Only 'high' and 'medium' listings enter a published median. 'low' and
    'none' are collected and stored, because a better rule tomorrow can
    reclassify them from stored data without re-crawling, but they do not get
    to move a number a vendor reads today.
    """
    if is_service:
        return "low"
    if contested:
        return "medium"
    return "high" if hit_count >= 2 else "medium"


# Confidence bands admitted into published aggregates.
AGGREGATABLE = frozenset({"high", "medium"})


def is_aggregatable(result: dict[str, Any]) -> bool:
    """Whether this listing may contribute to a published figure."""
    return (
        result.get("category_code") is not None
        and result.get("confidence") in AGGREGATABLE
        and not result.get("is_service", False)
    )


def commission_rate(category_code: str | None) -> float | None:
    """The published commission for a category, for the margin calculator.

    Returns None rather than a default when we do not have the rate. A margin
    computed against a guessed commission is a wrong answer about somebody's
    money, and the product must say it does not know instead.
    """
    if not category_code:
        return None
    entry = category_index().get(category_code)
    if not entry:
        return None
    rate = entry.get("commission_rate")
    return float(rate) if rate is not None else None
