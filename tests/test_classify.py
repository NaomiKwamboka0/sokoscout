"""Classification tests.

The canonical case from the proposal is here: a power bank, a powerbank, a
phone charger and a portable charger are one product and must land in one
category, or the median for that category is an average of four different
things and means nothing.
"""

import pytest

from soko.classify import classify, commission_rate, is_aggregatable


class TestTheFourNamesProblem:
    """One product, four names. This is the case the proposal names as a moat."""

    @pytest.mark.parametrize(
        "title",
        [
            "Power Bank 20000mAh Fast Charging",
            "Powerbank 20000mAh Fast Charging",
            "Portable Charger 20000mAh",
            "Phone Charger Portable 20000mAh",
            "POWER BANK 20000MAH",
            "power-bank 20000mah",
        ],
    )
    def test_all_four_names_land_in_one_category(self, title):
        assert classify(title)["category_code"] == "power_banks"


class TestKenyanVocabulary:
    """Terms a classifier built elsewhere would not know."""

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Mtumba Dresses Bale Grade A", "thrift"),
            ("Sufuria Set 5 Pieces Non Stick", "home_kitchen"),
            ("Kabambe Phone Dual Sim", "smartphones"),
            ("Ngoma Sneakers Size 42", "footwear"),
            ("Kitenge Fabric 6 Yards", "fashion_womens"),
            ("Jiko Koa Charcoal Cooker", "home_kitchen"),
            ("Leso Pair Cotton", "fashion_womens"),
        ],
    )
    def test_local_terms_classify(self, title, expected):
        assert classify(title)["category_code"] == expected


class TestOrdinaryCases:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Wireless Earbuds Bluetooth 5.0", "audio"),
            ("iPhone Silicone Phone Case Black", "phone_accessories"),
            ("Tempered Glass Screen Protector", "phone_accessories"),
            ("Samsung 43 Inch Smart TV", "electronics_home"),
            ("Ladies Bodycon Dress Size M", "fashion_womens"),
            ("Mens Official Shirt Slim Fit", "fashion_mens"),
            ("Matte Lipstick Long Lasting", "beauty"),
        ],
    )
    def test_classifies(self, title, expected):
        assert classify(title)["category_code"] == expected


class TestRefusal:
    """What the classifier does when it does not know.

    An unclassified listing is excluded from every aggregate. That is correct:
    a listing we cannot identify contributes only noise to a median, and a
    wrong bucket is worse than a smaller sample.
    """

    @pytest.mark.parametrize(
        "title",
        [
            None,
            "",
            "Assorted Items Job Lot",
            "Miscellaneous Goods",
            "Item 4738",
        ],
    )
    def test_unknown_stays_unknown(self, title):
        result = classify(title)
        assert result["category_code"] is None
        assert result["confidence"] == "none"

    def test_unclassified_is_not_aggregatable(self):
        assert not is_aggregatable(classify("Assorted Items"))

    def test_no_silent_default_category(self):
        # The predecessor's specific bug, in its price-data form: falling back
        # to a default category labelled unrelated things as that category and
        # moved its median.
        assert classify("Random Unmatched Product")["category_code"] is None


class TestServiceListings:
    """A service priced per hour has no place in a product median."""

    def test_service_is_flagged(self):
        result = classify("Salon Booking Braiding Hair Appointment")
        assert result["is_service"] is True
        assert result["confidence"] == "low"

    def test_service_is_not_aggregatable(self):
        result = classify("Massage Spa Consultation Session")
        assert not is_aggregatable(result)


class TestDeterminism:
    """The property a vendor is actually paying for."""

    def test_same_input_same_output(self):
        title = "Power Bank 20000mAh Fast Charging Original"
        first = classify(title)
        second = classify(title)
        assert first == second

    def test_tie_break_is_stable(self):
        # A listing matching two categories equally must not oscillate between
        # them across runs. Whichever wins, it must always win.
        title = "Phone Case and Power Bank Combo Offer"
        results = {classify(title)["category_code"] for _ in range(10)}
        assert len(results) == 1


class TestConfidence:
    def test_multiple_hits_raise_confidence(self):
        # Two independent keyword hits is better evidence than one.
        strong = classify("Wireless Earbuds Bluetooth Headphones")
        weak = classify("Earbuds")
        assert strong["confidence"] == "high"
        assert weak["confidence"] == "medium"

    def test_both_bands_are_aggregatable(self):
        assert is_aggregatable(classify("Wireless Earbuds Bluetooth Headphones"))
        assert is_aggregatable(classify("Earbuds"))


class TestAccessoryGuard:
    """An accessory FOR a thing is not the thing.

    From the first live run: "Universal Charging Plug For Kids Toy Cars"
    matched the toys keyword "kids toy", because that phrase appears inside
    "Kids Toy Cars". It is a charger, and it put a 2,499 shilling charger
    into the toys median. Keyword matching alone cannot tell an item apart
    from an accessory for that item.
    """

    @pytest.mark.parametrize(
        "title",
        [
            "Universal Charging Plug For Kids Toy Cars",
            "Long Lasting Power Charger For Kids Ride On Car",
            "12V Charger For Kids Ride On Car Battery",
            "12V Power Adapter For Kids Car Battery",
            "Replacement Charger For Kids Ride On 12V Batteries",
        ],
    )
    def test_a_charger_for_a_toy_is_a_charger(self, title):
        assert classify(title)["category_code"] == "power_banks"

    def test_the_toy_itself_is_still_a_toy(self):
        # The mirror failure: over-correcting and sending real toys away.
        assert classify("Construction truck with music and lights")["category_code"] == "toys_games"
        assert classify("Uno Card Game For Family Fun")["category_code"] == "toys_games"

    def test_it_rescues_listings_no_keyword_matched(self):
        # "12V Charger For Kids Ride On Car Battery" matches no category
        # keyword at all, but is unambiguously a charger.
        result = classify("12V Charger For Kids Ride On Car Battery")
        assert result["category_code"] == "power_banks"
        assert result["classified_by"] == "accessory_guard"

    def test_a_case_for_a_phone_is_a_phone_accessory(self):
        assert classify("Silicone Case For Samsung S24")["category_code"] == "phone_accessories"

    def test_exempt_categories_are_never_redirected(self):
        # power_banks sells chargers as its main product. Redirecting it on
        # the word "charger" would be the mirror image of the bug.
        result = classify("Oraimo Power Bank 20000mAh Fast Charger")
        assert result["category_code"] == "power_banks"
        assert result["classified_by"] == "keyword"

    def test_a_redirect_never_claims_high_confidence(self):
        # A correction is not a confident classification.
        result = classify("Universal Charging Plug For Kids Toy Cars")
        assert result["confidence"] == "medium"

    def test_it_does_not_invent_a_category_from_nothing(self):
        # The guard only fires when an accessory marker is actually present.
        # It is not a fallback to a default category.
        assert classify("Assorted Job Lot Items")["category_code"] is None


class TestAccessoryGuardRestraint:
    """Found by adversarial review of the guard's first draft.

    That draft redirected on any accessory word with no restraint at all,
    which sent a sofa cover and a guitar case into the phone accessories
    median. That is the corrupt-median failure the product exists to prevent,
    and it was introduced by the fix for a different corrupt-median failure.
    """

    def test_a_sofa_cover_is_not_a_phone_accessory(self):
        assert classify("Sofa Cover For 3 Seater")["category_code"] != "phone_accessories"

    def test_a_guitar_case_is_not_a_phone_accessory(self):
        assert classify("Guitar Case For Acoustic")["category_code"] != "phone_accessories"

    def test_a_camera_case_is_not_a_phone_accessory(self):
        assert classify("Hard Case For Camera DSLR")["category_code"] != "phone_accessories"

    def test_a_qualified_marker_still_fires_with_context(self):
        # "case for" plus a phone brand is unambiguous and must still work.
        result = classify("Silicone Case For Samsung S24")
        assert result["category_code"] == "phone_accessories"

    def test_a_confident_keyword_winner_is_not_overridden(self):
        # Two or more keyword hits means the title said what it was more than
        # once. A single accessory word must not outvote that.
        assert classify("Dog Bed Washable Cover")["category_code"] == "pet_supplies"

    def test_markers_are_word_bounded_at_both_ends(self):
        # Without a trailing boundary a marker fires inside longer words.
        result = classify("Supercharged Performance Sneakers")
        assert result["category_code"] != "power_banks"

    def test_the_original_bug_stays_fixed(self):
        # The guard must still do the job it was added for.
        assert classify(
            "Universal Charging Plug For Kids Toy Cars"
        )["category_code"] == "power_banks"

    def test_an_unclassifiable_accessory_stays_unclassified(self):
        # A sofa cover matches no category keyword. Returning None is the
        # correct answer: excluding it costs a sample of one, while guessing
        # corrupts whichever median it is guessed into.
        assert classify("Sofa Cover For 3 Seater")["category_code"] is None


class TestAmbiguousKeywords:
    """Keywords broad enough to pull unrelated products into a median.

    Found by review: a bare "adapter" in power_banks matched HDMI adapters,
    SATA adapters and Bluetooth audio adapters. Three different categories of
    product, all landing in one median, all invisible.
    """

    @pytest.mark.parametrize(
        "title",
        [
            "HDMI Adapter For Monitor",
            "SATA to USB Adapter Hard Drive",
            "Bluetooth Audio Adapter Receiver",
        ],
    )
    def test_an_adapter_for_something_else_is_not_a_charger(self, title):
        assert classify(title)["category_code"] != "power_banks"

    @pytest.mark.parametrize(
        "title",
        ["Travel Adapter UK to EU Plug", "Power Adapter 65W", "Wall Adapter Fast Charging"],
    )
    def test_genuine_power_adapters_still_classify(self, title):
        # Narrowing the keyword must not lose the real thing.
        assert classify(title)["category_code"] == "power_banks"


class TestConditionOverrides:
    """Condition beats product type, because it dominates price.

    Found by review: a thrift listing names its garments repeatedly and its
    condition once, so hit counting sent wholesale bales into the retail
    womenswear median. Those two prices are nothing alike.
    """

    @pytest.mark.parametrize(
        "title",
        [
            "Camera Bale Dresses Skirts Blouse",
            "Mtumba Dresses Bale Grade A",
            "Second Hand Shoes Sneakers Boots",
            "Preloved Ladies Tops and Skirts",
        ],
    )
    def test_thrift_wins_over_the_garments_it_contains(self, title):
        assert classify(title)["category_code"] == "thrift"

    @pytest.mark.parametrize(
        "title",
        ["Ladies Bodycon Dress Size M", "Mens Official Shirt Slim Fit"],
    )
    def test_ordinary_retail_is_unaffected(self, title):
        # The override must not swallow everything that mentions clothing.
        assert classify(title)["category_code"] != "thrift"


class TestBreadcrumbPrecedence:
    def test_a_clean_title_beats_a_wrong_breadcrumb(self):
        # Platform taxonomies are broad and sometimes wrong. The title said
        # "earbuds" and that is stronger evidence than a navigation path.
        result = classify(
            "Wireless Earbuds Bluetooth",
            breadcrumb="Home > Computing > Laptop Accessories",
        )
        assert result["category_code"] == "audio"


class TestExpandedTaxonomy:
    """Categories added after the first live run found the original eleven
    classified 0 of 12 real listings."""

    @pytest.mark.parametrize(
        "title,expected",
        [
            # The most common real item in the sample: 8 of 45 listings.
            ("Tv remote", "tv_accessories"),
            ("Digital Decorder Remote", "tv_accessories"),   # Kenyan spelling
            ("DECORDER", "tv_accessories"),
            ("Alto saxophone reed", "musical_instruments"),
            ("3pcs High Waist Seamless Lace Panties", "innerwear"),
            ("Leather Dog Muzzles", "pet_supplies"),
            ("Tennis Racket adult", "sports_outdoor"),
            ("EliteBook 840 G3 Replacement Battery Laptop Battery", "computing"),
            ("5-Tier Storage Shelving Unit Heavy Duty Storage Rack", "storage_furniture"),
            ("200 KG Digital Hanging Scale with Accurate Sensors", "tools_hardware"),
        ],
    )
    def test_real_catalogue_items_classify(self, title, expected):
        assert classify(title)["category_code"] == expected

    def test_the_kenyan_decoder_spelling_is_covered(self):
        # "decorder" is near-universal in Kenyan listings and would be a
        # spelling mistake to correct rather than to match.
        assert classify("Digital Decorder")["category_code"] == "tv_accessories"


class TestCommissionRate:
    def test_returns_published_rate(self):
        assert commission_rate("phone_accessories") == pytest.approx(0.11)

    def test_unknown_category_returns_none_not_a_default(self):
        # A margin computed against a guessed commission is a wrong answer
        # about somebody's money. The product must say it does not know.
        assert commission_rate("not_a_category") is None
        assert commission_rate(None) is None
