"""What is popular, and the careful distinction this module exists to keep.

A vendor asks "what is the most searched product in Nakuru". That question has
two possible readings, and only one of them can be answered honestly:

  What buyers SEARCHED FOR      Nobody publishes this. Not Jumia, not
                                Kilimall, not Glovo. Search volume is among
                                the most commercially valuable data a
                                marketplace holds and none of them expose it.
                                We do not have it and cannot get it by
                                crawling. Claiming otherwise would be
                                inventing a number.

  What sellers are SELLING      This we can see, because every listing is
                                public. Where a county has many sellers
                                competing in a category, sellers have already
                                decided there is demand there. That is a real
                                signal, and it is the one this module reports.

The second is not a substitute for the first and this module never presents it
as one. It is a different, weaker, honest thing: assortment concentration, not
demand. Every answer says so in the sentence itself, because a vendor who
misreads a seller count as a search volume will over-order.

Google Trends is a legitimate future source for genuine search interest and is
noted in the roadmap. Until it is collected, this module reports assortment
and says that is what it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from soko.aggregate import saturation_label


@dataclass
class CategoryActivity:
    """How much seller activity a category has, in one place.

    Deliberately named for what it measures. An earlier draft of this called
    itself `demand` and every reader who saw the field name assumed it meant
    buyer demand, which is exactly the misreading the whole module exists to
    prevent.
    """

    category_code: str
    listing_count: int
    seller_count: int
    median_price: Decimal
    saturation_index: float | None

    @property
    def listings_per_seller(self) -> float:
        return round(self.listing_count / self.seller_count, 2) if self.seller_count else 0.0

    @property
    def activity_score(self) -> float:
        """A rank, not a measurement.

        Distinct sellers dominate, and total listings only break ties between
        categories with similar seller counts.

        The first version of this multiplied sellers by listings-per-seller,
        which had the effect of cancelling the divisor: two sellers with 300
        listings scored above twenty-five real sellers with fifty. That is the
        exact failure this module is supposed to prevent, because one shop
        with a large catalogue is not a busy category, and a vendor told
        otherwise walks into a market with one incumbent and no proven demand.

        So sellers are weighted heavily and listings contribute only a small
        logarithmic nudge. Deliberately unitless: it orders categories against
        each other and nothing else, and giving it a unit would invite it
        being quoted as though it measured something.
        """
        import math

        return round(self.seller_count + math.log10(max(self.listing_count, 1)), 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category_code,
            "listings": self.listing_count,
            "sellers": self.seller_count,
            "median_price_kes": str(self.median_price),
            "listings_per_seller": self.listings_per_seller,
            "activity_score": self.activity_score,
            "saturation": saturation_label(self.saturation_index),
        }


# What this module reports, stated once and attached to every answer, so it
# cannot be dropped by a caller that forgets.
DISCLAIMER = (
    "This is what sellers are listing, not what buyers are searching for. "
    "No marketplace publishes search volume. A crowded category means sellers "
    "believe there is demand, which is a real signal but a weaker one, and it "
    "can equally mean the category is already saturated."
)


def category_activity(
    rollups: dict[tuple[str, str], dict[str, Any]],
    platform_code: str | None = None,
    limit: int = 10,
) -> list[CategoryActivity]:
    """Categories ranked by how much seller activity they carry."""
    activities: list[CategoryActivity] = []

    for (category, platform), figures in rollups.items():
        if platform_code and platform != platform_code:
            continue
        if not figures.get("available"):
            continue

        evidence = figures["evidence"]
        activities.append(CategoryActivity(
            category_code=category,
            listing_count=evidence["observation_count"],
            seller_count=evidence["seller_count"],
            median_price=Decimal(figures["median"]),
            saturation_index=figures.get("saturation_index"),
        ))

    activities.sort(key=lambda a: a.activity_score, reverse=True)
    return activities[:limit]


def activity_answer(
    rollups: dict[tuple[str, str], dict[str, Any]],
    county_code: str | None = None,
    county_name: str | None = None,
    platform_code: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """The honest answer to "what is popular here".

    Two things a vendor needs kept apart, and both are said explicitly:

      What we are reporting is assortment, not search volume.

      Our data is national. Marketplace listings do not carry a county, so we
      cannot break assortment down by county even though the question asked
      for that. What IS county specific is delivery cost and coverage, and
      those genuinely change the arithmetic, so the answer offers them.
    """
    ranked = category_activity(rollups, platform_code, limit)

    if not ranked:
        return {
            "answered": False,
            "text": (
                "Nothing collected yet, so there is no activity to report. "
                + DISCLAIMER
            ),
        }

    lead = ranked[0]
    names = ", ".join(a.category_code.replace("_", " ") for a in ranked[:3])

    location_note = ""
    if county_code:
        from soko.logistics import delivery_cost

        place = county_name or county_code.replace("_", " ").title()
        cost = delivery_cost(platform_code or "jumia_ke", county_code)
        location_note = (
            f" Our listing data is national, not per county: marketplace "
            f"listings do not say where a buyer is. What does change with "
            f"{place} is delivery"
        )
        if cost:
            location_note += (
                f", at KSh {cost.door_delivery} to the door against KSh "
                f"{cost.pickup_station} to a pickup station, on a "
                f"{cost.typical_days} day cycle. That is the figure that "
                f"actually moves your margin there."
            )
        else:
            location_note += (
                f", for which we do not have a confirmed figure there."
            )

    text = (
        f"By seller activity, the busiest categories we track are {names}. "
        f"{lead.category_code.replace('_', ' ').title()} leads with "
        f"{lead.seller_count} sellers across {lead.listing_count} listings, "
        f"at a median of KSh {lead.median_price} and looking "
        f"{saturation_label(lead.saturation_index)}."
        f"{location_note} {DISCLAIMER}"
    )

    return {
        "answered": True,
        "text": text,
        "ranked": [a.as_dict() for a in ranked],
        "disclaimer": DISCLAIMER,
        "county": county_code,
        "is_search_volume": False,
    }


def opportunity_answer(
    rollups: dict[tuple[str, str], dict[str, Any]],
    platform_code: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Where there is room, which is the question behind the question.

    A vendor asking what is most popular usually wants to know where to enter.
    Those are close to opposite: the most crowded category is the hardest one
    to enter. So this reports the inverse ranking, with the same honesty about
    what it does and does not measure.
    """
    activities = category_activity(rollups, platform_code, limit=100)
    if not activities:
        return {"answered": False, "text": "Nothing collected yet."}

    # Least crowded first, but only categories with enough sellers to say
    # anything. A category with two sellers is not an opportunity, it is an
    # absence of data.
    candidates = [a for a in activities if a.seller_count >= 3]
    if not candidates:
        return {
            "answered": False,
            "text": (
                "Not enough sellers in any category yet to say where there is "
                "room. " + DISCLAIMER
            ),
        }

    candidates.sort(key=lambda a: a.listings_per_seller)
    top = candidates[:limit]
    lead = top[0]

    return {
        "answered": True,
        "text": (
            f"{lead.category_code.replace('_', ' ').title()} is the least "
            f"crowded category we track: {lead.listings_per_seller} listings "
            f"per seller across {lead.seller_count} sellers, at a median of "
            f"KSh {lead.median_price}. Fewer listings per seller means sellers "
            f"are not yet stacking depth into it. "
            f"{DISCLAIMER}"
        ),
        "ranked": [a.as_dict() for a in top],
        "disclaimer": DISCLAIMER,
        "is_search_volume": False,
    }
