"""Delivery coverage and last mile cost.

The second half of the margin calculation, and the reason a vendor cannot be
shown an encouraging answer about a county where the platform cannot actually
deliver.

The refusal behaviour here is the same as everywhere else in the product: an
unknown cost returns None and the caller refuses, rather than a national
average quietly standing in for a county nobody has checked.
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
def _logistics() -> dict[str, Any]:
    with open(CONFIG_DIR / "logistics.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@functools.lru_cache(maxsize=1)
def _counties() -> dict[str, Any]:
    with open(CONFIG_DIR / "counties.yaml", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return {entry["code"]: entry for entry in data["counties"]}


@dataclass(frozen=True)
class DeliveryCost:
    """What last mile costs, and how current the figure is.

    `read` travels with the number because a stale delivery cost is a wrong
    answer about money. The product shows it next to any margin it computes.
    """

    county_code: str
    county_name: str
    platform_code: str
    door_delivery: Decimal
    pickup_station: Decimal | None
    stations: int
    typical_days: int
    read: date
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "county": self.county_code,
            "county_name": self.county_name,
            "platform": self.platform_code,
            "door_delivery_kes": str(self.door_delivery),
            "pickup_station_kes": str(self.pickup_station) if self.pickup_station else None,
            "stations": self.stations,
            "typical_days": self.typical_days,
            "read": self.read.isoformat(),
            "source": self.source,
        }


def counties(with_logistics_only: bool = False) -> list[dict[str, Any]]:
    """The county list the product offers.

    Read from the coverage file rather than typed as free text, so a question
    about a county we do not serve is impossible rather than merely unlikely.
    """
    entries = list(_counties().values())
    if with_logistics_only:
        entries = [e for e in entries if e.get("has_logistics")]
    return sorted(entries, key=lambda e: (e["tier"], e["name"]))


def county(county_code: str | None) -> dict[str, Any] | None:
    """One county's record, or None if we do not cover it.

    Returns None rather than a placeholder, so a caller cannot accidentally
    render a county we have no logistics for as though we did.
    """
    if not county_code:
        return None
    for entry in counties():
        if entry["code"] == county_code:
            return entry
    return None


def county_exists(county_code: str | None) -> bool:
    return bool(county_code) and county_code in _counties()


def delivery_cost(platform_code: str, county_code: str) -> DeliveryCost | None:
    """Last mile cost for one county on one platform.

    Returns None when we do not have the figure, which the caller must treat
    as a refusal rather than as zero. A zero would silently improve every
    margin computed for that county.
    """
    if not county_exists(county_code):
        return None

    table = _logistics().get(platform_code) or {}
    entry = table.get(county_code)
    if not entry or entry.get("door_delivery") is None:
        return None

    meta = _logistics()["meta"]
    county = _counties()[county_code]

    pickup = entry.get("pickup_station")

    return DeliveryCost(
        county_code=county_code,
        county_name=county["name"],
        platform_code=platform_code,
        door_delivery=Decimal(str(entry["door_delivery"])),
        pickup_station=Decimal(str(pickup)) if pickup is not None else None,
        stations=int(entry.get("stations") or 0),
        typical_days=int(entry.get("typical_days") or 0),
        read=meta["read"] if isinstance(meta["read"], date)
             else date.fromisoformat(str(meta["read"])),
        source=meta["source"],
    )


def coverage_answer(platform_code: str, county_code: str) -> dict[str, Any]:
    """Whether a platform can deliver to a county, and on what terms.

    Answers the coverage intent. Distinguishes three cases that a vendor needs
    kept apart: a county we do not recognise, a county with no logistics at
    all, and a county served but at a cost that changes the arithmetic.
    """
    if not county_exists(county_code):
        return {
            "available": False,
            "reason": (
                "That is not a county in our coverage file. The list is "
                "maintained rather than guessed, so if it is missing we have "
                "not confirmed logistics there."
            ),
        }

    county = _counties()[county_code]
    if not county.get("has_logistics"):
        return {
            "available": True,
            "covered": False,
            "county_name": county["name"],
            "text": (
                f"{county['name']} has no confirmed platform logistics. "
                f"Listing there is possible but collection and delivery are not."
            ),
        }

    cost = delivery_cost(platform_code, county_code)
    if cost is None:
        return {
            "available": True,
            "covered": None,
            "county_name": county["name"],
            "text": (
                f"{county['name']} is a served county, but we do not have a "
                f"published delivery figure for this platform there. We are "
                f"not going to estimate one, because it feeds a margin."
            ),
        }

    pickup_text = (
        f", or KSh {cost.pickup_station} to one of {cost.stations} pickup stations"
        if cost.pickup_station is not None and cost.stations
        else ""
    )

    return {
        "available": True,
        "covered": True,
        "county_name": county["name"],
        "text": (
            f"Yes. Door delivery to {cost.county_name} costs KSh "
            f"{cost.door_delivery}{pickup_text}, typically {cost.typical_days} "
            f"day{'s' if cost.typical_days != 1 else ''}. "
            f"Read from {cost.source} on {cost.read.isoformat()}."
        ),
        "cost": cost.as_dict(),
    }
