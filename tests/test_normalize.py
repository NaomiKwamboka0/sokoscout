"""Price parsing tests.

Each case here is a form that appears on a real Kenyan marketplace listing.
The near misses at the bottom matter as much as the happy path: a parser that
accepts everything is how a rating of 4.5 becomes a price of four shillings
fifty and quietly drags a median down.
"""

from decimal import Decimal

import pytest

from soko.normalize import (
    clean_title,
    extract_specs,
    normalise_sku,
    parse_price,
    parse_price_range,
)


class TestParsePrice:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # The ordinary forms, as Jumia renders them.
            ("KSh 1,299", 1299),
            ("KSh1,299", 1299),
            ("Ksh 1299", 1299),
            ("KES 1,299", 1299),
            ("1,299", 1299),
            ("1299", 1299),
            # Kenyan shorthand. "/=" is near universal in informal listings.
            ("1,299/=", 1299),
            ("1299/-", 1299),
            ("2500 bob", 2500),
            ("KSh 45,000", 45000),
            # The case the proposal names explicitly: 1.5k is fifteen hundred.
            ("1.5k", 1500),
            ("1.5K", 1500),
            ("2k", 2000),
            ("15k", 15000),
            ("1.25k", 1250),
            # Whitespace and casing chaos from seller written text.
            ("  ksh   1,299  ", 1299),
            ("KSHS 1,299", 1299),
            ("1,299 shillings", 1299),
            # Large but real: a fridge or a television.
            ("KSh 189,999", 189999),
        ],
    )
    def test_reads_real_forms(self, raw, expected):
        assert parse_price(raw) == Decimal(expected)

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "   ",
            "Price on request",
            "Contact seller",
            "KSh",
            "free",
            # Below the plausible floor: this is a rating or a page number that
            # a looser parser would happily return as a price.
            "4.5",
            "1",
            "9",
            "KSh 5",
            # Above the ceiling: a decimal point went missing somewhere.
            "999,999,999",
        ],
    )
    def test_rejects_rather_than_guesses(self, raw):
        assert parse_price(raw) is None

    def test_range_takes_low_end(self):
        # A category page range. The low end is the conservative read for a
        # vendor working out whether they can compete.
        assert parse_price("KSh 500 - KSh 1,200") == Decimal(500)
        assert parse_price("KSh 500 to KSh 1,200") == Decimal(500)

    def test_returns_decimal_not_float(self):
        # Money that gets aggregated must not be binary floating point. A
        # median a vendor prices against cannot carry representation drift.
        result = parse_price("KSh 1,299")
        assert isinstance(result, Decimal)

    def test_thousands_separator_is_not_a_decimal_point(self):
        # The specific silent failure: "1,299" read as one point two nine nine.
        assert parse_price("1,299") == Decimal(1299)
        assert parse_price("1,299") != Decimal("1.299")


class TestParsePriceRange:
    def test_reads_both_ends(self):
        assert parse_price_range("KSh 500 - KSh 1,200") == (Decimal(500), Decimal(1200))

    def test_orders_reversed_ranges(self):
        assert parse_price_range("KSh 1,200 - KSh 500") == (Decimal(500), Decimal(1200))

    def test_single_price_becomes_a_point_range(self):
        assert parse_price_range("KSh 800") == (Decimal(800), Decimal(800))

    def test_unparseable_end_rejects_whole_range(self):
        # Half a range is not a range. Returning (500, 500) here would silently
        # narrow a category's spread and understate its dispersion.
        assert parse_price_range("KSh 500 - contact us") is None


class TestCleanTitle:
    def test_strips_promotional_padding(self):
        raw = "BRAND NEW Original 20000mAh Power Bank Fast Charging [FREE SHIPPING]"
        assert clean_title(raw) == "20000mah power bank fast charging"

    def test_strips_bracketed_segments(self):
        assert clean_title("Phone Case (Hot Sale!)") == "phone case"

    def test_handles_empty(self):
        assert clean_title(None) is None
        assert clean_title("") is None
        assert clean_title("   ") is None

    def test_is_deterministic(self):
        # The same listing must classify the same way on every run, or a
        # published median moves for no reason a vendor can see.
        raw = "Original Power Bank 10000mAh | Fast Delivery"
        assert clean_title(raw) == clean_title(raw)


class TestExtractSpecs:
    def test_reads_capacity(self):
        assert extract_specs("power bank 20000mah fast charging")["capacity_mah"] == 20000

    def test_reads_storage(self):
        specs = extract_specs("smartphone 128gb 6gb ram")
        assert specs["storage_gb"] == 128

    def test_reads_screen_size(self):
        assert extract_specs('smart tv 43 inch led')["screen_inch"] == 43

    def test_absent_specs_are_absent_not_zero(self):
        # A zero would enter arithmetic as a real measurement. An absent key
        # cannot.
        assert "capacity_mah" not in extract_specs("phone case silicone")


class TestNormaliseSku:
    def test_stable_across_calls(self):
        first = normalise_sku("jumia_ke", "GE983EL1KQ4XLNAFAMZ")
        second = normalise_sku("jumia_ke", "GE983EL1KQ4XLNAFAMZ")
        assert first == second

    def test_namespaced_by_platform(self):
        # The same SKU string on two platforms is two products. Without the
        # namespace they would collapse into one and merge two price series.
        assert normalise_sku("jumia_ke", "ABC123") != normalise_sku("kilimall_ke", "ABC123")

    def test_rejects_empty(self):
        assert normalise_sku("jumia_ke", None) is None
        assert normalise_sku("jumia_ke", "") is None
        assert normalise_sku("jumia_ke", "!!!") is None
