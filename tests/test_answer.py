"""Answer engine tests.

The sample questions from the proposal's appendix are all here, including the
one it says the product must refuse. If "what sells best in Kisumu" ever
returns a figure, this suite has failed at its main job.
"""

import pytest

from soko.answer import (
    Intent,
    TEMPLATES,
    assemble,
    refuse,
    route,
)

CATEGORIES = {
    "phone_accessories": ["phone case", "phone cover", "phone accessories"],
    "power_banks": ["power bank", "powerbank", "portable charger"],
    "audio": ["earbuds", "wireless earbuds", "headphones"],
    "beauty": ["beauty", "cosmetics", "makeup"],
}

PLATFORMS = {
    "jumia_ke": ["jumia"],
    "kilimall_ke": ["kilimall"],
    "glovo_ke": ["glovo"],
}

COUNTIES = {
    "nairobi": ["nairobi"],
    "kisumu": ["kisumu"],
    "mombasa": ["mombasa"],
}


def r(question: str):
    return route(question, CATEGORIES, PLATFORMS, COUNTIES)


class TestProposalSampleQuestions:
    """Every question in appendix A2, routed as the proposal says it should be."""

    def test_price_question(self):
        result = r("What does a wireless earbud sell for on Jumia Kenya?")
        assert result.intent is Intent.PRICE_LEVEL
        assert result.platform_code == "jumia_ke"

    def test_saturation_question(self):
        # The proposal's phrasing, "which category is least saturated", is an
        # open question across all categories rather than a question about a
        # named one, so it routes to OPPORTUNITY. Both intents answer it from
        # the same saturation figures; OPPORTUNITY ranks every category,
        # SATURATION reports on the one the vendor named.
        result = r("Which category is least saturated right now?")
        assert result.intent is Intent.OPPORTUNITY
        assert result.answerable

    def test_saturation_of_a_named_category(self):
        result = r("Is the earbuds category crowded?")
        assert result.intent is Intent.SATURATION
        assert result.category_code == "audio"

    def test_margin_question(self):
        result = r("If I import phone cases, what margin can I expect?")
        assert result.intent is Intent.MARGIN
        assert result.category_code == "phone_accessories"

    def test_platform_specific_question(self):
        result = r("Is it worth entering beauty on Kilimall?")
        assert result.platform_code == "kilimall_ke"
        assert result.category_code == "beauty"

    def test_the_question_the_product_must_refuse(self):
        # "What sells best in Kisumu?" The proposal is explicit: we observe
        # listings and prices, not sales volume.
        result = r("What sells best in Kisumu?")
        assert not result.answerable
        assert "sales volume" in result.refusal

    def test_the_refusal_explains_what_we_can_do_instead(self):
        # A refusal that teaches the vendor what the product is for.
        result = r("What sells best in Kisumu?")
        assert "what we can tell you" in result.refusal.lower()


class TestIntentRouting:
    @pytest.mark.parametrize(
        "question,expected",
        [
            ("What is the median price of a power bank?", Intent.PRICE_LEVEL),
            ("How much do earbuds go for?", Intent.PRICE_LEVEL),
            ("How many sellers list phone cases?", Intent.SELLER_COUNT),
            ("Is the earbuds category crowded?", Intent.SATURATION),
            ("What has the price done since June?", Intent.TREND),
            ("What margin can I expect on power banks?", Intent.MARGIN),
            ("Compare phone cases across platforms", Intent.COMPARE),
            ("What commission does Jumia take on beauty?", Intent.COMMISSION),
            ("Can Jumia deliver to Kisumu?", Intent.COVERAGE),
            ("What are the seller onboarding requirements?", Intent.POLICY),
        ],
    )
    def test_routes(self, question, expected):
        assert r(question).intent is expected

    def test_margin_beats_price_when_both_words_appear(self):
        # "What margin can I expect if I import phone cases at this price"
        # contains a price question inside a margin question. Order matters.
        result = r("At what price would I still make a margin on phone cases?")
        assert result.intent is Intent.MARGIN

    def test_trend_beats_price(self):
        result = r("What has the price of earbuds done over the last quarter?")
        assert result.intent is Intent.TREND


class TestDeterminism:
    """The same question must always route the same way."""

    def test_repeated_routing_is_identical(self):
        question = "What does a wireless earbud sell for on Jumia?"
        results = {r(question).intent for _ in range(20)}
        assert len(results) == 1

    def test_routing_does_not_depend_on_case(self):
        assert r("WHAT IS THE PRICE OF EARBUDS").intent is r(
            "what is the price of earbuds"
        ).intent


class TestParameterExtraction:
    def test_extracts_category(self):
        assert r("price of power banks").category_code == "power_banks"

    def test_longest_alias_wins(self):
        # "phone case" must beat a bare "phone" if both are aliases.
        assert r("what do phone cases cost").category_code == "phone_accessories"

    def test_extracts_platform(self):
        assert r("earbuds price on kilimall").platform_code == "kilimall_ke"

    def test_extracts_county(self):
        assert r("can they deliver to mombasa").county_code == "mombasa"

    @pytest.mark.parametrize(
        "phrase,days",
        [
            ("over the last 30 days", 30),
            ("over the last 2 weeks", 14),
            ("over the last 3 months", 90),
            ("last quarter", 90),
            ("last year", 365),
        ],
    )
    def test_extracts_time_window(self, phrase, days):
        assert r(f"what has the earbuds price done {phrase}").window_days == days

    def test_unknown_category_is_none_not_a_guess(self):
        # Nothing supplies an unvalidated value to the query layer. A category
        # we do not know is None, and the caller decides what to do about it.
        assert r("what is the price of a helicopter").category_code is None


class TestRefusals:
    @pytest.mark.parametrize(
        "question",
        [
            # Every one of these asks what MOVED, which nobody publishes and
            # for which there is no honest partial answer.
            "What sells best in Nairobi?",
            "What is the top selling product?",
            "How many units sold last month?",
            "What are the sales figures for earbuds?",
            "Which product moves the fastest?",
        ],
    )
    def test_sales_volume_questions_are_refused(self, question):
        result = r(question)
        assert not result.answerable
        assert "sales volume" in result.refusal

    @pytest.mark.parametrize(
        "question",
        [
            "What is most popular on Jumia?",
            "What is the most searched product in Nakuru?",
            "Which are the busiest categories?",
        ],
    )
    def test_popularity_questions_get_a_caveated_answer(self, question):
        # These used to be refused alongside sales-volume questions. There is
        # an honest partial answer to them: what sellers are listing. The
        # answer says in the sentence that assortment is not search volume,
        # so the caveat is carried rather than dropped. A refusal that could
        # have been a caveated answer is a worse product.
        result = r(question)
        assert result.answerable
        assert result.intent is Intent.ACTIVITY

    def test_the_refusal_points_at_what_we_can_answer(self):
        # A refusal that teaches is worth more than one that stops.
        refusal = r("What sells best in Nairobi?").refusal
        assert "busiest" in refusal or "concentrated" in refusal

    @pytest.mark.parametrize(
        "question",
        [
            "Will I make money selling phone cases?",
            "Is it a good idea to import earbuds?",
        ],
    )
    def test_forecast_questions_are_refused(self, question):
        result = r(question)
        assert not result.answerable
        assert "forecast" in result.refusal

    def test_competitor_cost_questions_are_refused(self):
        result = r("Who supplies the top phone case seller?")
        assert not result.answerable

    def test_unroutable_question_offers_the_nearest_thing(self):
        # Where routing cannot place a question, the product says so and says
        # what it can answer. A better failure than a fluent guess.
        result = r("asdfgh qwerty")
        assert not result.answerable
        assert "prices" in result.refusal

    def test_empty_question(self):
        assert not route("").answerable
        assert not route("   ").answerable


class TestAnswerAssembly:
    """The mechanism that keeps a model from producing a figure."""

    EVIDENCE = {
        "observation_count": 1284,
        "seller_count": 212,
        "platforms": ["jumia_ke"],
        "collected_on": "2026-09-08",
        "caveats": [],
    }

    def test_the_number_in_the_sentence_is_the_number_given(self):
        answer = assemble(
            Intent.PRICE_LEVEL,
            {
                "category_name": "phone cases",
                "platform_name": "Jumia Kenya",
                "median": "649",
                "p25": "450",
                "p75": "890",
            },
            self.EVIDENCE,
        )
        assert "KSh 649" in answer.text
        assert "KSh 450" in answer.text
        assert "KSh 890" in answer.text

    def test_every_answer_carries_its_citation(self):
        answer = assemble(
            Intent.PRICE_LEVEL,
            {
                "category_name": "phone cases",
                "platform_name": "Jumia Kenya",
                "median": "649",
                "p25": "450",
                "p75": "890",
            },
            self.EVIDENCE,
        )
        assert "1,284 listings" in answer.text
        assert "212 sellers" in answer.text
        assert "2026-09-08" in answer.text

    def test_caveats_appear_in_the_answer(self):
        evidence = dict(self.EVIDENCE)
        evidence["platforms"] = ["kilimall_ke"]
        evidence["caveats"] = ["Category level only, coarser than Jumia."]

        answer = assemble(
            Intent.PRICE_LEVEL,
            {
                "category_name": "beauty",
                "platform_name": "Kilimall",
                "median": "585",
                "p25": "400",
                "p75": "800",
            },
            evidence,
        )
        assert "Category level only" in answer.text

    def test_a_missing_template_raises_rather_than_improvising(self):
        # A missing template is a bug to fix in one place, not a case to paper
        # over by generating prose about the figures.
        with pytest.raises(KeyError, match="No template"):
            assemble(Intent.UNKNOWN, {}, self.EVIDENCE)

    def test_every_answerable_intent_has_a_template(self):
        # A route that cannot be answered is a route that should not exist.
        answerable = {
            Intent.PRICE_LEVEL,
            Intent.SELLER_COUNT,
            Intent.SATURATION,
            Intent.TREND,
            Intent.MARGIN,
            Intent.COMMISSION,
        }
        assert answerable <= set(TEMPLATES)

    def test_margin_answer_matches_the_proposal_mockup(self):
        # The screen in section 3 of the proposal, reproduced exactly.
        answer = assemble(
            Intent.MARGIN,
            {
                "category_name": "phone cases",
                "median_price": "649",
                "commission_percent": 11,
                "last_mile_kes": "145",
                "landed_cost_ceiling_kes": "310",
                "target_percent": 30,
            },
            self.EVIDENCE,
        )
        assert "KSh 649" in answer.text
        assert "KSh 310" in answer.text
        assert "30% gross margin" in answer.text


class TestRefusalHelper:
    def test_refusal_is_marked_unanswered(self):
        answer = refuse("Too thin.")
        assert answer.answered is False
        assert answer.refusal_reason == "Too thin."

    def test_refusal_serialises(self):
        payload = refuse("Too thin.").as_dict()
        assert payload["answered"] is False
