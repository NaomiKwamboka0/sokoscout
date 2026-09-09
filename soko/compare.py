"""One product, one place, every platform side by side.

This is the screen a vendor actually wants. They have a product in mind and a
county to deliver to, and the question is where to list it. Everything else
in the product exists to make this comparison honest.

Each column answers four things in the order a vendor thinks about them:

  what it sells for there      the median, with the sample behind it
  what the platform takes      commission, delivery, a provision for returns
  what they keep               the number that decides it
  when they are paid           because a better price on a slower cycle can
                               still be the worse deal for someone financing
                               stock

Every figure carries a link to where it came from: a product URL for a price,
the seller documentation for a fee. A source a vendor cannot click is only
marginally better than no source, because it cannot be checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from soko.markets import category_kind, country, platforms_to_compare
from soko.policies import platform_cost, policy


@dataclass
class Column:
    """One platform's answer for this product in this place."""

    platform_code: str
    platform_name: str
    source_url: str | None

    # Price, from collected listings.
    median: Decimal | None = None
    p25: Decimal | None = None
    p75: Decimal | None = None
    observations: int = 0
    sellers: int = 0
    collected_on: str | None = None
    examples: list[dict[str, Any]] = field(default_factory=list)

    # Cost, from published documentation.
    commission_rate: float | None = None
    commission_kes: Decimal | None = None
    delivery_kes: Decimal | None = None
    other_fees: list[dict[str, Any]] = field(default_factory=list)
    net_receipt: Decimal | None = None
    take_rate: float | None = None
    payout_days: int | None = None

    # Why this column is incomplete, if it is.
    gaps: list[str] = field(default_factory=list)

    @property
    def comparable(self) -> bool:
        """Whether this column can be ranked against the others.

        A column missing either a price or a cost is shown, but never ranked:
        putting it in the ordering would imply a completeness it does not
        have.
        """
        return self.median is not None and self.net_receipt is not None

    def as_dict(self) -> dict[str, Any]:
        def money(value: Decimal | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "platform": self.platform_code,
            "platform_name": self.platform_name,
            "source_url": self.source_url,
            "median_kes": money(self.median),
            "p25_kes": money(self.p25),
            "p75_kes": money(self.p75),
            "observations": self.observations,
            "sellers": self.sellers,
            "collected_on": self.collected_on,
            "examples": self.examples,
            "commission_rate": self.commission_rate,
            "commission_kes": money(self.commission_kes),
            "delivery_kes": money(self.delivery_kes),
            "other_fees": self.other_fees,
            "net_receipt_kes": money(self.net_receipt),
            "take_rate": self.take_rate,
            "payout_days": self.payout_days,
            "comparable": self.comparable,
            "gaps": self.gaps,
        }


def _examples(rows: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """A few real listings behind the median, so it can be spot-checked.

    Priced nearest the median rather than cheapest or dearest, because the
    point is to show what a typical listing looks like, not an outlier.
    """
    priced = [r for r in rows if r.get("price_kes") and r.get("source_url")]
    if not priced:
        return []

    values = sorted(Decimal(str(r["price_kes"])) for r in priced)
    middle = values[len(values) // 2]

    nearest = sorted(priced, key=lambda r: abs(Decimal(str(r["price_kes"])) - middle))
    return [
        {
            "title": r.get("title", "")[:90],
            "price_kes": str(Decimal(str(r["price_kes"]))),
            "seller": r.get("seller_name"),
            "url": r.get("source_url"),
        }
        for r in nearest[:limit]
    ]


def build_columns(
    category_code: str,
    rows: list[dict[str, Any]],
    rollups: dict[tuple[str, str], dict[str, Any]],
    county_code: str = "nairobi",
    country_code: str | None = None,
    chosen_platforms: list[str] | None = None,
) -> list[Column]:
    """A column per platform, ranked by what the vendor keeps.

    `rows` are the enriched observations, used only to pull example listings
    so a figure can be spot-checked. The figures themselves come from
    `rollups`, which is the one place medians are computed.
    """
    codes = platforms_to_compare(category_code, country_code, chosen_platforms)
    columns: list[Column] = []

    for code in codes:
        entry = policy(code) or {}
        column = Column(
            platform_code=code,
            platform_name=entry.get("name", code),
            source_url=entry.get("source_url"),
        )

        figures = rollups.get((category_code, code))

        if figures is None:
            column.gaps.append(
                f"We have not collected {category_code.replace('_', ' ')} "
                f"listings on {column.platform_name} yet."
            )
        elif not figures.get("available"):
            column.observations = figures.get("observation_count", 0)
            column.gaps.append(
                f"Only {column.observations} listings collected, and we need "
                f"{figures.get('needed', 30)} before stating a price."
            )
        else:
            evidence = figures["evidence"]
            column.median = Decimal(figures["median"])
            column.p25 = Decimal(figures["p25"])
            column.p75 = Decimal(figures["p75"])
            column.observations = evidence["observation_count"]
            column.sellers = evidence["seller_count"]
            column.collected_on = evidence["collected_on"]
            column.examples = _examples(
                [r for r in rows
                 if r.get("category_code") == category_code
                 and r.get("platform_code") == code]
            )

        # Costs are computed against this platform's own median where we have
        # one. Comparing a Jumia fee against a Kilimall price would produce a
        # number that describes no real transaction.
        if column.median is not None:
            cost = platform_cost(
                code,
                sale_price=column.median,
                category_code=category_code,
                county_code=county_code,
            )
            if cost:
                column.commission_rate = cost.commission_rate
                column.commission_kes = cost.commission_kes
                column.delivery_kes = cost.last_mile_kes
                column.other_fees = cost.other_fees
                column.net_receipt = cost.net_receipt
                column.take_rate = cost.take_rate
                column.payout_days = cost.payout_days
                column.gaps.extend(cost.unconfirmed)

        columns.append(column)

    # Comparable columns first, best-keeping first within them. An incomplete
    # column sorts last rather than being hidden, so a vendor can see that a
    # platform exists and why it has no numbers.
    columns.sort(
        key=lambda c: (not c.comparable, -(c.net_receipt or Decimal(0)))
    )
    return columns


def verdict(columns: list[Column], symbol: str = "KSh") -> dict[str, Any]:
    """The one-line answer, in the terms a vendor decides on.

    Names the gap in money rather than declaring a winner, because "Jumia is
    better" is not actionable and "you keep 47 shillings more per unit" is.
    Hedges the lead when it is smaller than something we do not know.
    """
    ranked = [c for c in columns if c.comparable]

    if not ranked:
        return {
            "decided": False,
            "text": (
                "We cannot compare these yet. "
                + " ".join(g for c in columns for g in c.gaps[:1])
            ),
        }

    best = ranked[0]
    parts = [
        f"{best.platform_name} leaves you the most: {symbol} {best.net_receipt} "
        f"per unit on a {symbol} {best.median} sale."
    ]

    if len(ranked) > 1:
        runner = ranked[1]
        gap = best.net_receipt - runner.net_receipt
        if gap > 0:
            parts.append(f"That is {symbol} {gap} more than {runner.platform_name}.")
        else:
            parts.append(f"{runner.platform_name} is level with it.")

        # A lead narrower than an unknown fee is not a lead. This came up on
        # the first real comparison and would otherwise read as decisive.
        unknown_fee = any("not confirmed" in g for c in ranked for g in c.gaps)
        if unknown_fee and 0 < gap < 30:
            parts.append(
                "Treat that as unproven though: the gap is small and at least "
                "one fee here is unconfirmed, so the order could flip."
            )

        faster = min((c for c in ranked if c.payout_days),
                     key=lambda c: c.payout_days, default=None)
        if faster and faster.platform_code != best.platform_code:
            parts.append(
                f"{faster.platform_name} pays faster though, in "
                f"{faster.payout_days} days against {best.payout_days}."
            )

    return {"decided": True, "best": best.platform_code, "text": " ".join(parts)}


def compare(
    category_code: str,
    category_label: str,
    rows: list[dict[str, Any]],
    rollups: dict[tuple[str, str], dict[str, Any]],
    county_code: str = "nairobi",
    county_label: str = "Nairobi",
    country_code: str | None = None,
    chosen_platforms: list[str] | None = None,
) -> dict[str, Any]:
    """The whole comparison, ready to render."""
    market = country(country_code)
    columns = build_columns(
        category_code, rows, rollups, county_code, country_code, chosen_platforms
    )

    return {
        "category": category_code,
        "category_label": category_label,
        "kind": category_kind(category_code),
        "county": county_code,
        "county_label": county_label,
        "country": market.code,
        "country_label": market.name,
        "currency": market.symbol,
        "columns": [c.as_dict() for c in columns],
        "verdict": verdict(columns, market.symbol),
    }
