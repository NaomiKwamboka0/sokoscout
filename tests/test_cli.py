"""CLI integration tests, run against a seeded store rather than a live site.

The regression class at the bottom guards a bug found during the first real
end to end run: a question naming an unrecognised category answered with a
different category's median, in a confident sentence with a citation attached.
That is the precise failure this product exists to avoid, and it was invisible
in unit tests because every layer was behaving correctly on its own.
"""

import json
from datetime import date
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from soko.cli import _vocabularies, app
from soko.answer import route
from soko.pipeline import JsonlStore
from soko.sources.base import Listing

runner = CliRunner()
TODAY = date(2026, 9, 9)


def seed(path, category_titles: dict[str, tuple[str, int, int]]):
    """Write a store with the given per category counts."""
    listings = []
    for title_stem, (count, low, high) in category_titles.items():
        for i in range(count):
            listings.append(Listing(
                platform_code="jumia_ke",
                source_key=f"{title_stem[:8]}-{i}",
                source_url=f"https://www.jumia.co.ke/x{i}.html",
                title=f"{title_stem} {i}",
                price_kes=Decimal(low + (high - low) * i // max(count - 1, 1)),
                observed_on=TODAY,
                seller_name=f"Seller {i % 12}",
            ))
    store = JsonlStore(path)
    store.write(listings)
    return store


@pytest.fixture
def store_path(tmp_path):
    path = tmp_path / "run.jsonl"
    seed(path, {
        "Power Bank 20000mAh Fast Charging": (45, 1500, 4500),
        "Silicone Phone Case Cover": (60, 250, 1200),
        # Deliberately below the threshold, to test refusal.
        "Matte Lipstick Long Lasting": (12, 300, 2200),
    })
    return str(path)


def last_json(result) -> dict:
    """Every command's last line is a JSON summary."""
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestStatsAndRollup:
    def test_stats_reports_counts(self, store_path):
        result = runner.invoke(app, ["stats", "--in", store_path])
        payload = last_json(result)
        assert payload["ok"] is True
        assert payload["observations"] == 117

    def test_rollup_states_figures_for_thick_categories(self, store_path):
        result = runner.invoke(app, ["rollup", "--in", store_path])
        payload = last_json(result)
        assert payload["categories_with_figures"] == 2

    def test_rollup_names_thin_categories_rather_than_hiding_them(self, store_path):
        result = runner.invoke(app, ["rollup", "--in", store_path])
        assert last_json(result)["categories_too_thin"] == 1
        assert "12 of 30 observations" in result.stdout

    def test_stats_on_an_empty_store(self, tmp_path):
        result = runner.invoke(app, ["stats", "--in", str(tmp_path / "nothing.jsonl")])
        assert last_json(result)["observations"] == 0


class TestAsk:
    def test_answers_a_price_question(self, store_path):
        result = runner.invoke(app, ["ask", "What do phone cases sell for?", "--in", store_path])
        assert "median price for phone accessories" in result.stdout
        assert last_json(result)["answered"] is True

    def test_every_answer_carries_its_evidence(self, store_path):
        result = runner.invoke(app, ["ask", "What do phone cases cost?", "--in", store_path])
        assert "Based on" in result.stdout
        assert "listings" in result.stdout
        assert last_json(result)["evidence"]["observation_count"] == 60

    def test_refuses_the_demand_question(self, store_path):
        result = runner.invoke(app, ["ask", "What sells best in Kisumu?", "--in", store_path])
        assert "sales volume" in result.stdout
        assert last_json(result)["answered"] is False

    def test_refuses_a_thin_category(self, store_path):
        # Lipstick is in the store, but only 12 observations of it.
        result = runner.invoke(app, ["ask", "What does lipstick cost?", "--in", store_path])
        assert "too thin" in result.stdout
        assert "12 observations" in result.stdout
        assert last_json(result)["answered"] is False

    def test_no_question_triggers_a_crawl(self, store_path):
        # Answers come from what has already been collected. If asking could
        # crawl, a vendor's curiosity could get the project rate limited.
        result = runner.invoke(app, ["ask", "What do phone cases cost?", "--in", store_path])
        assert result.exit_code == 0
        # No network driver is constructed on this path at all.
        assert "blocked" not in result.stdout


class TestUnrecognisedCategoryRegression:
    """The bug found on the first end to end run.

    "What is the price of beauty products?" returned the phone accessories
    median, in a confident sentence, with a real citation attached. A wrong
    number carrying evidence is worse than no answer, because the evidence is
    what makes it believable.
    """

    def test_unknown_category_refuses_rather_than_substituting(self, store_path):
        result = runner.invoke(
            app, ["ask", "What is the price of helicopters?", "--in", store_path]
        )
        assert "could not tell which product category" in result.stdout
        assert last_json(result)["answered"] is False

    def test_unknown_category_does_not_quote_another_categorys_median(self, store_path):
        result = runner.invoke(
            app, ["ask", "What is the price of helicopters?", "--in", store_path]
        )
        # Naming the categories we do have is fine and useful. Quoting a
        # figure for one of them is the bug. So the assertion is about
        # figures, not about the category names appearing at all.
        assert "median price" not in result.stdout.lower()
        assert "KSh" not in result.stdout
        assert "Based on" not in result.stdout
        assert last_json(result)["answered"] is False

    def test_the_refusal_lists_what_it_can_answer_about(self, store_path):
        # A refusal that teaches, rather than a dead end.
        result = runner.invoke(
            app, ["ask", "What is the price of helicopters?", "--in", store_path]
        )
        assert "phone accessories" in result.stdout or "power banks" in result.stdout

    def test_beauty_is_addressable_by_its_common_name(self):
        # The original trigger: the alias was the full display name "Beauty
        # and Personal Care", so the ordinary word "beauty" matched nothing.
        categories, platforms, counties = _vocabularies()
        assert route("price of beauty", categories, platforms, counties).category_code == "beauty"

    @pytest.mark.parametrize(
        "question,expected",
        [
            ("price of beauty", "beauty"),
            ("price of audio", "audio"),
            ("what do thrift clothes cost", "thrift"),
            ("price of footwear", "footwear"),
            ("price of smartphones", "smartphones"),
        ],
    )
    def test_categories_are_addressable_by_their_plain_names(self, question, expected):
        categories, platforms, counties = _vocabularies()
        assert route(question, categories, platforms, counties).category_code == expected

    def test_saturation_may_still_span_all_categories(self):
        # "Which category is least saturated" is a legitimate question with no
        # single category, and must not be caught by the new guard.
        categories, platforms, counties = _vocabularies()
        routed = route("Which category is least saturated?", categories, platforms, counties)
        assert routed.answerable
        assert routed.category_code is None


class TestJsonSummaryContract:
    """Every command ends with one parseable JSON object, for automation."""

    @pytest.mark.parametrize("args", [
        ["stats"],
        ["rollup"],
        ["categories"],
    ])
    def test_last_line_is_json(self, args, store_path):
        if args[0] in {"stats", "rollup"}:
            args = args + ["--in", store_path]
        result = runner.invoke(app, args)
        payload = last_json(result)
        assert "ok" in payload
        assert payload["command"] == args[0]

    def test_ask_emits_json_even_when_refusing(self, store_path):
        result = runner.invoke(app, ["ask", "What sells best?", "--in", store_path])
        payload = last_json(result)
        assert payload["ok"] is True
        assert payload["answered"] is False
