"""Pipeline tests: storage, enrichment, rollup.

The idempotence tests matter most. A rerun that duplicates observations does
not fail loudly; it quietly doubles the weight of whatever it re-collected and
moves the median toward it.
"""

from datetime import date
from decimal import Decimal

import pytest

from soko.pipeline import JsonlStore, enrich, rollup, summarise_run
from soko.sources.base import Listing

TODAY = date(2026, 9, 9)
YESTERDAY = date(2026, 9, 8)


def listing(key: str, title: str, price: int, seller: str = "Shop A",
            observed: date = TODAY, platform: str = "jumia_ke") -> Listing:
    return Listing(
        platform_code=platform,
        source_key=key,
        source_url=f"https://example.test/{key}",
        title=title,
        price_kes=Decimal(price),
        observed_on=observed,
        seller_name=seller,
    )


def power_banks(count: int, start_price: int = 2000, sellers: int = 10) -> list[Listing]:
    return [
        listing(
            f"pb{i}",
            f"Power Bank {10000 + i * 100}mAh Fast Charging",
            start_price + i * 10,
            seller=f"Shop {i % sellers}",
        )
        for i in range(count)
    ]


@pytest.fixture
def store(tmp_path):
    return JsonlStore(tmp_path / "run.jsonl")


class TestStorage:
    def test_writes_and_reads_back(self, store):
        assert store.write([listing("a", "Power Bank 10000mAh", 2000)]) == 1
        rows = list(store.read())
        assert len(rows) == 1
        assert rows[0]["title"] == "Power Bank 10000mAh"

    def test_price_survives_as_a_decimal_string(self, store):
        # A Decimal silently becoming a float on write is exactly the drift
        # this product cannot have.
        store.write([listing("a", "Power Bank", 2499)])
        row = next(iter(store.read()))
        assert row["price_kes"] == "2499"
        assert Decimal(row["price_kes"]) == Decimal(2499)

    def test_rerun_on_the_same_day_writes_nothing_new(self, store):
        rows = [listing("a", "Power Bank", 2000)]
        assert store.write(rows) == 1
        assert store.write(rows) == 0
        assert store.count() == 1

    def test_the_next_day_is_a_new_observation(self, store):
        # This is the time series growing, not a duplicate.
        store.write([listing("a", "Power Bank", 2000, observed=YESTERDAY)])
        store.write([listing("a", "Power Bank", 1900, observed=TODAY)])
        assert store.count() == 2

    def test_reopening_remembers_what_is_stored(self, tmp_path):
        path = tmp_path / "run.jsonl"
        first = JsonlStore(path)
        first.write([listing("a", "Power Bank", 2000)])

        # A separate process, resuming. It must not re-add what is there.
        second = JsonlStore(path)
        assert second.write([listing("a", "Power Bank", 2000)]) == 0

    def test_same_key_on_two_platforms_is_two_observations(self, store):
        store.write([listing("a", "Power Bank", 2000, platform="jumia_ke")])
        store.write([listing("a", "Power Bank", 1800, platform="kilimall_ke")])
        assert store.count() == 2


class TestEnrichment:
    def test_attaches_a_category(self, store):
        store.write([listing("a", "Power Bank 20000mAh Fast Charging", 2000)])
        row = next(iter(enrich(store.read())))
        assert row["category_code"] == "power_banks"
        assert row["aggregatable"] is True

    def test_unclassifiable_rows_are_kept_but_not_aggregatable(self, store):
        # Stored, so a better rule tomorrow can reclassify without re-crawling.
        # Not aggregatable, so it does not move a number today.
        store.write([listing("a", "Assorted Job Lot Items", 2000)])
        row = next(iter(enrich(store.read())))
        assert row["category_code"] is None
        assert row["aggregatable"] is False

    def test_reclassification_needs_no_recrawl(self, store):
        # The property that makes storing the raw payload worth it.
        store.write([listing("a", "Power Bank 20000mAh", 2000)])
        first = [r["category_code"] for r in enrich(store.read())]
        second = [r["category_code"] for r in enrich(store.read())]
        assert first == second == ["power_banks"]


class TestRollup:
    def test_produces_a_figure_above_the_threshold(self, store):
        store.write(power_banks(40))
        results = rollup(list(enrich(store.read())))

        figures = results[("power_banks", "jumia_ke")]
        assert figures["available"] is True
        assert Decimal(figures["median"]) > 0

    def test_reports_thin_categories_rather_than_hiding_them(self, store):
        # A vendor asking about a thin category should be told it is thin.
        store.write(power_banks(5))
        results = rollup(list(enrich(store.read())))

        figures = results[("power_banks", "jumia_ke")]
        assert figures["available"] is False
        assert figures["observation_count"] == 5
        assert "Refusing" in figures["reason"]

    def test_excludes_unclassified_listings_from_figures(self, store):
        store.write(power_banks(40))
        store.write([listing(f"junk{i}", "Assorted Items", 999999) for i in range(20)])

        results = rollup(list(enrich(store.read())))
        figures = results[("power_banks", "jumia_ke")]

        # The 20 junk rows at a wild price must not have moved the median.
        assert figures["evidence"]["observation_count"] == 40

    def test_counts_distinct_sellers(self, store):
        store.write(power_banks(40, sellers=10))
        results = rollup(list(enrich(store.read())))
        assert results[("power_banks", "jumia_ke")]["evidence"]["seller_count"] == 10

    def test_attaches_the_platform_caveat(self, store):
        # Kilimall is category level and the product must say so.
        rows = [
            listing(f"k{i}", f"Power Bank {i}0000mAh", 2000 + i, platform="kilimall_ke")
            for i in range(40)
        ]
        store.write(rows)
        results = rollup(list(enrich(store.read())))

        caveats = results[("power_banks", "kilimall_ke")]["evidence"]["caveats"]
        # Kilimall used to be category-level, on a misreading of its robots
        # file. It is product-level now, and its caveat is about missing
        # commission rates instead.
        assert any("commission" in c.lower() for c in caveats)

    def test_separates_platforms(self, store):
        store.write(power_banks(40))
        store.write([
            listing(f"k{i}", f"Power Bank {i}0000mAh", 1800 + i, platform="kilimall_ke")
            for i in range(40)
        ])
        results = rollup(list(enrich(store.read())))

        assert ("power_banks", "jumia_ke") in results
        assert ("power_banks", "kilimall_ke") in results
        assert results[("power_banks", "jumia_ke")]["median"] != \
               results[("power_banks", "kilimall_ke")]["median"]


class TestRunSummary:
    def test_reports_classification_rate(self, store):
        store.write(power_banks(30))
        store.write([listing(f"j{i}", "Assorted Items", 500) for i in range(10)])

        summary = summarise_run(list(enrich(store.read())))

        assert summary["observations"] == 40
        assert summary["classified"] == 30
        assert summary["classification_rate"] == 0.75

    def test_empty_store_does_not_divide_by_zero(self):
        assert summarise_run([])["classification_rate"] == 0.0
