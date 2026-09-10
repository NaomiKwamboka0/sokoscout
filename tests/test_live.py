"""On-demand category fetching.

No test here touches the network. The parsers are fed fixture markup and the
fetch path is driven through a stubbed client, because a suite that reaches a
live marketplace is slow, flaky, and puts load on somebody else's site every
time anyone runs pytest.

The property that matters most is the last class: live fetching exists to get
a category TO thirty observations faster, and must never become a way for a
figure to escape below thirty. Fresher and honest are different problems.
"""

from datetime import date

import pytest

from soko import live
from soko.classify import classify

TODAY = date(2026, 9, 10)


JUMIA_PAGE = """
<article class="prd _box _hvr" data-ga4-item_id="PB123ABC"
         data-ga4-item_brand="Oraimo">
  <a class="core" href="/oraimo-power-bank-20000mah-mpg123.html">
    <div class="name">Oraimo Power Bank 20000mAh Fast Charging</div>
    <div class="prc">KSh 2,499</div>
  </a>
</article>
<article class="prd _box _hvr" data-ga4-item_id="PB456DEF"
         data-ga4-item_brand="">
  <a class="core" href="/amtec-43l12-tv-mpg456.html">
    <div class="name">Amtec 43L12, 43&quot; FHD Smart Android TV</div>
    <div class="prc">KSh 17,920</div>
  </a>
</article>
"""

KILIMALL_PAGE = """
<div class="listings">
<div class="product-item" data-v-f6ed5f40>
  <a href="/listing/2517665-Vitron-32-Inch-QLED-Smart-TV" target="_blank">
    <div class="product-image"><img lazy="loading"></div>
    <div class="info-box">
      <p class="product-title"><!----><!--[--><!--]--> Vitron 32 Inch QLED
      Smart TV Frameless HTC3288QS</p>
      <div class="product-price">KSh 11,699</div>
    </div>
  </a>
</div>
<div class="product-item" data-v-f6ed5f40>
  <a href="/listing/2517999-Oraimo-Power-Bank" target="_blank">
    <div class="info-box">
      <p class="product-title"><!----> Oraimo 20000mAh Power Bank</p>
      <div class="product-price">KSh 2,199</div>
    </div>
  </a>
</div>
</div>
"""


class TestJumiaParsing:
    def test_reads_products_from_a_category_page(self):
        found = live._parse_jumia_cards(JUMIA_PAGE, TODAY)
        assert len(found) == 2

    def test_carries_price_title_and_url(self):
        first = live._parse_jumia_cards(JUMIA_PAGE, TODAY)[0]
        assert str(first.price_kes) == "2499"
        assert "Power Bank" in first.title
        assert first.source_url.startswith("https://www.jumia.co.ke/")

    def test_decodes_html_entities_in_titles(self):
        # A live page carried 'Amtec 43L12, 43&quot; FHD Smart Android TV'.
        # Left encoded, the entity splits the words either side of it and the
        # classifier fails on a product it should recognise.
        second = live._parse_jumia_cards(JUMIA_PAGE, TODAY)[1]
        assert "&quot;" not in second.title
        assert '43"' in second.title
        assert classify(second.title)["category_code"] == "electronics_home"

    def test_uses_the_platform_sku_as_the_key(self):
        # Idempotence depends on this: the same product fetched live and by
        # the crawler must collide rather than double-count.
        first = live._parse_jumia_cards(JUMIA_PAGE, TODAY)[0]
        assert first.source_key == "jumia_ke:PB123ABC"

    def test_an_empty_page_yields_nothing_rather_than_raising(self):
        assert live._parse_jumia_cards("<html></html>", TODAY) == []


class TestKilimallParsing:
    def test_reads_products_from_a_search_page(self):
        found = live._parse_kilimall_cards(KILIMALL_PAGE, TODAY)
        assert len(found) == 2

    def test_takes_the_product_title_not_a_banner(self):
        # The first version scanned for the nearest price to a listing link
        # and pulled "Flash Sale" off a promotional banner as the title for
        # seven listings in a row, which would have fed the classifier
        # garbage and filed those prices under whatever the banner matched.
        titles = [l.title for l in live._parse_kilimall_cards(KILIMALL_PAGE, TODAY)]
        assert not any("Flash Sale" in t for t in titles)
        assert any("Vitron" in t for t in titles)

    def test_strips_framework_comment_markers(self):
        first = live._parse_kilimall_cards(KILIMALL_PAGE, TODAY)[0]
        assert "<!--" not in first.title
        assert not first.title.startswith(" ")

    def test_titles_still_classify(self):
        found = live._parse_kilimall_cards(KILIMALL_PAGE, TODAY)
        assert classify(found[0].title)["category_code"] == "electronics_home"
        assert classify(found[1].title)["category_code"] == "power_banks"

    def test_builds_a_reachable_product_url(self):
        first = live._parse_kilimall_cards(KILIMALL_PAGE, TODAY)[0]
        assert first.source_url.startswith("https://www.kilimall.co.ke/listing/")


class TestFreshness:
    def setup_method(self):
        live.reset_cache()

    def test_nothing_is_fresh_to_begin_with(self):
        assert not live.is_fresh("jumia_ke", "power_banks")

    def test_a_fetch_marks_the_category_fresh(self):
        live._mark_fetched("jumia_ke", "power_banks")
        assert live.is_fresh("jumia_ke", "power_banks")

    def test_freshness_is_per_category_and_per_platform(self):
        # One category going stale must not force a refetch of every other.
        live._mark_fetched("jumia_ke", "power_banks")
        assert not live.is_fresh("jumia_ke", "audio")
        assert not live.is_fresh("kilimall_ke", "power_banks")

    def test_freshness_expires(self):
        import time
        live._mark_fetched("jumia_ke", "power_banks")
        later = time.monotonic() + live.CACHE_SECONDS + 1
        assert not live.is_fresh("jumia_ke", "power_banks", now=later)


class TestConfiguredSources:
    def test_jumia_uses_category_paths(self):
        paths = live._category_paths("jumia_ke", "power_banks")
        assert paths
        assert all(p.startswith("https://www.jumia.co.ke/") for p in paths)

    def test_jumia_paths_never_carry_a_query_string(self):
        # Jumia disallows roughly two hundred query parameters, so a search
        # URL is not available to us there and a path with one would be a
        # policy violation rather than a bug.
        from soko.sources.base import path_allowed

        for category in ("power_banks", "footwear", "beauty"):
            for url in live._category_paths("jumia_ke", category):
                assert "?" not in url
                assert path_allowed("jumia_ke", url)

    def test_kilimall_falls_back_to_its_permitted_search(self):
        # Kilimall has no configured category paths, and its robots does not
        # disallow /search.
        urls = live._search_urls("kilimall_ke", ["power bank"])
        assert urls
        assert "search?q=" in urls[0]

    def test_search_terms_come_from_the_taxonomy(self):
        # So the query and the classifier agree by construction. Searching a
        # word the classifier does not know collects rows it then refuses.
        terms = live._search_terms("power_banks")
        assert terms
        assert any(classify(t)["category_code"] == "power_banks" for t in terms)

    def test_an_unmapped_category_offers_no_jumia_path(self):
        assert live._category_paths("jumia_ke", "not_a_category") == []


class StubResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.content = text.encode()


class StubClient:
    """Stands in for httpx, so the fetch path runs without a network."""

    def __init__(self, pages: dict[str, StubResponse] | None = None,
                 default: StubResponse | None = None):
        self.pages = pages or {}
        self.default = default or StubResponse(JUMIA_PAGE)
        self.requested: list[str] = []

    def get(self, url: str) -> StubResponse:
        self.requested.append(url)
        return self.pages.get(url, self.default)

    def close(self) -> None:
        pass


class TestFetching:
    def setup_method(self):
        live.reset_cache()

    def test_returns_what_it_parsed(self):
        report = live.fetch_category("jumia_ke", "power_banks", client=StubClient())
        assert report.found == 2
        assert report.pages >= 1

    def test_a_second_call_is_served_from_cache(self):
        client = StubClient()
        live.fetch_category("jumia_ke", "power_banks", client=client)
        again = live.fetch_category("jumia_ke", "power_banks", client=client)
        assert again.from_cache
        assert again.found == 0

    def test_force_overrides_the_cache(self):
        client = StubClient()
        live.fetch_category("jumia_ke", "power_banks", client=client)
        again = live.fetch_category(
            "jumia_ke", "power_banks", client=client, force=True
        )
        assert not again.from_cache

    def test_a_block_stops_the_fetch(self):
        # Continuing to request after a platform says stop turns a throttle
        # into a ban.
        client = StubClient(default=StubResponse("", status_code=429))
        report = live.fetch_category("jumia_ke", "power_banks", client=client)
        assert report.blocked
        assert report.found == 0

    def test_a_block_is_explained_rather_than_raised(self):
        # This runs inside a page load, so it degrades to stored data rather
        # than breaking the page.
        client = StubClient(default=StubResponse("", status_code=403))
        report = live.fetch_category("jumia_ke", "power_banks", client=client)
        assert "slow down" in (report.error or "")

    def test_it_never_requests_a_disallowed_url(self):
        client = StubClient()
        live.fetch_category("jumia_ke", "power_banks", client=client)
        from soko.sources.base import path_allowed
        assert all(path_allowed("jumia_ke", u) for u in client.requested)

    def test_it_stays_within_its_page_budget(self):
        # A user's click must never become a crawl.
        client = StubClient(default=StubResponse("<html></html>"))
        live.fetch_category("jumia_ke", "beauty", client=client)
        assert len(client.requested) <= live.MAX_PAGES

    def test_an_unmapped_category_says_so(self):
        report = live.fetch_category(
            "jumia_ke", "not_a_category", client=StubClient()
        )
        assert report.found == 0
        assert "cannot fetch it" in (report.error or "")


class TestSummary:
    def test_reports_what_was_found(self):
        report = live.FetchReport("jumia_ke", "Jumia Kenya", "power_banks", found=40)
        assert "Jumia Kenya" in live.summarise([report])
        assert "40" in live.summarise([report])

    def test_says_when_a_platform_asked_us_to_slow_down(self):
        report = live.FetchReport(
            "jumia_ke", "Jumia Kenya", "power_banks", blocked=True
        )
        assert "slow down" in live.summarise([report])

    def test_silent_when_nothing_happened(self):
        # A cached hit should not announce itself; there is nothing to say.
        report = live.FetchReport(
            "jumia_ke", "Jumia Kenya", "power_banks", from_cache=True
        )
        assert live.summarise([report]) is None


class TestItDoesNotLowerTheBar:
    """The property this whole module must not break.

    Live fetching makes a category reach thirty observations sooner. It is
    not a way for a figure to escape below thirty. Solving freshness must not
    quietly undo honesty.
    """

    def test_the_threshold_is_untouched(self):
        from soko.aggregate import MIN_OBSERVATIONS
        assert MIN_OBSERVATIONS == 30

    def test_a_live_fetch_that_finds_too_little_still_refuses(self):
        from decimal import Decimal
        from soko.aggregate import InsufficientData, summarise_prices

        # Two listings is what our fixture page yields. It must not produce a
        # median just because it arrived seconds ago.
        found = live._parse_jumia_cards(JUMIA_PAGE, TODAY)
        with pytest.raises(InsufficientData):
            summarise_prices(
                [l.price_kes for l in found],
                sellers=["Shop A", "Shop B"],
                platform_codes=["jumia_ke"],
                collected_on=TODAY,
            )
