"""Platform policies and the total cost of doing business.

Answers the qualitative half of the product: commissions, payout timing,
onboarding requirements, returns, and the fees that never appear in a
commission rate but still come out of a vendor's money.

The comparison this produces is the thing a vendor actually needs and cannot
easily assemble themselves: not "which platform has the lowest commission",
which is the question they ask, but "what does it cost me in total to sell
this product here versus there", which is the question they mean.

Every figure carries its source and the date it was read. Where a figure is
not confirmed the answer says so rather than estimating, because a vendor
choosing a platform on a fabricated fee is worse off than one who was told we
do not know.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@functools.lru_cache(maxsize=1)
def _policies() -> dict[str, Any]:
    with open(CONFIG_DIR / "policies.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def policy(platform_code: str) -> dict[str, Any] | None:
    return (_policies().get("platforms") or {}).get(platform_code)


def known_platforms() -> list[str]:
    return sorted((_policies().get("platforms") or {}).keys())


def _read_date(entry: dict[str, Any]) -> str:
    value = entry.get("read") or _policies()["meta"]["read"]
    return value.isoformat() if isinstance(value, date) else str(value)


# ---------------------------------------------------------------------------
# Policy answers
# ---------------------------------------------------------------------------

def onboarding_answer(platform_code: str) -> dict[str, Any]:
    """What it takes to start selling on a platform."""
    entry = policy(platform_code)
    if not entry:
        return {"available": False, "reason": f"We do not have policy data for {platform_code}."}

    onboarding = entry.get("onboarding") or {}
    requirements = onboarding.get("requirements") or []

    if not requirements:
        return {
            "available": True,
            "answered": False,
            "text": (
                f"We do not have confirmed onboarding requirements for "
                f"{entry['name']}. {onboarding.get('note') or ''}".strip()
            ),
        }

    listed = "; ".join(requirements)
    days = onboarding.get("typical_days")
    timing = f" Approval typically takes about {days} days." if days else ""
    cost = onboarding.get("cost")
    cost_text = " There is no listing fee or monthly seller subscription." if cost == 0 else ""

    return {
        "available": True,
        "answered": True,
            "source_url": entry.get("source_url"),
            "source_name": entry.get("source"),
        "text": (
            f"To sell on {entry['name']} you need: {listed}.{timing}{cost_text} "
            f"Read from {entry['source']} on {_read_date(entry)}."
        ),
        "requirements": requirements,
    }


def payout_answer(platform_code: str) -> dict[str, Any]:
    """When a vendor actually gets paid, and what that does to their cash."""
    entry = policy(platform_code)
    if not entry:
        return {"available": False, "reason": f"We do not have policy data for {platform_code}."}

    payout = entry.get("payout") or {}
    days = payout.get("cycle_days")

    if days is None:
        return {
            "available": True,
            "answered": False,
            "text": f"We do not have a confirmed payout cycle for {entry['name']}.",
        }

    # The note usually restates the cycle in words. Lead with the note where
    # there is one and skip the generated sentence, rather than printing both
    # and saying "14 day cycle" twice in consecutive sentences.
    if payout.get("note"):
        text = f"{entry['name']}: {payout['note'].strip()}"
    else:
        text = (
            f"{entry['name']} pays out on roughly a {days} day cycle after "
            f"delivery confirmation."
        )

    if payout.get("cash_cycle_warning"):
        text += f" {payout['cash_cycle_warning'].strip()}"

    return {
        "available": True,
        "answered": True,
            "source_url": entry.get("source_url"),
            "source_name": entry.get("source"),
        "text": f"{text} Read from {entry['source']} on {_read_date(entry)}.",
        "cycle_days": days,
    }


def returns_answer(platform_code: str) -> dict[str, Any]:
    entry = policy(platform_code)
    if not entry:
        return {"available": False, "reason": f"We do not have policy data for {platform_code}."}

    returns = entry.get("returns") or {}
    window = returns.get("window_days")

    if window is None:
        return {
            "available": True,
            "answered": False,
            "text": f"We do not have a confirmed returns policy for {entry['name']}.",
        }

    who = returns.get("who_pays") or "Not specified."
    note = f" {returns['note']}" if returns.get("note") else ""

    return {
        "available": True,
        "answered": True,
            "source_url": entry.get("source_url"),
            "source_name": entry.get("source"),
        "text": (
            f"{entry['name']} allows returns within {window} days. "
            f"Who pays: {who}{note} "
            f"Read from {entry['source']} on {_read_date(entry)}."
        ),
    }


# ---------------------------------------------------------------------------
# Cost of doing business comparison
# ---------------------------------------------------------------------------

@dataclass
class PlatformCost:
    """What one platform costs for one sale, itemised.

    Itemised rather than a single number because the point of the comparison
    is that the commission is not the whole cost, and collapsing it back to
    one figure would hide exactly what a vendor came here to see.
    """

    platform_code: str
    platform_name: str
    sale_price: Decimal
    commission_rate: float | None
    commission_kes: Decimal | None
    last_mile_kes: Decimal | None
    other_fees: list[dict[str, Any]]
    payout_days: int | None
    net_receipt: Decimal | None
    unconfirmed: list[str]

    @property
    def total_cost(self) -> Decimal | None:
        if self.net_receipt is None:
            return None
        return self.sale_price - self.net_receipt

    @property
    def take_rate(self) -> float | None:
        """Everything the platform costs, as a share of the sale price.

        This is the number the comparison exists to produce. A platform with a
        lower commission and a higher return fee can easily have the higher
        take rate, and a vendor comparing commissions alone will pick wrong.
        """
        total = self.total_cost
        if total is None or self.sale_price <= 0:
            return None
        return round(float(total / self.sale_price), 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform_code,
            "platform_name": self.platform_name,
            "sale_price_kes": str(self.sale_price),
            "commission_rate": self.commission_rate,
            "commission_kes": str(self.commission_kes) if self.commission_kes is not None else None,
            "last_mile_kes": str(self.last_mile_kes) if self.last_mile_kes is not None else None,
            "other_fees": self.other_fees,
            "payout_days": self.payout_days,
            "net_receipt_kes": str(self.net_receipt) if self.net_receipt is not None else None,
            "total_cost_kes": str(self.total_cost) if self.total_cost is not None else None,
            "take_rate": self.take_rate,
            "unconfirmed": self.unconfirmed,
        }


def platform_cost(
    platform_code: str,
    sale_price: Decimal,
    category_code: str | None = None,
    county_code: str = "nairobi",
    include_return_provision: bool = True,
    return_rate: float = 0.05,
) -> PlatformCost | None:
    """What one sale actually costs on one platform.

    `return_rate` provisions for returns as an expected cost rather than
    pretending they never happen. Five percent is a conservative default and
    is stated in the output, because a vendor in fashion should raise it and
    one in electronics can lower it.
    """
    entry = policy(platform_code)
    if not entry:
        return None

    from soko.classify import commission_rate as category_commission
    from soko.logistics import delivery_cost

    unconfirmed: list[str] = []

    # The per-category rates in taxonomy.yaml are Jumia's published schedule.
    # They are only valid for Jumia.
    #
    # Applying them to another platform was a real bug: Kilimall, which
    # publishes no commission rate anywhere we can read, was being costed at
    # Jumia's 11% on power banks and producing a confident net receipt from a
    # figure that describes a different company. That is the same failure as
    # borrowing another platform's price, and harder to see because the
    # arithmetic is correct.
    band = entry.get("commission") or {}
    rate = None
    if category_code and band.get("per_category_rates_apply"):
        rate = category_commission(category_code)

    if rate is None:
        low, high = band.get("range_low"), band.get("range_high")
        if low is not None and high is not None:
            # Midpoint of the published band, clearly flagged. Better than
            # refusing outright for a comparison view, but the vendor is told.
            rate = round((low + high) / 2, 4)
            unconfirmed.append(
                f"commission is the midpoint of {entry['name']}'s published "
                f"{low:.0%} to {high:.0%} band, not a category rate"
            )
        else:
            unconfirmed.append(f"no commission figure confirmed for {entry['name']}")

    commission_kes = (
        (sale_price * Decimal(str(rate))).quantize(Decimal("0.01"))
        if rate is not None else None
    )

    cost = delivery_cost(platform_code, county_code)
    last_mile = cost.door_delivery if cost else None
    if last_mile is None:
        unconfirmed.append(f"no delivery figure for {county_code} on {entry['name']}")

    other_fees: list[dict[str, Any]] = []
    for fee in entry.get("fees") or []:
        basis = fee.get("basis")
        value = fee.get("value")

        if basis == "per_return" and include_return_provision:
            if value is None:
                unconfirmed.append(f"return handling fee not confirmed for {entry['name']}")
                continue
            provision = (Decimal(str(value)) * Decimal(str(return_rate))).quantize(Decimal("0.01"))
            other_fees.append({
                "name": f"{fee['name']} provision",
                "amount_kes": str(provision),
                "note": f"{fee['name']} of KSh {value} at an assumed {return_rate:.0%} return rate",
            })
        elif basis == "per_unit_per_month" and value:
            other_fees.append({
                "name": fee["name"],
                "amount_kes": str(Decimal(str(value))),
                "note": fee.get("note", ""),
            })

    if commission_kes is None or last_mile is None:
        net = None
    else:
        deductions = commission_kes + last_mile + sum(
            Decimal(f["amount_kes"]) for f in other_fees
        )
        net = (sale_price - deductions).quantize(Decimal("0.01"))

    return PlatformCost(
        platform_code=platform_code,
        platform_name=entry["name"],
        sale_price=sale_price,
        commission_rate=rate,
        commission_kes=commission_kes,
        last_mile_kes=last_mile,
        other_fees=other_fees,
        payout_days=(entry.get("payout") or {}).get("cycle_days"),
        net_receipt=net,
        unconfirmed=unconfirmed,
    )


def compare_platforms(
    sale_price: Decimal,
    category_code: str | None = None,
    county_code: str = "nairobi",
    platform_codes: list[str] | None = None,
) -> dict[str, Any]:
    """The cost of doing business across platforms, side by side.

    Ranked by what the vendor actually keeps, not by commission rate. Those
    orders differ, and that difference is the entire value of this view: a
    platform with a lower commission and a real return cost can be the more
    expensive place to sell.
    """
    codes = platform_codes or known_platforms()
    costs = [
        c for c in (
            platform_cost(code, sale_price, category_code, county_code)
            for code in codes
        ) if c is not None
    ]

    comparable = [c for c in costs if c.net_receipt is not None]
    incomparable = [c for c in costs if c.net_receipt is None]

    comparable.sort(key=lambda c: c.net_receipt, reverse=True)

    result: dict[str, Any] = {
        "sale_price_kes": str(sale_price),
        "category_code": category_code,
        "county_code": county_code,
        "platforms": [c.as_dict() for c in comparable],
        "not_comparable": [
            {
                "platform": c.platform_code,
                "platform_name": c.platform_name,
                "why": c.unconfirmed,
            }
            for c in incomparable
        ],
    }

    if comparable:
        best = comparable[0]
        result["best"] = best.platform_code
        result["text"] = _comparison_sentence(best, comparable)

    return result


def _comparison_sentence(best: PlatformCost, ranked: list[PlatformCost]) -> str:
    """The comparison as a vendor reads it.

    Names the gap in shillings rather than only the winner, because "Jumia is
    better" is not actionable and "you keep KSh 47 more per unit" is.
    """
    parts = [
        f"On a KSh {best.sale_price} sale, {best.platform_name} leaves you the "
        f"most: KSh {best.net_receipt} after a "
        f"{best.commission_rate:.0%} commission"
        f"{f' and KSh {best.last_mile_kes} delivery' if best.last_mile_kes else ''}."
    ]

    if len(ranked) > 1:
        runner = ranked[1]
        gap = best.net_receipt - runner.net_receipt
        if gap > 0:
            parts.append(
                f"That is KSh {gap} per unit more than {runner.platform_name}."
            )
        else:
            parts.append(f"{runner.platform_name} is effectively level.")

        # A win by less than the size of an unconfirmed fee is not a win, and
        # a vendor should not act on it. This came up on the very first real
        # comparison: Kilimall led Jumia by KSh 7.50 while its own return
        # handling fee was unknown and Jumia's provision was KSh 27.50. The
        # ranking would flip outright once that figure is known, and a
        # comparison that presented the lead without saying so would be
        # technically accurate and practically misleading.
        missing_fee_platforms = [
            c.platform_name for c in ranked
            if any("return handling fee not confirmed" in note for note in c.unconfirmed)
        ]
        if missing_fee_platforms and gap > 0:
            largest_known_fee = max(
                (
                    Decimal(fee["amount_kes"])
                    for c in ranked for fee in c.other_fees
                ),
                default=Decimal(0),
            )
            if largest_known_fee >= gap:
                parts.append(
                    f"Treat that lead as unproven: the gap is KSh {gap} and "
                    f"{', '.join(missing_fee_platforms)} has an unconfirmed "
                    f"return handling fee, which is worth up to KSh "
                    f"{largest_known_fee} elsewhere. The ranking could flip "
                    f"once that figure is known."
                )

    if best.payout_days:
        fastest = min(
            (c for c in ranked if c.payout_days), key=lambda c: c.payout_days, default=None
        )
        if fastest and fastest.platform_code != best.platform_code:
            parts.append(
                f"{fastest.platform_name} pays faster though, on a "
                f"{fastest.payout_days} day cycle against {best.payout_days}."
            )

    if best.unconfirmed:
        parts.append(f"Caveat: {best.unconfirmed[0]}.")

    return " ".join(parts)
