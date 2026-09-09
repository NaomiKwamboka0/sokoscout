"""Jumia Kenya. The primary source, and the only one giving product level
prices at volume.

Sitemap driven, because that is what the platform publishes to be crawled.
Keyword search and facet URLs are disallowed by their robots file and are not
used, which is also why this driver walks a sitemap rather than doing the
obvious thing of searching for the categories we care about.

Product pages carry a JSON-LD block with the price, the seller and the rating.
That is a published, structured, machine readable description of the page, and
reading it is both more reliable and less work for their servers than parsing
the rendered HTML.
"""

from __future__ import annotations

import gzip
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Iterator

import httpx
from selectolax.parser import HTMLParser

from soko.normalize import normalise_sku, parse_price
from soko.sources.base import (
    BlockedError,
    Listing,
    RateLimiter,
    RunStats,
    collection_settings,
    path_allowed,
    platform_entry,
)

PLATFORM = "jumia_ke"

# Bot protection frequently returns 200 with a challenge page rather than an
# error status. Detecting that as a block rather than as an empty product is
# the difference between a run that stops and a run that writes nonsense.
_CHALLENGE_MARKERS = (
    "captcha",
    "are you a robot",
    "access denied",
    "cf-browser-verification",
    "checking your browser",
)


class JumiaDriver:
    """Sitemap driven collector for Jumia Kenya."""

    platform_code = PLATFORM

    def __init__(self, client: httpx.Client | None = None) -> None:
        entry = platform_entry(PLATFORM)
        settings = collection_settings()
        access = entry["access"]

        self.base_url = entry["base_url"]
        self.sitemaps = access.get("sitemaps") or []
        self.limiter = RateLimiter(access.get("delay_seconds", 1.5))
        self.stats = RunStats(platform_code=PLATFORM)

        self._owns_client = client is None
        self.client = client or httpx.Client(
            headers={
                # Identify honestly. A crawler that lies about who it is has
                # no standing to claim it was reading published data.
                "User-Agent": settings["user_agent"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
                "Accept-Language": "en-KE,en;q=0.9",
            },
            timeout=settings["request_timeout_seconds"],
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "JumiaDriver":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- fetching ----------------------------------------------------------

    def _fetch(self, url: str) -> str | None:
        """One polite GET. Returns None on a recoverable failure.

        Raises BlockedError on a block, because a block is not a per URL
        problem to skip past. Continuing to hammer a platform that has just
        told us to stop is exactly the behaviour that turns a temporary
        throttle into a permanent ban.
        """
        if not path_allowed(PLATFORM, url):
            # Our own policy file refused this. Not an error: a refusal we
            # want to be able to point at.
            return None

        self.limiter.wait()
        self.stats.urls_seen += 1

        try:
            response = self.client.get(url)
        except httpx.HTTPError:
            self.stats.urls_failed += 1
            return None

        if response.status_code in (403, 429):
            self.stats.blocked += 1
            raise BlockedError(
                f"{PLATFORM} returned {response.status_code} for {url}. "
                f"Stopping this run rather than continuing to request."
            )

        if response.status_code != 200:
            self.stats.urls_failed += 1
            return None

        body = self._decode(response, url)
        lowered = body[:4000].lower()
        if any(marker in lowered for marker in _CHALLENGE_MARKERS):
            self.stats.blocked += 1
            raise BlockedError(
                f"{PLATFORM} served a challenge page for {url}. This returns "
                f"HTTP 200, so it must be detected by content or it is "
                f"indistinguishable from a product with no data."
            )

        self.stats.urls_ok += 1
        return body

    @staticmethod
    def _decode(response: Any, url: str) -> str:
        """Response body as text, decompressing a gzipped sitemap.

        httpx transparently handles Content-Encoding, but these sitemaps are
        gzip *files* served as a download rather than a gzip transfer
        encoding, so the bytes arrive still compressed. Reading .text on them
        yields mojibake with no <loc> elements in it, which is
        indistinguishable from an empty sitemap and would be reported as an
        empty catalogue.
        """
        raw = response.content if hasattr(response, "content") else None

        looks_gzipped = url.endswith(".gz") or (
            isinstance(raw, (bytes, bytearray)) and raw[:2] == b"\x1f\x8b"
        )
        if looks_gzipped and raw:
            try:
                return gzip.decompress(raw).decode("utf-8", errors="replace")
            except (OSError, EOFError, gzip.BadGzipFile):
                # Not actually gzip despite the name. Fall through to text.
                pass
        return response.text

    # -- sitemap walking ---------------------------------------------------

    def product_urls(self, limit: int) -> Iterator[str]:
        """Product page URLs from the published sitemaps.

        Sitemap indexes nest, so this walks one level down into child
        sitemaps. Yields lazily and stops at the limit, because there is no
        reason to download a whole index to use the first hundred entries.

        Two things here were learned from running this against the live site
        rather than from reading the documentation, and neither was visible in
        the fixture tests:

        The child sitemaps are gzipped. A plain text read of a .xml.gz body
        finds no <loc> elements and returns nothing, which looks exactly like
        an empty catalogue and would have tripped the empty-result guard with
        a misleading cause.

        The index lists 111 product sitemaps alongside brand and category
        ones, and sorts alphabetically, so brands come first. Walking the
        index in order spends the whole limit on sitemaps whose URLs robots
        disallows, and collects nothing at all.
        """
        seen = 0
        for sitemap_url in self.sitemaps:
            body = self._fetch(sitemap_url)
            if not body:
                continue

            child_maps = [c.strip() for c in
                          re.findall(r"<sitemap>.*?<loc>(.*?)</loc>.*?</sitemap>", body, re.S)]

            if child_maps:
                for child in self._prefer_products(child_maps):
                    if seen >= limit:
                        return
                    child_body = self._fetch(child)
                    if not child_body:
                        continue
                    for url in self._locs(child_body):
                        if seen >= limit:
                            return
                        if self._is_product_url(url):
                            seen += 1
                            yield url
            else:
                for url in self._locs(body):
                    if seen >= limit:
                        return
                    if self._is_product_url(url):
                        seen += 1
                        yield url

    @staticmethod
    def _prefer_products(child_maps: list[str]) -> list[str]:
        """Product sitemaps first, then anything not explicitly off limits.

        Brand sitemaps are dropped entirely rather than merely deprioritised:
        their contents are */brands/ URLs, which robots disallows, so fetching
        the sitemap at all is a request that can only produce URLs we are not
        allowed to use.
        """
        products = [c for c in child_maps if "products-sitemap" in c]
        others = [
            c for c in child_maps
            if "products-sitemap" not in c and "brands-sitemap" not in c
        ]
        return products + others

    @staticmethod
    def _locs(xml: str) -> list[str]:
        return [loc.strip() for loc in re.findall(r"<loc>(.*?)</loc>", xml, re.S)]

    @staticmethod
    def _is_product_url(url: str) -> bool:
        """Jumia product pages end in .html and are not category listings."""
        if not url.endswith(".html"):
            return False
        return path_allowed(PLATFORM, url)

    # -- parsing -----------------------------------------------------------

    def parse_product(self, html: str, url: str, today: date) -> Listing | None:
        """One product page to a Listing, or None if it is not readable.

        Prefers the page's own JSON-LD, which is a published structured
        description, over scraping rendered markup. Falls back to meta tags,
        because a missing structured block is common enough that refusing
        those pages would cost real coverage.
        """
        tree = HTMLParser(html)
        data = self._json_ld_product(tree)

        title = None
        price_raw = None
        seller = None
        brand = None
        rating = None
        rating_count = None
        sku = None
        in_stock = None

        if data:
            title = data.get("name")
            sku = data.get("sku") or data.get("mpn")
            brand = self._flatten(data.get("brand"), "name")

            offers = data.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price_raw = offers.get("price")
            availability = str(offers.get("availability") or "")
            if availability:
                in_stock = "InStock" in availability
            seller = self._flatten(offers.get("seller"), "name")

            rating_block = data.get("aggregateRating") or {}
            if rating_block:
                rating = self._as_float(rating_block.get("ratingValue"))
                rating_count = self._as_int(rating_block.get("reviewCount"))

        if not title:
            node = tree.css_first("meta[property='og:title']")
            title = node.attributes.get("content") if node else None
        if not title:
            node = tree.css_first("h1")
            title = node.text(strip=True) if node else None

        if price_raw is None:
            node = tree.css_first("[data-price]")
            price_raw = node.attributes.get("data-price") if node else None

        price = parse_price(str(price_raw) if price_raw is not None else None)

        # A product without a title or a readable price is not an observation.
        # Writing it with a null price would put a hole in the time series that
        # looks like a real gap in the market.
        if not title or price is None:
            return None

        source_key = normalise_sku(PLATFORM, sku or url)
        if not source_key:
            return None

        return Listing(
            platform_code=PLATFORM,
            source_key=source_key,
            source_url=url,
            title=title.strip(),
            price_kes=price,
            observed_on=today,
            seller_name=seller,
            brand=brand,
            breadcrumb=self._breadcrumb(tree),
            in_stock=in_stock,
            rating=rating,
            rating_count=rating_count,
            raw={
                "json_ld": data or None,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    @staticmethod
    def _json_ld_product(tree: HTMLParser) -> dict[str, Any] | None:
        """The Product block from the page's JSON-LD, if it has one.

        Pages carry several JSON-LD blocks (breadcrumbs, organisation, the
        product), sometimes wrapped in an @graph, so this looks through all of
        them rather than assuming the first is the one we want.
        """
        for node in tree.css("script[type='application/ld+json']"):
            try:
                payload = json.loads(node.text())
            except (json.JSONDecodeError, ValueError):
                continue

            for candidate in JumiaDriver._walk_ld(payload):
                if str(candidate.get("@type", "")).lower() == "product":
                    return candidate
        return None

    @staticmethod
    def _walk_ld(payload: Any) -> Iterator[dict[str, Any]]:
        if isinstance(payload, dict):
            if "@graph" in payload:
                for item in payload["@graph"] or []:
                    yield from JumiaDriver._walk_ld(item)
            else:
                yield payload
        elif isinstance(payload, list):
            for item in payload:
                yield from JumiaDriver._walk_ld(item)

    @staticmethod
    def _breadcrumb(tree: HTMLParser) -> str | None:
        crumbs = [n.text(strip=True) for n in tree.css("nav a, .brcbs a")]
        crumbs = [c for c in crumbs if c and c.lower() not in {"home", "jumia"}]
        return " > ".join(crumbs[:4]) if crumbs else None

    @staticmethod
    def _flatten(value: Any, key: str) -> str | None:
        if isinstance(value, dict):
            result = value.get(key)
            return str(result).strip() if result else None
        if isinstance(value, str):
            return value.strip() or None
        return None

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # -- the driver contract ----------------------------------------------

    def collect(self, limit: int = 100) -> Iterator[Listing]:
        """Yield up to `limit` listings collected today.

        Does not itself raise on an empty harvest: the caller applies
        guard_not_empty, because only the caller knows whether this was a
        full run or a deliberately narrow one.
        """
        today = date.today()
        for url in self.product_urls(limit):
            html = self._fetch(url)
            if not html:
                continue
            listing = self.parse_product(html, url, today)
            if listing is not None:
                self.stats.rows_written += 1
                yield listing
