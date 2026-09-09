"""Collector tests.

Everything here runs against fixture HTML rather than the live site. That is
not only about speed: a test suite that hits a real marketplace on every run
is a test suite that eventually gets the project blocked.

The cases that matter most are the failure ones. A collector that parses a
good page correctly is easy; a collector that notices it has been served a
challenge page with a 200 status is the one that saves a week.
"""

from datetime import date
from decimal import Decimal

import pytest

from soko.sources.base import (
    BlockedError,
    EmptyResultError,
    Listing,
    RateLimiter,
    guard_not_empty,
    path_allowed,
    platform_entry,
)
from soko.sources.jumia import JumiaDriver

TODAY = date(2026, 9, 9)


PRODUCT_HTML = """
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "Oraimo Power Bank 20000mAh Fast Charging",
  "sku": "OR983EL1KQ4XL",
  "brand": {"@type": "Brand", "name": "Oraimo"},
  "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.5", "reviewCount": "212"},
  "offers": {
    "@type": "Offer",
    "price": "2499",
    "priceCurrency": "KES",
    "availability": "https://schema.org/InStock",
    "seller": {"@type": "Organization", "name": "Oraimo Official Store"}
  }
}
</script>
</head><body>
<nav><a>Home</a><a>Electronics</a><a>Power Banks</a></nav>
<h1>Oraimo Power Bank 20000mAh Fast Charging</h1>
</body></html>
"""

# The page's JSON-LD is wrapped in an @graph alongside other blocks, which is
# how a lot of real pages are actually shaped.
GRAPH_HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"BreadcrumbList","itemListElement":[]},
  {"@type":"Organization","name":"Jumia"},
  {"@type":"Product","name":"Wireless Earbuds Bluetooth",
   "sku":"WE111","offers":{"@type":"Offer","price":"1299","availability":"InStock"}}
]}
</script>
</head><body><h1>Wireless Earbuds Bluetooth</h1></body></html>
"""

# No structured data at all. Falls back to meta and data attributes.
FALLBACK_HTML = """
<html><head><meta property="og:title" content="Silicone Phone Case Black">
</head><body>
<h1>Silicone Phone Case Black</h1>
<div data-price="450"></div>
</body></html>
"""

CHALLENGE_HTML = """
<html><head><title>Just a moment...</title></head>
<body><div class="cf-browser-verification">Checking your browser before accessing</div></body></html>
"""

NO_PRICE_HTML = """
<html><head></head><body><h1>Some Product With No Price</h1></body></html>
"""


class FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200,
                 content: bytes | None = None) -> None:
        self.text = text
        self.status_code = status_code
        # Mirrors httpx: .content is the raw bytes, which is what the gzip
        # path needs. Tests that pass gzipped bytes set this directly.
        self.content = content if content is not None else text.encode("utf-8")


class FakeClient:
    """Stands in for httpx.Client. Serves canned bodies by URL."""

    def __init__(self, routes: dict[str, FakeResponse], default: FakeResponse | None = None):
        self.routes = routes
        self.default = default or FakeResponse("", 404)
        self.requested: list[str] = []

    def get(self, url: str) -> FakeResponse:
        self.requested.append(url)
        return self.routes.get(url, self.default)

    def close(self) -> None:
        pass


@pytest.fixture
def driver():
    d = JumiaDriver(client=FakeClient({}))
    yield d
    d.close()


class TestParsing:
    def test_reads_structured_product_data(self, driver):
        listing = driver.parse_product(PRODUCT_HTML, "https://jumia.co.ke/p.html", TODAY)

        assert listing is not None
        assert listing.title == "Oraimo Power Bank 20000mAh Fast Charging"
        assert listing.price_kes == Decimal(2499)
        assert listing.seller_name == "Oraimo Official Store"
        assert listing.brand == "Oraimo"
        assert listing.rating == 4.5
        assert listing.rating_count == 212
        assert listing.in_stock is True

    def test_finds_product_inside_a_graph(self, driver):
        listing = driver.parse_product(GRAPH_HTML, "https://jumia.co.ke/e.html", TODAY)
        assert listing is not None
        assert listing.title == "Wireless Earbuds Bluetooth"
        assert listing.price_kes == Decimal(1299)

    def test_falls_back_when_no_structured_data(self, driver):
        listing = driver.parse_product(FALLBACK_HTML, "https://jumia.co.ke/c.html", TODAY)
        assert listing is not None
        assert listing.title == "Silicone Phone Case Black"
        assert listing.price_kes == Decimal(450)

    def test_keeps_the_raw_payload(self, driver):
        # Storing the payload is what lets extraction improve and re-run over
        # data already collected, without re-crawling anybody.
        listing = driver.parse_product(PRODUCT_HTML, "https://jumia.co.ke/p.html", TODAY)
        assert listing.raw["json_ld"]["sku"] == "OR983EL1KQ4XL"

    def test_a_page_with_no_price_is_not_an_observation(self, driver):
        # Writing this with a null price would put a hole in the time series
        # that reads like a real gap in the market.
        assert driver.parse_product(NO_PRICE_HTML, "https://jumia.co.ke/x.html", TODAY) is None

    def test_same_page_yields_the_same_key(self, driver):
        # Idempotence. A rerun must not grow a second arm on the price series.
        first = driver.parse_product(PRODUCT_HTML, "https://jumia.co.ke/p.html", TODAY)
        second = driver.parse_product(PRODUCT_HTML, "https://jumia.co.ke/p.html", TODAY)
        assert first.source_key == second.source_key


class TestBlockDetection:
    """The failure that is invisible without an explicit check."""

    def test_challenge_page_with_200_status_raises(self):
        url = "https://www.jumia.co.ke/thing.html"
        client = FakeClient({url: FakeResponse(CHALLENGE_HTML, 200)})
        driver = JumiaDriver(client=client)

        with pytest.raises(BlockedError, match="challenge page"):
            driver._fetch(url)

    @pytest.mark.parametrize("status", [403, 429])
    def test_refusal_status_raises(self, status):
        url = "https://www.jumia.co.ke/thing.html"
        client = FakeClient({url: FakeResponse("", status)})
        driver = JumiaDriver(client=client)

        with pytest.raises(BlockedError):
            driver._fetch(url)

    def test_block_stops_the_run_rather_than_skipping(self):
        # Continuing to request after a block turns a temporary throttle into
        # a permanent ban, so this must not be a per URL skip.
        url = "https://www.jumia.co.ke/thing.html"
        client = FakeClient({url: FakeResponse("", 429)})
        driver = JumiaDriver(client=client)

        with pytest.raises(BlockedError):
            driver._fetch(url)
        assert driver.stats.blocked == 1

    def test_ordinary_failure_is_skipped_not_raised(self):
        # A 404 on one product is not a reason to abandon the whole run.
        url = "https://www.jumia.co.ke/gone.html"
        client = FakeClient({url: FakeResponse("", 404)})
        driver = JumiaDriver(client=client)

        assert driver._fetch(url) is None
        assert driver.stats.urls_failed == 1


class TestEmptyResultGuard:
    """Rule 1: an empty harvest raises rather than passing silently."""

    def test_empty_raises(self):
        with pytest.raises(EmptyResultError, match="throttling"):
            guard_not_empty([], "jumia_ke", "sitemap section 3")

    def test_message_names_the_context(self):
        # "jumia returned nothing" is much less useful at 3am than knowing
        # which unit of work was running.
        with pytest.raises(EmptyResultError, match="sitemap section 3"):
            guard_not_empty([], "jumia_ke", "sitemap section 3")

    def test_non_empty_passes_through(self):
        rows = [
            Listing(
                platform_code="jumia_ke",
                source_key="k",
                source_url="u",
                title="t",
                price_kes=Decimal(100),
                observed_on=TODAY,
            )
        ]
        assert guard_not_empty(rows, "jumia_ke", "ctx") == rows


class TestPolicyCompliance:
    def test_disallowed_paths_are_refused(self):
        # Our own reading of their robots policy, kept as a reviewable
        # document rather than buried in request code.
        assert not path_allowed("jumia_ke", "https://www.jumia.co.ke/catalog/?q=shoes")
        assert not path_allowed("jumia_ke", "https://www.jumia.co.ke/customer/account")

    def test_product_pages_are_allowed(self):
        assert path_allowed("jumia_ke", "https://www.jumia.co.ke/oraimo-power-bank.html")

    def test_driver_does_not_fetch_disallowed_urls(self):
        client = FakeClient({})
        driver = JumiaDriver(client=client)

        assert driver._fetch("https://www.jumia.co.ke/catalog/?q=shoes") is None
        assert client.requested == []

    def test_a_delay_is_configured_even_though_none_is_declared(self):
        # Jumia declares no crawl delay. Being permitted to go faster is not
        # a reason to.
        entry = platform_entry("jumia_ke")
        assert entry["access"]["delay_seconds"] >= 1.0

    def test_user_agent_identifies_us(self):
        driver = JumiaDriver()
        agent = driver.client.headers["User-Agent"]
        driver.close()
        assert "SokoScout" in agent
        assert "http" in agent  # a contactable URL, not just a name


class TestRateLimiter:
    def test_first_call_does_not_wait(self):
        import time

        limiter = RateLimiter(0.05)
        start = time.monotonic()
        limiter.wait()
        assert time.monotonic() - start < 0.02

    def test_second_call_waits(self):
        import time

        limiter = RateLimiter(0.05)
        limiter.wait()
        start = time.monotonic()
        limiter.wait()
        assert time.monotonic() - start >= 0.04


class TestSitemapWalking:
    def test_extracts_product_urls(self):
        sitemap = """<?xml version="1.0"?><urlset>
        <url><loc>https://www.jumia.co.ke/power-bank.html</loc></url>
        <url><loc>https://www.jumia.co.ke/earbuds.html</loc></url>
        <url><loc>https://www.jumia.co.ke/catalog/?q=x</loc></url>
        </urlset>"""

        client = FakeClient({"https://static.jumia.co.ke/index-sitemap.xml": FakeResponse(sitemap)})
        driver = JumiaDriver(client=client)

        urls = list(driver.product_urls(limit=10))

        assert "https://www.jumia.co.ke/power-bank.html" in urls
        assert "https://www.jumia.co.ke/earbuds.html" in urls
        # The disallowed facet URL must not appear even though it is listed.
        assert not any("catalog" in u for u in urls)

    def test_respects_the_limit(self):
        entries = "".join(
            f"<url><loc>https://www.jumia.co.ke/p{i}.html</loc></url>" for i in range(50)
        )
        client = FakeClient(
            {"https://static.jumia.co.ke/index-sitemap.xml": FakeResponse(f"<urlset>{entries}</urlset>")}
        )
        driver = JumiaDriver(client=client)

        assert len(list(driver.product_urls(limit=5))) == 5

    def test_follows_a_sitemap_index(self):
        index = """<sitemapindex>
        <sitemap><loc>https://static.jumia.co.ke/sitemap-1.xml</loc></sitemap>
        </sitemapindex>"""
        child = """<urlset>
        <url><loc>https://www.jumia.co.ke/thing.html</loc></url>
        </urlset>"""

        client = FakeClient({
            "https://static.jumia.co.ke/index-sitemap.xml": FakeResponse(index),
            "https://static.jumia.co.ke/sitemap-1.xml": FakeResponse(child),
        })
        driver = JumiaDriver(client=client)

        assert list(driver.product_urls(limit=10)) == ["https://www.jumia.co.ke/thing.html"]


class TestLiveDiscoveredBehaviour:
    """Two bugs found by running the driver against the real site.

    Neither was visible in the fixture tests, because the fixtures encoded
    what the sitemap was assumed to look like. This is the case the proposal
    makes for running a collector against reality before building on it.
    """

    def test_gzipped_sitemaps_are_decompressed(self):
        # Jumia's child sitemaps are .xml.gz files served as downloads, so the
        # bytes arrive still compressed. Reading .text yields mojibake with no
        # <loc> in it, which is indistinguishable from an empty sitemap and
        # would be reported as an empty catalogue.
        import gzip

        xml = "<urlset><url><loc>https://www.jumia.co.ke/thing.html</loc></url></urlset>"
        index = ("<sitemapindex><sitemap><loc>"
                 "https://static.jumia.co.ke/products-sitemap-1.xml.gz"
                 "</loc></sitemap></sitemapindex>")

        client = FakeClient({
            "https://static.jumia.co.ke/index-sitemap.xml": FakeResponse(index),
            "https://static.jumia.co.ke/products-sitemap-1.xml.gz":
                FakeResponse(content=gzip.compress(xml.encode())),
        })
        driver = JumiaDriver(client=client)

        assert "https://www.jumia.co.ke/thing.html" in list(driver.product_urls(limit=10))

    def test_product_sitemaps_are_walked_before_others(self):
        # The index sorts alphabetically, so brands come first. Walking it in
        # order spends the whole limit on sitemaps whose URLs robots
        # disallows, and collects nothing at all.
        ordered = JumiaDriver._prefer_products([
            "https://static.jumia.co.ke/brands-sitemap-1.xml.gz",
            "https://static.jumia.co.ke/categories-sitemap.xml.gz",
            "https://static.jumia.co.ke/products-sitemap-1.xml.gz",
        ])
        assert "products-sitemap" in ordered[0]

    def test_brand_sitemaps_are_dropped_entirely(self):
        # Their contents are */brands/ URLs, which robots disallows, so
        # fetching the sitemap can only produce URLs we may not use.
        ordered = JumiaDriver._prefer_products([
            "https://static.jumia.co.ke/brands-sitemap-1.xml.gz",
            "https://static.jumia.co.ke/products-sitemap-1.xml.gz",
        ])
        assert not any("brands-sitemap" in u for u in ordered)


class TestRealRobotsPolicy:
    """The policy as their live robots.txt actually writes it.

    The first draft of the config guessed a prefix based policy. The real one
    is overwhelmingly query parameter based, and blocks seller and brand pages
    by path fragment rather than prefix.
    """

    def test_seller_pages_are_refused(self):
        assert not path_allowed("jumia_ke", "https://www.jumia.co.ke/shop/seller/acme/")

    def test_brand_pages_are_refused(self):
        assert not path_allowed("jumia_ke", "https://www.jumia.co.ke/brands/oraimo/")

    def test_facet_urls_are_refused(self):
        # Their file lists roughly two hundred facet parameters individually.
        # Any query string on a product URL is treated as one.
        assert not path_allowed(
            "jumia_ke", "https://www.jumia.co.ke/phones.html?color=black&capacity_mah=20000"
        )

    def test_the_allowed_specification_endpoints_are_permitted(self):
        # /catalog/ is not blanket disallowed: two endpoints under it are
        # explicitly allowed, and they are exactly the ones we want.
        assert path_allowed(
            "jumia_ke", "https://www.jumia.co.ke/catalog/productspecifications/sku/ABC/"
        )
        assert path_allowed(
            "jumia_ke", "https://www.jumia.co.ke/catalog/productratingsreviews/sku/ABC/"
        )

    def test_plain_product_pages_remain_allowed(self):
        assert path_allowed("jumia_ke", "https://www.jumia.co.ke/oraimo-power-bank-20000.html")

    def test_a_facet_on_an_allowed_page_is_still_refused(self):
        # The precedence bug, guarded. A faceted product URL matches the
        # "/*.html" allow rule, so checking allows before facets returns True
        # and walks straight into the URLs their robots file spends two
        # hundred lines asking us not to touch. The allow covers the product
        # page; it does not cover every parameterised view of it.
        assert path_allowed("jumia_ke", "https://www.jumia.co.ke/phones.html")
        assert not path_allowed("jumia_ke", "https://www.jumia.co.ke/phones.html?color=black")
        assert not path_allowed(
            "jumia_ke", "https://www.jumia.co.ke/phones.html?capacity_mah=20000&dir=asc"
        )

    def test_a_facet_on_an_allowed_catalog_endpoint_is_still_refused(self):
        assert not path_allowed(
            "jumia_ke",
            "https://www.jumia.co.ke/catalog/productspecifications/sku/ABC/?color=black",
        )

    def test_we_stay_well_under_their_published_rate_limit(self):
        # Their file permits 200 requests per minute. Being permitted to go
        # faster is not a reason to.
        access = platform_entry("jumia_ke")["access"]
        assert access["max_requests_per_minute"] <= 200
        assert access["delay_seconds"] >= 1.0


class TestCollectEndToEnd:
    def test_collects_listings_from_a_sitemap(self):
        sitemap = """<urlset>
        <url><loc>https://www.jumia.co.ke/power-bank.html</loc></url>
        </urlset>"""

        client = FakeClient({
            "https://static.jumia.co.ke/index-sitemap.xml": FakeResponse(sitemap),
            "https://www.jumia.co.ke/power-bank.html": FakeResponse(PRODUCT_HTML),
        })
        driver = JumiaDriver(client=client)

        listings = list(driver.collect(limit=5))

        assert len(listings) == 1
        assert listings[0].price_kes == Decimal(2499)
        assert driver.stats.rows_written == 1
