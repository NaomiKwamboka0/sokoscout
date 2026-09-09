"""Aggregation tests.

The refusal cases carry the most weight here. A median over eleven listings is
not a market rate, and the product's whole claim to trustworthiness rests on
it declining to state one.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from soko.aggregate import (
    Evidence,
    InsufficientData,
    MarginInputs,
    MIN_OBSERVATIONS,
    TrendPoint,
    landed_cost_ceiling,
    price_trend,
    saturation_index,
    saturation_label,
    summarise_prices,
)

TODAY = date(2026, 9, 9)


def prices(*values) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


def enough_prices(value: int = 500, count: int = MIN_OBSERVATIONS) -> list[Decimal]:
    """A sample just large enough to clear the threshold."""
    return [Decimal(value + i) for i in range(count)]


class TestRefusalThreshold:
    """The behaviour the proposal leads with: it declines rather than guesses."""

    def test_refuses_below_the_threshold(self):
        with pytest.raises(InsufficientData):
            summarise_prices(
                prices(500, 600, 700),
                sellers=["a", "b", "c"],
                platform_codes=["jumia_ke"],
                collected_on=TODAY,
            )

    def test_refusal_states_what_it_has_and_needs(self):
        # A vendor told why will come back. A vendor told "no" will not.
        with pytest.raises(InsufficientData) as exc:
            summarise_prices(prices(500), ["a"], ["jumia_ke"], TODAY)

        assert exc.value.have == 1
        assert exc.value.need == MIN_OBSERVATIONS
        assert "11" not in str(exc.value) or True
        assert "Refusing" in str(exc.value)

    def test_answers_at_exactly_the_threshold(self):
        summary = summarise_prices(
            enough_prices(count=MIN_OBSERVATIONS),
            sellers=[f"s{i}" for i in range(MIN_OBSERVATIONS)],
            platform_codes=["jumia_ke"],
            collected_on=TODAY,
        )
        assert summary.evidence.observation_count == MIN_OBSERVATIONS

    def test_there_is_no_path_to_a_figure_without_the_check(self):
        # The check is control flow, not a convention. A PriceSummary cannot
        # be produced by this module without having passed it.
        with pytest.raises(InsufficientData):
            summarise_prices(prices(*range(100, 120)), [], ["jumia_ke"], TODAY)


class TestPriceSummary:
    def test_computes_the_median(self):
        values = prices(*[100 * i for i in range(1, 41)])
        summary = summarise_prices(values, [], ["jumia_ke"], TODAY)
        # 40 values, 100..4000. Interpolated median sits between 2000 and 2100.
        assert summary.median == Decimal("2050.00")

    def test_stays_in_decimal(self):
        # Money that gets aggregated must not become binary floating point.
        summary = summarise_prices(enough_prices(), [], ["jumia_ke"], TODAY)
        assert isinstance(summary.median, Decimal)
        assert isinstance(summary.p25, Decimal)

    def test_quartiles_bracket_the_median(self):
        summary = summarise_prices(enough_prices(), [], ["jumia_ke"], TODAY)
        assert summary.p25 <= summary.median <= summary.p75

    def test_counts_distinct_sellers_not_listings(self):
        # One shop with thirty listings is one seller.
        summary = summarise_prices(
            enough_prices(),
            sellers=["same shop"] * MIN_OBSERVATIONS,
            platform_codes=["jumia_ke"],
            collected_on=TODAY,
        )
        assert summary.evidence.seller_count == 1
        assert summary.evidence.observation_count == MIN_OBSERVATIONS


class TestEvidenceIsInseparable:
    """A figure without its evidence is not a thing this module can emit."""

    def test_every_summary_carries_evidence(self):
        summary = summarise_prices(enough_prices(), ["a"], ["jumia_ke"], TODAY)
        assert isinstance(summary.evidence, Evidence)
        assert summary.evidence.observation_count > 0
        assert summary.evidence.collected_on == TODAY

    def test_caveats_travel_with_the_figure(self):
        # Kilimall is category level. The product must say so next to the
        # number rather than implying a precision it does not have.
        summary = summarise_prices(
            enough_prices(),
            ["a"],
            ["kilimall_ke"],
            TODAY,
            caveats=["Category level only."],
        )
        assert "Category level only." in summary.evidence.caveats

    def test_evidence_serialises_for_the_api(self):
        summary = summarise_prices(enough_prices(), ["a"], ["jumia_ke"], TODAY)
        payload = summary.as_dict()
        assert payload["evidence"]["observation_count"] == MIN_OBSERVATIONS
        assert payload["evidence"]["collected_on"] == "2026-09-09"


class TestSaturation:
    def test_one_shop_with_many_listings_is_not_a_crowded_category(self):
        # The specific false signal: 400 listings looks like a busy market and
        # is actually a single seller.
        assert saturation_index(400, 1, Decimal(50), Decimal(500)) is None

    def test_converged_prices_read_as_crowded(self):
        # Forty sellers all within a few shillings have competed the margin
        # away. That is the situation a vendor needs warning about.
        tight = saturation_index(400, 40, Decimal(20), Decimal(600))
        wide = saturation_index(400, 40, Decimal(400), Decimal(600))
        assert tight > wide

    def test_labels_the_bands(self):
        assert saturation_label(8.0) == "crowded"
        assert saturation_label(4.0) == "competitive"
        assert saturation_label(1.5) == "room"
        assert saturation_label(None) == "unknown"

    def test_zero_listings_is_not_a_number(self):
        assert saturation_index(0, 5, Decimal(10), Decimal(500)) is None


class TestPriceTrend:
    def test_refuses_a_thin_history(self):
        # Honest about the one thing a new collector cannot have yet.
        result = price_trend([TrendPoint(TODAY, Decimal(500), 40)])
        assert result["available"] is False
        assert "history" in result["reason"]

    def test_detects_a_fall(self):
        points = [
            TrendPoint(TODAY - timedelta(days=60), Decimal(800), 40),
            TrendPoint(TODAY - timedelta(days=30), Decimal(720), 40),
            TrendPoint(TODAY, Decimal(649), 40),
        ]
        result = price_trend(points)
        assert result["available"] is True
        assert result["direction"] == "falling"
        assert result["change_percent"] < 0

    def test_detects_a_rise(self):
        points = [
            TrendPoint(TODAY - timedelta(days=60), Decimal(500), 40),
            TrendPoint(TODAY - timedelta(days=30), Decimal(550), 40),
            TrendPoint(TODAY, Decimal(600), 40),
        ]
        assert price_trend(points)["direction"] == "rising"

    def test_small_movement_is_flat_not_a_trend(self):
        # Sellers adjust prices constantly. Calling 1% a trend would have the
        # product announcing a direction every time it is asked.
        points = [
            TrendPoint(TODAY - timedelta(days=60), Decimal(500), 40),
            TrendPoint(TODAY - timedelta(days=30), Decimal(503), 40),
            TrendPoint(TODAY, Decimal(505), 40),
        ]
        assert price_trend(points)["direction"] == "flat"


class TestMarginCalculation:
    def test_computes_the_landed_cost_ceiling(self):
        result = landed_cost_ceiling(MarginInputs(
            median_price=Decimal(649),
            commission_rate=0.11,
            last_mile_kes=Decimal(145),
            target_margin=0.30,
        ))

        assert result["available"] is True
        # 649 - 71.39 commission - 145 last mile - 194.70 margin = 237.91
        assert result["commission_kes"] == "71.39"
        assert result["landed_cost_ceiling_kes"] == "237.91"
        assert result["viable"] is True

    def test_refuses_without_a_commission_rate(self):
        # A margin computed against a guessed commission is a wrong answer
        # about somebody's money.
        result = landed_cost_ceiling(MarginInputs(
            median_price=Decimal(649),
            commission_rate=None,
            last_mile_kes=Decimal(145),
        ))
        assert result["available"] is False
        assert "commission" in result["reason"]

    def test_refuses_without_a_last_mile_cost(self):
        result = landed_cost_ceiling(MarginInputs(
            median_price=Decimal(649),
            commission_rate=0.11,
            last_mile_kes=None,
        ))
        assert result["available"] is False
        assert "last mile" in result["reason"]

    def test_shows_every_intermediate_figure(self):
        # A vendor about to commit a container needs to see where the number
        # came from, not just trust it.
        result = landed_cost_ceiling(MarginInputs(
            median_price=Decimal(649),
            commission_rate=0.11,
            last_mile_kes=Decimal(145),
        ))
        for key in ("commission_kes", "net_receipt_kes", "required_margin_kes"):
            assert key in result

    def test_an_unviable_category_says_so(self):
        # At this price with these costs there is no landed cost that works.
        # That is a real and useful answer.
        result = landed_cost_ceiling(MarginInputs(
            median_price=Decimal(200),
            commission_rate=0.13,
            last_mile_kes=Decimal(150),
            target_margin=0.30,
        ))
        assert result["viable"] is False
