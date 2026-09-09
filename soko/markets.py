"""Countries, and which platforms compete for a given product.

Two jobs, both about not showing a vendor the wrong comparison.

First, the country. Every market has its own marketplaces, commission
schedules and delivery costs, so a country is a dimension of the data rather
than an assumption in the code. A country we have not collected says so
instead of borrowing a neighbour's figures.

Second, the pairing. A vendor searching power banks should see Jumia against
Kilimall. A vendor searching chicken should see Glovo against Uber Eats.
Making them choose is making them do the app's thinking, so the pairing
follows the product, and the interface still allows an override.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@functools.lru_cache(maxsize=1)
def _config() -> dict[str, Any]:
    with open(CONFIG_DIR / "countries.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass(frozen=True)
class Country:
    code: str
    name: str
    active: bool
    currency: str
    symbol: str
    retail_platforms: tuple[str, ...]
    food_platforms: tuple[str, ...]
    note: str

    def platforms_for(self, kind: str) -> tuple[str, ...]:
        return self.food_platforms if kind == "food" else self.retail_platforms

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "active": self.active,
            "currency": self.currency,
            "symbol": self.symbol,
            "note": self.note,
        }


def _build(entry: dict[str, Any]) -> Country:
    platforms = entry.get("platforms") or {}
    return Country(
        code=entry["code"],
        name=entry["name"],
        active=bool(entry.get("active")),
        currency=entry.get("currency", ""),
        symbol=entry.get("currency_symbol", ""),
        retail_platforms=tuple(platforms.get("retail") or []),
        food_platforms=tuple(platforms.get("food") or []),
        note=(entry.get("coming_soon") or entry.get("tax_note") or "").strip(),
    )


@functools.lru_cache(maxsize=1)
def countries() -> list[Country]:
    return [_build(e) for e in _config()["countries"]]


def country(code: str | None = None) -> Country:
    """One country, falling back to the configured default."""
    wanted = code or _config().get("default", "kenya")
    for entry in countries():
        if entry.code == wanted:
            return entry
    return countries()[0]


def active_countries() -> list[Country]:
    return [c for c in countries() if c.active]


@functools.lru_cache(maxsize=1)
def _food_categories() -> frozenset[str]:
    kinds = _config().get("category_kinds") or {}
    return frozenset(kinds.get("food") or [])


def category_kind(category_code: str | None) -> str:
    """Whether a category is sold through marketplaces or delivery apps.

    An unrecognised category falls back to retail, which is the safer error:
    most of the taxonomy is retail, and a food comparison for a mystery
    product would confuse more than the reverse.
    """
    if category_code and category_code in _food_categories():
        return "food"
    default = (_config().get("category_kinds") or {}).get("default", "retail")
    return default


def platforms_to_compare(
    category_code: str | None,
    country_code: str | None = None,
    chosen: list[str] | None = None,
) -> list[str]:
    """Which platforms to put side by side.

    `chosen` is the vendor's explicit override. It wins over the automatic
    pairing, but is still filtered to the selected country, because a
    Tanzanian selection cannot be satisfied by Kenyan platforms and silently
    showing them would be the fabrication we are trying to avoid.
    """
    market = country(country_code)

    if chosen:
        available = set(market.retail_platforms) | set(market.food_platforms)
        return [code for code in chosen if code in available]

    return list(market.platforms_for(category_kind(category_code)))


def all_platforms(country_code: str | None = None) -> list[dict[str, Any]]:
    """Every platform in a country, for building a picker.

    Carries the kind so the interface can group them, and whether we actually
    hold data for each, so a vendor ticking Uber Eats is told up front rather
    than after they have run a comparison.
    """
    from soko.policies import policy

    market = country(country_code)
    rows: list[dict[str, Any]] = []

    for kind, codes in (("retail", market.retail_platforms),
                        ("food", market.food_platforms)):
        for code in codes:
            entry = policy(code) or {}
            commission = entry.get("commission") or {}
            rows.append({
                "code": code,
                "name": entry.get("name", code),
                "kind": kind,
                # A platform with no confirmed commission band cannot enter a
                # comparison, and the picker should say so before it is ticked.
                "has_data": commission.get("range_low") is not None,
                "source_url": entry.get("source_url"),
            })
    return rows
