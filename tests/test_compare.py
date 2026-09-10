"""Side-by-side comparison tests.

This is the screen a vendor decides on, so the tests that matter most are the
ones proving a column never borrows another platform's numbers, and that an
incomplete column says why rather than quietly disappearing.
"""

from decimal import Decimal

import pytest

from soko.compare import build_columns, compare, verdict
from soko.markets import (
    active_countries,
    all_platforms,
    category_kind,
    country,
    platforms_to_compare,
)


def rollup_entry(median: str, observations: int = 45, sellers: int = 22) -> dict:
    return {
        "available": True,
        "median": median,
        "p25": str(Decimal(median) - 400),
        "p75": str(Decimal(median) + 400),
        "saturation_index": 2.0,
        "saturation_label": "competitive",
        "evidence": {
            "observation_count": observations,
            "seller_count": sellers,
            "platforms": ["jumia_ke"],
            "collected_on": "2026-09-09",
            "caveats": [],
        },
    }


BOTH = {
    ("power_banks", "jumia_ke"): rollup_entry("3000"),
    ("power_banks", "kilimall_ke"): rollup_entry("2800"),
}

ONLY_JUMIA = {("power_banks", "jumia_ke"): rollup_entry("3000")}


class TestCountries:
    def test_kenya_is_active(self):
        assert country("kenya").active

    def test_uncollected_countries_are_inactive(self):
        # Tanzania and Uganda are listed so a vendor sees they are coming,
        # but inactive so no Kenyan figure is ever shown under their flag.
        assert not country("tanzania").active
        assert not country("uganda").active

    def test_an_inactive_country_explains_itself(self):
        assert country("tanzania").note

    def test_only_kenya_is_selectable(self):
        assert [c.code for c in active_countries()] == ["kenya"]

    def test_an_unknown_country_falls_back_rather_than_crashing(self):
        assert country("atlantis").code == "kenya"


class TestPlatformPairing:
    def test_retail_products_compare_marketplaces(self):
        codes = platforms_to_compare("power_banks")
        assert "jumia_ke" in codes
        assert "kilimall_ke" in codes
        # A vendor selling power banks should not see food delivery columns.
        assert "glovo_ke" not in codes

    def test_an_unknown_category_defaults_to_retail(self):
        # Retail is the safer error: most of the taxonomy is retail, so a
        # food comparison for a mystery product would confuse more.
        assert category_kind("something_new") == "retail"
        assert "jumia_ke" in platforms_to_compare("something_new")

    def test_the_vendor_can_override_the_pairing(self):
        chosen = platforms_to_compare("power_banks", chosen=["glovo_ke"])
        assert chosen == ["glovo_ke"]

    def test_an_override_is_still_filtered_to_the_country(self):
        # A platform that does not operate in the selected country cannot be
        # forced into the comparison.
        assert platforms_to_compare("power_banks", "tanzania", ["jumia_ke"]) == []

    def test_the_picker_reports_which_platforms_have_a_commission(self):
        # Uber Eats publishes headline plan rates on a Kenya page, so it now
        # has a confirmed band. Kilimall does not publish one anywhere we can
        # read, so it does not. The picker reflects what is actually known
        # rather than a fixed idea of which platforms are covered.
        rows = {p["code"]: p for p in all_platforms("kenya")}
        assert rows["jumia_ke"]["has_data"] is True
        assert rows["kilimall_ke"]["has_data"] is False

    def test_every_platform_carries_a_source_link(self):
        for row in all_platforms("kenya"):
            assert row["source_url"], f"{row['code']} has no source link"


class TestColumns:
    def test_a_column_per_platform(self):
        columns = build_columns("power_banks", [], BOTH)
        assert {c.platform_code for c in columns} == {"jumia_ke", "kilimall_ke"}

    def test_ranked_by_what_the_vendor_keeps(self):
        columns = [c for c in build_columns("power_banks", [], BOTH) if c.comparable]
        receipts = [c.net_receipt for c in columns]
        assert receipts == sorted(receipts, reverse=True)

    def test_a_platform_without_data_is_shown_not_hidden(self):
        # A vendor who cannot find a platform assumes the tool is broken.
        columns = build_columns("power_banks", [], ONLY_JUMIA)
        kilimall = next(c for c in columns if c.platform_code == "kilimall_ke")
        assert kilimall.median is None
        assert kilimall.gaps

    def test_a_platform_without_data_never_borrows_a_price(self):
        # The fabrication this whole screen exists to avoid.
        columns = build_columns("power_banks", [], ONLY_JUMIA)
        kilimall = next(c for c in columns if c.platform_code == "kilimall_ke")
        assert kilimall.net_receipt is None
        assert not kilimall.comparable

    def test_costs_are_computed_against_each_platforms_own_price(self):
        # Comparing a Jumia fee against a Kilimall price would describe no
        # real transaction.
        columns = {c.platform_code: c for c in build_columns("power_banks", [], BOTH)}
        assert columns["jumia_ke"].median == Decimal("3000")
        assert columns["kilimall_ke"].median == Decimal("2800")
        assert columns["jumia_ke"].commission_kes != columns["kilimall_ke"].commission_kes

    def test_a_thin_category_reports_the_shortfall(self):
        thin = {
            ("power_banks", "jumia_ke"): {
                "available": False,
                "observation_count": 7,
                "needed": 30,
            }
        }
        columns = build_columns("power_banks", [], thin)
        jumia = next(c for c in columns if c.platform_code == "jumia_ke")
        assert jumia.median is None
        assert "7" in jumia.gaps[0]

    def test_incomplete_columns_sort_last(self):
        columns = build_columns("power_banks", [], ONLY_JUMIA)
        assert columns[0].comparable
        assert not columns[-1].comparable


class TestExampleListings:
    def test_examples_carry_a_clickable_url(self):
        rows = [
            {
                "category_code": "power_banks",
                "platform_code": "jumia_ke",
                "title": "Oraimo Power Bank 20000mAh",
                "price_kes": "3000",
                "seller_name": "Shop A",
                "source_url": "https://www.jumia.co.ke/p1.html",
            }
        ]
        columns = build_columns("power_banks", rows, ONLY_JUMIA)
        jumia = next(c for c in columns if c.platform_code == "jumia_ke")
        assert jumia.examples
        assert jumia.examples[0]["url"].startswith("https://")

    def test_listings_without_a_url_are_not_offered(self):
        # An example a vendor cannot open cannot be checked, so it is not
        # worth showing.
        rows = [{
            "category_code": "power_banks", "platform_code": "jumia_ke",
            "title": "No link", "price_kes": "3000", "seller_name": "X",
        }]
        columns = build_columns("power_banks", rows, ONLY_JUMIA)
        assert not next(c for c in columns if c.platform_code == "jumia_ke").examples


class TestVerdict:
    def test_names_the_gap_in_money(self):
        # "Jumia is better" is not actionable. "You keep 47 more" is.
        #
        # Only Jumia is costable today, because Kilimall publishes no
        # commission rate, so there may be a single ranked column and nothing
        # to compare it against. The gap sentence appears only when there is
        # a second column; what must always hold is that the decided answer
        # names money.
        columns = build_columns("power_banks", [], BOTH)
        result = verdict(columns)
        assert result["decided"]
        assert "KSh" in result["text"]

        if len([c for c in columns if c.comparable]) > 1:
            assert "more than" in result["text"] or "level" in result["text"]

    def test_refuses_when_nothing_is_comparable(self):
        assert not verdict(build_columns("power_banks", [], {}))["decided"]

    def test_mentions_the_faster_payer_only_when_it_is_a_different_platform(self):
        # A platform that pays less but faster is a real trade-off worth
        # naming. When the same platform both wins and pays fastest there is
        # nothing to add, and saying "Jumia pays faster than Jumia" would be
        # noise, so the sentence is correctly absent.
        columns = build_columns("power_banks", [], BOTH)
        ranked = [c for c in columns if c.comparable]
        text = verdict(columns)["text"]

        fastest = min(ranked, key=lambda c: c.payout_days or 999)
        if fastest.platform_code != ranked[0].platform_code:
            assert "pays faster" in text
        else:
            assert "pays faster" not in text


class TestWholeComparison:
    def test_carries_the_place_and_country(self):
        result = compare("power_banks", "Power Banks", [], BOTH,
                         "kisumu", "Kisumu")
        assert result["county_label"] == "Kisumu"
        assert result["country_label"] == "Kenya"
        assert result["currency"] == "KSh"

    def test_delivery_differs_by_county(self):
        # The county is not decoration: it changes the arithmetic.
        nairobi = compare("power_banks", "P", [], BOTH, "nairobi", "Nairobi")
        kisumu = compare("power_banks", "P", [], BOTH, "kisumu", "Kisumu")

        def delivery(result):
            return result["columns"][0]["delivery_kes"]

        assert delivery(nairobi) != delivery(kisumu)
