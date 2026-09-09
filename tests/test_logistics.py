"""Logistics and coverage tests.

The refusal cases are the point again: a county we have not checked must not
inherit a national average, because that average feeds a margin and a margin
is an answer about somebody's money.
"""

from decimal import Decimal

import pytest

from soko.logistics import (
    counties,
    county_exists,
    coverage_answer,
    delivery_cost,
)


class TestCountyList:
    def test_is_read_from_the_coverage_file(self):
        # Not free text. A question about a county we do not serve should be
        # impossible rather than merely unlikely.
        names = {c["code"] for c in counties()}
        assert "nairobi" in names
        assert "mombasa" in names

    def test_filters_to_served_counties(self):
        served = counties(with_logistics_only=True)
        assert all(c["has_logistics"] for c in served)

    def test_orders_by_tier(self):
        tiers = [c["tier"] for c in counties()]
        assert tiers == sorted(tiers)

    def test_rejects_an_unknown_county(self):
        assert not county_exists("atlantis")
        assert not county_exists(None)


class TestDeliveryCost:
    def test_returns_a_figure_for_a_known_county(self):
        cost = delivery_cost("jumia_ke", "nairobi")
        assert cost is not None
        assert cost.door_delivery == Decimal(145)
        assert cost.pickup_station == Decimal(99)

    def test_cost_rises_with_distance(self):
        nairobi = delivery_cost("jumia_ke", "nairobi")
        kisumu = delivery_cost("jumia_ke", "kisumu")
        assert kisumu.door_delivery > nairobi.door_delivery

    def test_carries_the_date_it_was_read(self):
        # A stale delivery cost is a wrong answer about money, so the date
        # travels with the number rather than living only in the file.
        cost = delivery_cost("jumia_ke", "nairobi")
        assert cost.read.year == 2026
        assert "Jumia" in cost.source

    def test_unknown_county_returns_none_not_zero(self):
        # A zero would silently improve every margin computed for it.
        assert delivery_cost("jumia_ke", "atlantis") is None

    def test_a_platform_without_a_figure_does_not_borrow_another(self):
        # Kilimall has no published figure for Nakuru. It must not inherit
        # Jumia's, which would be a fabricated comparison.
        assert delivery_cost("jumia_ke", "nakuru") is not None
        assert delivery_cost("kilimall_ke", "nakuru") is None


class TestCoverageAnswer:
    def test_confirms_a_served_county(self):
        result = coverage_answer("jumia_ke", "mombasa")
        assert result["covered"] is True
        assert "Mombasa" in result["text"]
        assert "210" in result["text"]

    def test_states_pickup_as_well_as_door(self):
        # For a low value product the pickup figure changes the arithmetic.
        result = coverage_answer("jumia_ke", "nairobi")
        assert "pickup station" in result["text"]

    def test_cites_the_source_and_date(self):
        result = coverage_answer("jumia_ke", "nairobi")
        assert "Read from" in result["text"]
        assert "2026-09-09" in result["text"]

    def test_unknown_county_is_refused(self):
        result = coverage_answer("jumia_ke", "atlantis")
        assert result["available"] is False
        assert "coverage file" in result["reason"]

    def test_served_county_without_a_figure_says_so(self):
        # Distinguishes "we do not deliver there" from "we have not checked".
        result = coverage_answer("kilimall_ke", "nakuru")
        assert result["covered"] is None
        assert "not going to estimate" in result["text"]


class TestMarginIntegration:
    """The margin varies by county because last mile does."""

    def test_a_further_county_lowers_the_ceiling(self):
        from soko.aggregate import MarginInputs, landed_cost_ceiling

        def ceiling(county: str) -> Decimal:
            cost = delivery_cost("jumia_ke", county)
            result = landed_cost_ceiling(MarginInputs(
                median_price=Decimal(3000),
                commission_rate=0.11,
                last_mile_kes=cost.door_delivery,
            ))
            return Decimal(result["landed_cost_ceiling_kes"])

        assert ceiling("kisumu") < ceiling("nairobi")

    def test_missing_logistics_refuses_the_margin(self):
        from soko.aggregate import MarginInputs, landed_cost_ceiling

        cost = delivery_cost("kilimall_ke", "nakuru")
        result = landed_cost_ceiling(MarginInputs(
            median_price=Decimal(3000),
            commission_rate=0.11,
            last_mile_kes=cost.door_delivery if cost else None,
        ))
        assert result["available"] is False
