"""Tests for the "what is popular" answers.

The whole point of this module is a distinction that is easy to blur and
expensive to get wrong: we can see what sellers LIST, and we cannot see what
buyers SEARCH FOR. A vendor who reads a seller count as a search volume will
over-order.

So the tests that matter most here are the ones asserting the disclaimer is
present and that no answer ever claims to be search data.
"""

from decimal import Decimal

import pytest

from soko.demand import (
    DISCLAIMER,
    activity_answer,
    category_activity,
    opportunity_answer,
)


def rollups(**categories) -> dict:
    """Build a rollup dict shaped like the real one."""
    out = {}
    for code, (listings, sellers, median) in categories.items():
        out[(code, "jumia_ke")] = {
            "available": True,
            "category_code": code,
            "platform_code": "jumia_ke",
            "median": str(median),
            "p25": str(median - 100),
            "p75": str(median + 100),
            "min": "1",
            "max": "9999",
            "saturation_index": round(listings / max(sellers, 1), 2),
            "saturation_label": "competitive",
            "evidence": {
                "observation_count": listings,
                "seller_count": sellers,
                "platforms": ["jumia_ke"],
                "collected_on": "2026-09-09",
                "caveats": [],
            },
        }
    return out


SAMPLE = rollups(
    phone_accessories=(200, 40, 700),
    audio=(120, 30, 3000),
    power_banks=(60, 25, 2500),
    beauty=(40, 8, 900),
)


class TestNeverClaimsToBeSearchData:
    """The distinction this module exists to keep."""

    def test_activity_answer_carries_the_disclaimer(self):
        result = activity_answer(SAMPLE)
        assert DISCLAIMER in result["text"]

    def test_the_disclaimer_says_what_it_is_not(self):
        assert "not what buyers are searching for" in DISCLAIMER
        assert "No marketplace publishes search volume" in DISCLAIMER

    def test_the_disclaimer_warns_both_ways(self):
        # A crowded category is ambiguous: sellers believe there is demand,
        # or the category is already saturated. Both readings must be given.
        assert "saturated" in DISCLAIMER

    def test_the_payload_is_flagged_as_not_search_volume(self):
        # A caller consuming the dict rather than the sentence must not be
        # able to mistake it either.
        assert activity_answer(SAMPLE)["is_search_volume"] is False
        assert opportunity_answer(SAMPLE)["is_search_volume"] is False

    def test_opportunity_answer_carries_the_disclaimer_too(self):
        assert DISCLAIMER in opportunity_answer(SAMPLE)["text"]

    def test_no_answer_uses_the_word_searched_as_a_claim(self):
        text = activity_answer(SAMPLE)["text"].lower()
        # It may say "not what buyers are searching for". It must never say
        # something is the most searched.
        assert "most searched" not in text


class TestActivityRanking:
    def test_ranks_by_seller_activity(self):
        ranked = category_activity(SAMPLE)
        assert ranked[0].category_code == "phone_accessories"

    def test_a_category_with_more_sellers_outranks_one_with_more_listings(self):
        # One shop with a huge catalogue is not a busy category.
        data = rollups(
            many_sellers=(50, 25, 1000),
            one_big_seller=(300, 2, 1000),
        )
        ranked = category_activity(data)
        assert ranked[0].category_code == "many_sellers"

    def test_thin_categories_are_excluded(self):
        data = dict(SAMPLE)
        data[("thin", "jumia_ke")] = {
            "available": False,
            "category_code": "thin",
            "platform_code": "jumia_ke",
            "observation_count": 4,
            "needed": 30,
            "reason": "too thin",
        }
        codes = {a.category_code for a in category_activity(data)}
        assert "thin" not in codes

    def test_respects_the_limit(self):
        assert len(category_activity(SAMPLE, limit=2)) == 2

    def test_empty_rollups_answer_honestly(self):
        result = activity_answer({})
        assert result["answered"] is False
        assert DISCLAIMER in result["text"]


class TestLocationHandling:
    """The county part of "what is most searched in Nakuru"."""

    def test_says_the_listing_data_is_national(self):
        # Marketplace listings do not carry a county. Pretending otherwise
        # would be the same class of invention as reporting search volume.
        result = activity_answer(SAMPLE, county_code="nakuru")
        assert "national" in result["text"]

    def test_offers_what_is_genuinely_county_specific(self):
        # Delivery cost really does vary by county and really does change the
        # margin, so the answer gives that instead of a fabricated breakdown.
        result = activity_answer(SAMPLE, county_code="nakuru")
        assert "delivery" in result["text"].lower()
        assert "250" in result["text"]      # Nakuru door delivery

    def test_a_county_without_a_figure_says_so(self):
        # Kilimall publishes no delivery figure for Bungoma. The answer must
        # say so rather than borrowing Jumia's number for it.
        #
        # The rollups have to be Kilimall's own, or the function returns
        # "nothing collected" before it ever reaches the delivery lookup.
        kilimall_data = {
            ("phone_accessories", "kilimall_ke"): dict(
                SAMPLE[("phone_accessories", "jumia_ke")],
                platform_code="kilimall_ke",
            )
        }
        result = activity_answer(
            kilimall_data, county_code="bungoma", platform_code="kilimall_ke"
        )
        assert "do not have a confirmed figure" in result["text"]

    def test_works_without_a_county(self):
        result = activity_answer(SAMPLE)
        assert result["answered"] is True
        assert "national" not in result["text"]


class TestOpportunity:
    def test_finds_the_least_crowded_category(self):
        data = rollups(
            crowded=(400, 20, 1000),      # 20 listings per seller
            roomy=(40, 20, 1000),         # 2 listings per seller
        )
        result = opportunity_answer(data)
        assert "Roomy" in result["text"] or "roomy" in result["text"]

    def test_ignores_categories_with_too_few_sellers(self):
        # Two sellers is not an opportunity, it is an absence of data.
        data = rollups(
            two_sellers=(4, 2, 1000),
            real_category=(60, 20, 1000),
        )
        result = opportunity_answer(data)
        assert "two_sellers" not in result["text"]

    def test_refuses_when_nothing_has_enough_sellers(self):
        data = rollups(tiny=(4, 2, 1000))
        result = opportunity_answer(data)
        assert result["answered"] is False
        assert DISCLAIMER in result["text"]

    def test_explains_what_the_ranking_means(self):
        result = opportunity_answer(SAMPLE)
        assert "listings per seller" in result["text"]
