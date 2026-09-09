"""Aggregation. Every number the product states is computed here.

This module exists so that no model ever computes a figure. The answer engine
selects which question to ask and how to phrase the result; the arithmetic
happens in this file and in SQL, where it can be checked, tested, and
reproduced exactly.

Two properties are non negotiable:

  Every figure carries its evidence. A median is returned together with the
  number of observations behind it, how many distinct sellers, and when they
  were collected. A figure without its evidence is not a thing this module can
  emit, because they are built by the same function call.

  Below a minimum observation count, it refuses. A median over eleven
  listings is not a market rate, and presenting it as one is how a vendor
  commits stock against a number that never meant anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Sequence

# The refusal threshold. Below this many observations the product says the
# data is too thin rather than producing a number.
#
# Thirty is a judgement, not a derivation, and it is deliberately on the
# conservative side: the cost of refusing an answerable question is a mildly
# annoyed vendor, and the cost of answering an unanswerable one is a vendor
# who bought stock against noise.
MIN_OBSERVATIONS = 30

# Distinct sellers required before a saturation figure means anything. One
# seller with 400 listings is not a saturated category, it is one shop.
MIN_SELLERS_FOR_SATURATION = 3


@dataclass(frozen=True)
class Evidence:
    """What a figure rests on. Travels with every number, always."""

    observation_count: int
    seller_count: int
    platform_codes: tuple[str, ...]
    collected_on: date
    # Set when the platform gives us category level rather than product level
    # data, so the product can say so next to the number instead of implying a
    # precision it does not have.
    caveats: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "observation_count": self.observation_count,
            "seller_count": self.seller_count,
            "platforms": list(self.platform_codes),
            "collected_on": self.collected_on.isoformat(),
            "caveats": list(self.caveats),
        }


@dataclass(frozen=True)
class PriceSummary:
    """A category's price picture on one day, with its evidence."""

    median: Decimal
    p25: Decimal
    p75: Decimal
    minimum: Decimal
    maximum: Decimal
    evidence: Evidence

    @property
    def spread(self) -> Decimal:
        """Interquartile range. How much sellers disagree about the price."""
        return self.p75 - self.p25

    def as_dict(self) -> dict[str, Any]:
        return {
            "median": str(self.median),
            "p25": str(self.p25),
            "p75": str(self.p75),
            "min": str(self.minimum),
            "max": str(self.maximum),
            "evidence": self.evidence.as_dict(),
        }


class InsufficientData(Exception):
    """Too few observations to state a figure.

    Carries the count so the product can say "we have 11 observations, we need
    30" rather than an unexplained refusal. A vendor told why will come back;
    a vendor told "no" will not.
    """

    def __init__(self, have: int, need: int, context: str = "") -> None:
        self.have = have
        self.need = need
        super().__init__(
            f"{context}: {have} observations, need at least {need}. "
            f"Refusing rather than stating a figure this thin."
        )


def _quantile(sorted_values: Sequence[Decimal], q: float) -> Decimal:
    """Linear interpolated quantile.

    Written out rather than taken from statistics.quantiles because these are
    Decimals and must stay Decimals. Converting money to float to compute a
    median and back again is exactly the kind of quiet drift this product
    cannot have.
    """
    if not sorted_values:
        raise ValueError("no values")
    if len(sorted_values) == 1:
        return sorted_values[0]

    position = Decimal(str(q)) * (len(sorted_values) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    fraction = position - lower_index

    lower = sorted_values[lower_index]
    upper = sorted_values[upper_index]
    value = lower + (upper - lower) * fraction

    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def summarise_prices(
    prices: Sequence[Decimal],
    sellers: Sequence[str | None],
    platform_codes: Sequence[str],
    collected_on: date,
    caveats: Sequence[str] = (),
    minimum: int = MIN_OBSERVATIONS,
    context: str = "price summary",
) -> PriceSummary:
    """The price picture for a set of observations.

    Raises InsufficientData below the threshold rather than returning a
    figure. This is the refusal the proposal describes, implemented as a
    control flow that cannot be forgotten: there is no code path that returns
    a PriceSummary without having passed the check.
    """
    clean = sorted(p for p in prices if p is not None)
    if len(clean) < minimum:
        raise InsufficientData(len(clean), minimum, context)

    distinct_sellers = {s for s in sellers if s}

    return PriceSummary(
        median=_quantile(clean, 0.5),
        p25=_quantile(clean, 0.25),
        p75=_quantile(clean, 0.75),
        minimum=clean[0],
        maximum=clean[-1],
        evidence=Evidence(
            observation_count=len(clean),
            seller_count=len(distinct_sellers),
            platform_codes=tuple(sorted(set(platform_codes))),
            collected_on=collected_on,
            caveats=tuple(caveats),
        ),
    )


def saturation_index(
    listing_count: int,
    seller_count: int,
    price_spread: Decimal,
    median_price: Decimal,
) -> float | None:
    """How crowded a category is.

    Listings per distinct seller, weighted by how tightly sellers have
    converged on a price. The second term is what distinguishes a large
    category from a crowded one: forty sellers with wide price dispersion are
    still finding the market, while forty sellers all within a few shillings
    of each other have already competed the margin away, and that is the
    situation a vendor needs warning about.

    Returns None below the seller floor rather than a number. One seller with
    four hundred listings is not a saturated category, it is one shop, and an
    index that says otherwise is worse than no index.
    """
    if seller_count < MIN_SELLERS_FOR_SATURATION or listing_count <= 0:
        return None
    if median_price <= 0:
        return None

    listings_per_seller = listing_count / seller_count

    # Relative spread: how wide the interquartile range is against the median.
    # Near zero means everybody charges the same, which is the crowded case.
    relative_spread = float(price_spread / median_price)

    # Convergence rises as spread falls. Clamped so a single strange listing
    # with an extreme price cannot drive the index negative.
    convergence = max(0.0, 1.0 - min(relative_spread, 1.0))

    return round(listings_per_seller * (1.0 + convergence), 3)


def saturation_label(index: float | None) -> str:
    """The index as a rep or a vendor reads it.

    Bands rather than a decimal, because a vendor acts on this in a second and
    a number would only invite an argument about the threshold. The bands are
    informed guesses and are expected to move once there is real outcome data
    to fit them against.
    """
    if index is None:
        return "unknown"
    if index >= 6.0:
        return "crowded"
    if index >= 3.0:
        return "competitive"
    return "room"


@dataclass
class TrendPoint:
    observed_on: date
    median: Decimal
    observation_count: int


def price_trend(points: Sequence[TrendPoint], minimum_points: int = 3) -> dict[str, Any]:
    """Direction of travel over a series of daily medians.

    This is the answer nobody else can give, because it requires having been
    collecting. A competitor starting today matches our snapshot within a
    week and cannot obtain last quarter at any price.

    Returns a refusal dict rather than raising, because a thin trend alongside
    a good current median is a partial answer worth giving: the product can
    state today's price and say the history is not yet deep enough.
    """
    usable = [p for p in points if p.observation_count >= 1]
    usable.sort(key=lambda p: p.observed_on)

    if len(usable) < minimum_points:
        return {
            "available": False,
            "reason": (
                f"{len(usable)} days of history, need at least {minimum_points}. "
                f"The series starts the day collection starts and deepens from there."
            ),
            "points": len(usable),
        }

    first, last = usable[0], usable[-1]
    change = last.median - first.median
    percent = float(change / first.median * 100) if first.median else 0.0

    # A few percent either way over a quarter is noise on a marketplace where
    # sellers adjust prices constantly. Calling that a trend would have the
    # product announcing a direction every single time it is asked.
    if abs(percent) < 3.0:
        direction = "flat"
    else:
        direction = "rising" if change > 0 else "falling"

    return {
        "available": True,
        "direction": direction,
        "change_kes": str(change),
        "change_percent": round(percent, 1),
        "from_date": first.observed_on.isoformat(),
        "to_date": last.observed_on.isoformat(),
        "points": len(usable),
    }


@dataclass
class MarginInputs:
    """Everything the margin calculation needs, each labelled with its source.

    Assembled rather than guessed: if any input is missing the calculation
    refuses, because a margin computed against an assumed commission is a
    wrong answer about somebody's money.
    """

    median_price: Decimal
    commission_rate: float | None
    last_mile_kes: Decimal | None
    target_margin: float = 0.30
    sources: dict[str, str] = field(default_factory=dict)


def landed_cost_ceiling(inputs: MarginInputs) -> dict[str, Any]:
    """The most a vendor can pay per unit and still hit their target margin.

    This is the question the proposal puts on the product's main screen, so
    the arithmetic is written out step by step rather than compressed: every
    intermediate figure is returned, because a vendor about to commit a
    container needs to see where the number came from, not just trust it.
    """
    missing = []
    if inputs.commission_rate is None:
        missing.append("category commission rate")
    if inputs.last_mile_kes is None:
        missing.append("last mile cost for this county")

    if missing:
        return {
            "available": False,
            "reason": (
                "Cannot compute a margin without " + " and ".join(missing) +
                ". Stating one anyway would be a wrong answer about money."
            ),
            "missing": missing,
        }

    price = inputs.median_price
    commission = (price * Decimal(str(inputs.commission_rate))).quantize(Decimal("0.01"))
    last_mile = inputs.last_mile_kes
    target = Decimal(str(inputs.target_margin))

    # What the vendor keeps before the cost of the goods themselves.
    net_receipt = price - commission - last_mile
    # What must remain as margin at the target rate.
    required_margin = (price * target).quantize(Decimal("0.01"))
    ceiling = (net_receipt - required_margin).quantize(Decimal("0.01"))

    return {
        "available": True,
        "median_price": str(price),
        "commission_rate": inputs.commission_rate,
        "commission_kes": str(commission),
        "last_mile_kes": str(last_mile),
        "net_receipt_kes": str(net_receipt),
        "target_margin": inputs.target_margin,
        "required_margin_kes": str(required_margin),
        "landed_cost_ceiling_kes": str(ceiling),
        # A negative ceiling is a real and useful answer: at this market price,
        # with these costs, there is no landed cost that yields the target.
        "viable": ceiling > 0,
        "sources": inputs.sources,
    }
