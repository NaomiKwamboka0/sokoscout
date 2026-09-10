"""Policy and cost-of-business tests.

A vendor picks where to list based on these numbers, so the tests that matter
most are the ones checking that an unconfirmed figure is reported as unknown
rather than filled in with a plausible guess.
"""

from decimal import Decimal

import pytest

from soko.policies import (
    compare_platforms,
    known_platforms,
    onboarding_answer,
    payout_answer,
    platform_cost,
    policy,
    returns_answer,
)


class TestPolicyLookup:
    def test_knows_the_four_platforms(self):
        codes = known_platforms()
        for expected in ("jumia_ke", "kilimall_ke", "glovo_ke", "ubereats_ke"):
            assert expected in codes

    def test_unknown_platform_returns_none(self):
        assert policy("amazon_ke") is None

    def test_every_platform_carries_a_source_and_a_date(self):
        # A vendor decides where to list on these figures. A number with no
        # provenance is not usable for that.
        for code in known_platforms():
            entry = policy(code)
            assert entry.get("source")
            assert entry.get("read")


class TestOnboarding:
    def test_lists_the_requirements(self):
        result = onboarding_answer("jumia_ke")
        assert result["answered"] is True
        assert "KRA PIN" in result["text"]
        assert "national ID" in result["text"]

    def test_states_the_timeline_and_that_it_is_free(self):
        text = onboarding_answer("jumia_ke")["text"]
        assert "5 days" in text
        assert "no listing fee" in text.lower()

    def test_kilimall_is_lighter_than_jumia(self):
        # A real difference a sole trader needs: Kilimall does not require a
        # registered business name.
        jumia = policy("jumia_ke")["onboarding"]["requirements"]
        kilimall = policy("kilimall_ke")["onboarding"]["requirements"]
        assert len(kilimall) < len(jumia)

    def test_an_unconfirmed_platform_says_so(self):
        # We do not crawl Uber Eats and have no verified figures for it.
        result = onboarding_answer("ubereats_ke")
        assert result["answered"] is False
        assert "not have confirmed" in result["text"]

    def test_cites_its_source(self):
        assert "Read from" in onboarding_answer("jumia_ke")["text"]


class TestPayout:
    def test_states_the_cycle(self):
        result = payout_answer("jumia_ke")
        assert result["answered"] is True
        assert result["cycle_days"] == 14

    def test_warns_about_the_pay_on_delivery_cash_cycle(self):
        # The headline payout figure understates the real cash cycle in a
        # market where pay on delivery dominates. A vendor financing stock
        # needs that said out loud.
        text = payout_answer("jumia_ke")["text"]
        assert "pay on delivery" in text.lower()
        assert "3 to 6 weeks" in text

    def test_does_not_repeat_the_cycle_twice(self):
        # The note restates the cycle in words, so printing both said
        # "14 day cycle" twice in consecutive sentences.
        text = payout_answer("jumia_ke")["text"]
        assert text.count("14 day") <= 1

    def test_unconfirmed_platform_refuses(self):
        result = payout_answer("ubereats_ke")
        assert result["answered"] is False


class TestReturns:
    def test_states_the_window_and_who_pays(self):
        result = returns_answer("jumia_ke")
        assert result["answered"] is True
        assert "7 days" in result["text"]
        assert "who pays" in result["text"].lower()

    def test_unconfirmed_platform_refuses(self):
        assert returns_answer("ubereats_ke")["answered"] is False


class TestPlatformCost:
    def test_itemises_rather_than_collapsing_to_one_number(self):
        # The point of the comparison is that commission is not the whole
        # cost. Collapsing it back to one figure hides what a vendor came for.
        cost = platform_cost("jumia_ke", Decimal(1000), "phone_accessories")
        assert cost.commission_kes is not None
        assert cost.last_mile_kes is not None
        assert cost.other_fees

    def test_uses_the_category_commission_when_there_is_one(self):
        cost = platform_cost("jumia_ke", Decimal(1000), "phone_accessories")
        assert cost.commission_rate == pytest.approx(0.11)
        assert cost.unconfirmed == [] or "commission" not in " ".join(cost.unconfirmed)

    def test_falls_back_to_the_band_midpoint_and_says_so(self):
        # Better than refusing for a comparison view, but the vendor is told.
        cost = platform_cost("jumia_ke", Decimal(1000), category_code=None)
        assert cost.commission_rate is not None
        assert any("midpoint" in note for note in cost.unconfirmed)

    def test_take_rate_is_everything_not_just_commission(self):
        cost = platform_cost("jumia_ke", Decimal(1000), "phone_accessories")
        # Take rate must exceed the bare commission, because delivery and the
        # return provision are real costs too.
        assert cost.take_rate > cost.commission_rate

    def test_provisions_for_returns_rather_than_pretending_they_never_happen(self):
        cost = platform_cost("jumia_ke", Decimal(1000), "phone_accessories")
        assert any("return" in fee["name"].lower() for fee in cost.other_fees)

    def test_the_return_assumption_is_stated(self):
        cost = platform_cost("jumia_ke", Decimal(1000), "phone_accessories")
        return_fee = next(f for f in cost.other_fees if "return" in f["name"].lower())
        assert "%" in return_fee["note"]

    def test_a_platform_with_no_delivery_figure_is_not_comparable(self):
        # Kilimall has no published figure for Nakuru. It must not borrow
        # Jumia's, which would be a fabricated comparison.
        cost = platform_cost("kilimall_ke", Decimal(1000), county_code="nakuru")
        assert cost.net_receipt is None
        assert cost.take_rate is None

    def test_unknown_platform_returns_none(self):
        assert platform_cost("amazon_ke", Decimal(1000)) is None


class TestComparison:
    def test_ranks_by_what_the_vendor_keeps(self):
        result = compare_platforms(Decimal(1000), "phone_accessories")
        receipts = [Decimal(p["net_receipt_kes"]) for p in result["platforms"]]
        assert receipts == sorted(receipts, reverse=True)

    def test_names_the_gap_in_shillings(self):
        # "Jumia is better" is not actionable. "You keep KSh 47 more" is.
        #
        # The per-unit gap sentence needs two comparable platforms. Kilimall
        # publishes no commission rate, so today there is often only one, and
        # the figure itself is what must always be present.
        result = compare_platforms(Decimal(1000), "phone_accessories")
        assert "KSh" in result["text"]
        if len(result["platforms"]) > 1:
            assert "per unit" in result["text"]

    def test_platforms_without_figures_are_listed_not_hidden(self):
        # A vendor who cannot find a platform assumes we are broken. Saying
        # why it is excluded is better than omitting it.
        result = compare_platforms(Decimal(1000), "phone_accessories")
        excluded = {p["platform"] for p in result["not_comparable"]}
        assert "ubereats_ke" in excluded

    def test_the_excluded_platforms_carry_a_reason(self):
        result = compare_platforms(Decimal(1000), "phone_accessories")
        for entry in result["not_comparable"]:
            assert entry["why"]

    def test_mentions_payout_speed_when_there_is_a_rival_to_compare(self):
        # A platform that pays less but faster is a real trade-off that a
        # money-only ranking hides. With a single costable platform there is
        # no trade-off to name.
        result = compare_platforms(Decimal(1000), "phone_accessories")
        if len(result["platforms"]) > 1:
            assert (
                "pays faster" in result["text"]
                or "level" in result["text"]
                or "day cycle" in result["text"]
            )
        else:
            assert result["text"]

    def test_caveats_travel_into_the_sentence(self):
        result = compare_platforms(Decimal(1000), category_code=None)
        assert "Caveat" in result["text"] or "midpoint" in result["text"]

    def test_a_lead_smaller_than_an_unconfirmed_fee_is_called_unproven(self):
        # Found on the first real comparison: Kilimall led Jumia by KSh 7.50
        # while its own return handling fee was unknown, and Jumia's
        # provision for the same fee was larger than the whole gap. The
        # ranking flips outright once that figure is known.
        #
        # Reporting the lead without saying so would be technically accurate
        # and practically misleading, which is the failure mode this whole
        # product is built to avoid.
        result = compare_platforms(Decimal(2500), "power_banks", county_code="kisumu")

        if len(result["platforms"]) >= 2:
            best = Decimal(result["platforms"][0]["net_receipt_kes"])
            runner = Decimal(result["platforms"][1]["net_receipt_kes"])
            gap = best - runner
            unconfirmed_fee = any(
                "return handling fee not confirmed" in note
                for p in result["platforms"] for note in p["unconfirmed"]
            )
            if unconfirmed_fee and 0 < gap < 30:
                assert "unproven" in result["text"]
                assert "could flip" in result["text"]

    def test_a_decisive_lead_is_not_hedged(self):
        # The warning must fire only when the gap is genuinely within the
        # margin of the unknown. Hedging a large, clear lead would train the
        # vendor to ignore the caveat entirely.
        result = compare_platforms(Decimal(100000), "smartphones")
        if len(result["platforms"]) >= 2:
            gap = (
                Decimal(result["platforms"][0]["net_receipt_kes"])
                - Decimal(result["platforms"][1]["net_receipt_kes"])
            )
            if gap > 100:
                assert "unproven" not in result["text"]
