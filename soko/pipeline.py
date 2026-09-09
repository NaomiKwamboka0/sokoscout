"""The pipeline, in one place: collect, classify, aggregate, answer.

Storage is deliberately abstracted behind a small interface with a JSONL
implementation. The prototype runs with no database at all, which matters for
the same reason the predecessor's day one mattered: the fastest way to find
out whether the collected data is any good is to collect some and look at it,
not to stand up Postgres first.

The Postgres implementation is a drop in for the same interface once the
shape of the data has stopped changing.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator

from soko.aggregate import (
    InsufficientData,
    PriceSummary,
    saturation_index,
    saturation_label,
    summarise_prices,
)
from soko.classify import classify, is_aggregatable
from soko.sources.base import Listing, platform_entry


class _Encoder(json.JSONEncoder):
    """Decimals and dates survive the round trip.

    A Decimal silently becoming a float on write is exactly the drift this
    product cannot have, so it is encoded as a string and parsed back as a
    Decimal rather than left to the default encoder to refuse or mangle.
    """

    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        return super().default(o)


class JsonlStore:
    """Append only observation storage in a file.

    Mirrors the raw_listing table: unique on (platform, key, date), append
    only, payload preserved. Reruns are idempotent because the key set is
    checked before writing, which is the same property the database unique
    constraint provides.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seen: set[tuple[str, str, str]] = set()
        if self.path.exists():
            for row in self.read():
                self._seen.add(self._key(row))

    @staticmethod
    def _key(row: dict[str, Any]) -> tuple[str, str, str]:
        return (row["platform_code"], row["source_key"], row["observed_on"])

    def write(self, listings: Iterable[Listing]) -> int:
        """Append listings that are not already stored for that date.

        Returns how many were actually new, which is the figure worth
        reporting: a run that fetched 100 pages and wrote 0 new rows has told
        you something important about the run before it.
        """
        written = 0
        with open(self.path, "a", encoding="utf-8") as fh:
            for listing in listings:
                row = asdict(listing)
                row["observed_on"] = listing.observed_on.isoformat()
                key = self._key(row)
                if key in self._seen:
                    continue
                fh.write(json.dumps(row, cls=_Encoder) + "\n")
                self._seen.add(key)
                written += 1
        return written

    def read(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def count(self) -> int:
        return sum(1 for _ in self.read())


def enrich(rows: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Attach a category to each stored observation.

    Classification runs here rather than in the driver so it can be improved
    and re-run over data already collected. Re-crawling a platform to fix our
    own classification bug would be both slow and rude.
    """
    for row in rows:
        result = classify(row.get("title"), row.get("breadcrumb"))
        row["category_code"] = result["category_code"]
        row["classify_confidence"] = result["confidence"]
        row["is_service"] = result["is_service"]
        row["aggregatable"] = is_aggregatable(result)
        yield row


def rollup(
    rows: Iterable[dict[str, Any]],
    minimum: int | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Daily aggregates per (category, platform).

    Only listings the classifier is confident about enter a published figure.
    Everything else is stored and counted but does not move a number a vendor
    reads today, which is the whole point of keeping the confidence band.

    Categories below the observation threshold appear in the output marked
    unavailable, with their count. That is deliberate: a vendor asking about a
    thin category should be told it is thin, not told nothing.
    """
    from soko.aggregate import MIN_OBSERVATIONS

    threshold = MIN_OBSERVATIONS if minimum is None else minimum
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        if not row.get("aggregatable"):
            continue
        key = (row["category_code"], row["platform_code"])
        buckets[key].append(row)

    results: dict[tuple[str, str], dict[str, Any]] = {}

    for (category_code, platform_code), group in buckets.items():
        prices = [Decimal(str(r["price_kes"])) for r in group]
        sellers = [r.get("seller_name") for r in group]
        observed = max(r["observed_on"] for r in group)
        caveat = platform_entry(platform_code).get("caveat")

        try:
            summary: PriceSummary = summarise_prices(
                prices,
                sellers,
                [platform_code],
                collected_on=date.fromisoformat(observed),
                caveats=(caveat,) if caveat else (),
                minimum=threshold,
                context=f"{category_code} on {platform_code}",
            )
        except InsufficientData as exc:
            results[(category_code, platform_code)] = {
                "available": False,
                "category_code": category_code,
                "platform_code": platform_code,
                "observation_count": exc.have,
                "needed": exc.need,
                "reason": str(exc),
            }
            continue

        index = saturation_index(
            listing_count=summary.evidence.observation_count,
            seller_count=summary.evidence.seller_count,
            price_spread=summary.spread,
            median_price=summary.median,
        )

        results[(category_code, platform_code)] = {
            "available": True,
            "category_code": category_code,
            "platform_code": platform_code,
            **summary.as_dict(),
            "saturation_index": index,
            "saturation_label": saturation_label(index),
        }

    return results


def summarise_run(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What a run collected, for the operator and for the n8n summary line."""
    total = len(rows)
    classified = sum(1 for r in rows if r.get("category_code"))
    aggregatable = sum(1 for r in rows if r.get("aggregatable"))
    by_platform: dict[str, int] = defaultdict(int)
    by_category: dict[str, int] = defaultdict(int)

    for row in rows:
        by_platform[row["platform_code"]] += 1
        if row.get("category_code"):
            by_category[row["category_code"]] += 1

    return {
        "observations": total,
        "classified": classified,
        "aggregatable": aggregatable,
        "classification_rate": round(classified / total, 3) if total else 0.0,
        "by_platform": dict(by_platform),
        "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
    }
