"""Forgiving lookup tests.

A vendor types "powerbanks", "power-bank" or "pwoer bank" and means the same
thing. A search that only answers the spelling we happened to write in a
config file is a search that tells a real user they are wrong.

The other half of the job matters just as much: a fuzzy match must never be
applied silently. "Showing results for power banks" is honest; quietly
answering about a different product than the vendor typed is the failure this
product exists to prevent, and it would be invisible to them.
"""

import pytest

from soko.search import (
    edit_distance,
    find_category,
    find_county,
    find_platform,
    fold,
    strip_stopwords,
    suggest_categories,
)


class TestFolding:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Power Bank", "power bank"),
            ("POWER BANK", "power bank"),
            ("power-bank", "power bank"),
            ("  power   bank  ", "power bank"),
            ("Power_Bank!!", "power bank"),
            ("power/bank", "power bank"),
        ],
    )
    def test_folds_to_one_form(self, raw, expected):
        assert fold(raw) == expected

    def test_hyphens_become_spaces_not_nothing(self):
        # "power-bank" must reach the two-word alias, not a one-word one.
        assert fold("power-bank") == "power bank"

    def test_handles_none_and_empty(self):
        assert fold(None) == ""
        assert fold("") == ""


class TestStopwords:
    def test_strips_question_words(self):
        assert strip_stopwords("what do power banks cost") == "power banks"

    def test_never_returns_nothing(self):
        # An all-stopword query keeps its words rather than becoming empty,
        # because an empty query matches everything.
        assert strip_stopwords("what is the") != ""


class TestExactAndAlias:
    @pytest.mark.parametrize(
        "typed",
        ["power bank", "power banks", "powerbank", "powerbanks",
         "power-bank", "POWER BANKS", "Power Bank"],
    )
    def test_every_spelling_of_power_bank(self, typed):
        match = find_category(typed)
        assert match is not None
        assert match.code == "power_banks"
        assert match.certain

    def test_the_kenyan_decoder_spelling(self):
        # "decorder" is how sellers actually write it.
        match = find_category("decorder")
        assert match.code == "tv_accessories"


class TestMisspellings:
    @pytest.mark.parametrize(
        "typed,expected",
        [
            ("pwoer bank", "power_banks"),
            ("erbuds", "audio"),
            ("kisumo", "kisumu"),
        ],
    )
    def test_typos_still_reach_the_right_thing(self, typed, expected):
        finder = find_county if expected == "kisumu" else find_category
        match = finder(typed)
        assert match is not None
        assert match.code == expected

    def test_a_typo_is_flagged_as_uncertain(self):
        # The interface shows "did you mean" rather than answering silently.
        match = find_category("pwoer bank")
        assert match.how == "fuzzy"
        assert not match.certain

    def test_an_exact_match_is_certain(self):
        assert find_category("power bank").certain

    def test_nonsense_matches_nothing(self):
        # Fuzzy matching must not stretch to reach anything at all.
        assert find_category("zzzqqxwv") is None


class TestPartial:
    @pytest.mark.parametrize(
        "typed,expected",
        [
            ("phone cases", "phone_accessories"),
            ("laptop", "computing"),
            ("tv remot", "tv_accessories"),
        ],
    )
    def test_partial_names_resolve(self, typed, expected):
        assert find_category(typed).code == expected


class TestCountiesAndPlatforms:
    @pytest.mark.parametrize(
        "typed", ["nairobi", "Nairobi", "nairobi county", "NAIROBI"]
    )
    def test_county_variants(self, typed):
        assert find_county(typed).code == "nairobi"

    @pytest.mark.parametrize(
        "typed,expected",
        [("jumia", "jumia_ke"), ("Jumia Kenya", "jumia_ke"),
         ("kilimall", "kilimall_ke"), ("kilimal", "kilimall_ke"),
         ("glovo", "glovo_ke")],
    )
    def test_platform_variants(self, typed, expected):
        assert find_platform(typed).code == expected


class TestEditDistance:
    def test_identical_is_zero(self):
        assert edit_distance("power", "power") == 0

    def test_one_typo(self):
        assert edit_distance("powre", "power") <= 2

    def test_abandons_early_beyond_the_cap(self):
        # The cap is what keeps this cheap across a whole taxonomy.
        assert edit_distance("a", "abcdefghij", cap=3) > 3


class TestSuggestions:
    def test_offers_near_misses(self):
        # A dead end that lists what we do have beats one that only says no.
        assert suggest_categories("powr bnk")

    def test_returns_nothing_for_nonsense(self):
        assert suggest_categories("") == []
