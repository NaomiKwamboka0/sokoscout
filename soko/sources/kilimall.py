"""Kilimall Kenya collector.

Their robots.txt is unusually permissive at the top ("Allow all user agents")
and then disallows specific paths. The important detail, checked against the
live file rather than assumed:

    Disallow: /product*        Disallow: /item*
    Disallow: /new/goods*      Disallow: /api/*

Every one of those looks like a product path, and a first reading suggests
Kilimall does not want its catalogue crawled at all. It does not. Their
canonical product URL is `/listing/<id>-<slug>`, which is not disallowed, and
they publish 102 sitemaps of exactly those URLs from their own sitemap index.
A platform listing URLs in its own sitemap is asking for them to be read.

So the rule this driver enforces is narrow and literal: `/listing/` is
allowed, the four disallowed prefixes are refused, and query strings are
dropped. `path_allowed` in base.py does the checking from config, so the
policy lives in YAML rather than here.

Pages carry clean JSON-LD: an ItemPage wrapping a Product, with price,
currency, availability, breadcrumb categories and even the shipping rate.
That is a better source than scraped markup because it is what Kilimall
themselves publish for search engines, so it changes far less often than the
page layout does.
"""

from __future__ import annotations

import gzip
import json
import re
from datetime import date
from decimal import Decimal
from typing import Any, Iterator

import httpx

from soko.normalize import parse_price
from soko.sources.base import (
    BlockedError,
    Listing,
    RateLimiter,
    RunStats,
    collection_settings,
    path_allowed,
    platform_entry,
)

PLATFORM = "kilimall_ke"


class KilimallDriver:
    """Collects Kilimall Kenya listings from their published sitemaps."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        entry = platform_entry(PLATFORM)
        settings = collection_settings()
        access = entry["access"]

        self.base_url = entry["base_url"]
        self.sitemap_index = access.get("sitemap_index")
        self.limiter = RateLimiter(access.get("delay_seconds", 2.0))
        self.stats = RunStats(platform_code=PLATFORM)

        self._owns_client = client is None
        self.client = client or httpx.Client(
            headers={
                # Identify honestly and carry a contact URL. A crawler that
                # hides what it is cannot be asked to slow down, only blocked.
                "User-Agent": settings["user_agent"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
                "Accept-Language": "en-KE,en;q=0.9",
            },
            timeout=settings["request_timeout_seconds"],
            follow_redirects=True,
        )

    # -- fetching ---------------------------------------------------------

    def _decode(self, response: httpx.Response) -> str:
        """Text of a sitemap, decompressing a gzipped file if that is what it is.

        Jumia taught us this one: a `.xml.gz` read as text yields mojibake
        with no <loc> elements, which is indistinguishable from an empty
        sitemap and fails silently. Kilimall serves plain XML today, but the
        check costs nothing and the failure it prevents is invisible.
        """
        body = response.content
        if body[:2] == b"\x1f\x8b":
            try:
                return gzip.decompress(body).decode("utf-8", errors="replace")
            except (OSError, EOFError):
                pass
        return response.text

    def _fetch(self, url: str) -> httpx.Response | None:
        self.limiter.wait()
        self.stats.urls_seen += 1
        try:
            response = self.client.get(url)
        except httpx.HTTPError:
            self.stats.urls_failed += 1
            return None

        if response.status_code in (403, 429):
            # A block stops the run. Continuing to request after a platform
            # has said stop turns a throttle into a ban.
            self.stats.blocked += 1
            raise BlockedError(
                f"{PLATFORM} returned {response.status_code} for {url}. Stopping."
            )
        if response.status_code != 200:
            self.stats.urls_failed += 1
            return None

        self.stats.urls_ok += 1
        return response

    # -- sitemap walking --------------------------------------------------

    def _child_sitemaps(self) -> list[str]:
        """Product sitemaps from the index, products first.

        Jumia's index sorted brand sitemaps ahead of product ones, so walking
        it in order spent the whole limit on URLs robots disallowed and
        collected nothing. Ordering explicitly rather than trusting the
        index's own order is cheap insurance against the same failure here.
        """
        response = self._fetch(self.sitemap_index)
        if response is None:
            return []

        locations = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", self._decode(response))
        products = [u for u in locations if "product" in u.lower()]
        return products or locations

    def product_urls(self, limit: int = 50) -> Iterator[str]:
        """Product URLs, up to `limit`, skipping anything robots refuses."""
        yielded = 0
        for sitemap in self._child_sitemaps():
            if yielded >= limit:
                return

            response = self._fetch(sitemap)
            if response is None:
                continue

            for url in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", self._decode(response)):
                if yielded >= limit:
                    return
                if not path_allowed(PLATFORM, url):
                    continue
                yield url
                yielded += 1

    # -- parsing ----------------------------------------------------------

    def _structured(self, html: str) -> dict[str, Any] | None:
        """The Product object out of the page's JSON-LD.

        Kilimall wraps it in an ItemPage, so the Product is one level down.
        Preferring their published structured data over scraped markup means
        a layout change does not break collection.
        """
        for block in re.finditer(
            r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", html, re.S
        ):
            try:
                data = json.loads(block.group(1).strip())
            except json.JSONDecodeError:
                continue

            for candidate in (data if isinstance(data, list) else [data]):
                if not isinstance(candidate, dict):
                    continue
                if candidate.get("@type") == "Product":
                    return candidate
                inner = candidate.get("mainEntity")
                if isinstance(inner, dict) and inner.get("@type") == "Product":
                    return inner
        return None

    def _breadcrumb(self, html: str) -> str | None:
        """The category trail, which is a stronger signal than a seller title."""
        for block in re.finditer(
            r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", html, re.S
        ):
            try:
                data = json.loads(block.group(1).strip())
            except json.JSONDecodeError:
                continue
            for candidate in (data if isinstance(data, list) else [data]):
                if isinstance(candidate, dict) and candidate.get("@type") == "BreadcrumbList":
                    names = [
                        item.get("name")
                        for item in candidate.get("itemListElement", [])
                        if isinstance(item, dict) and item.get("name")
                    ]
                    if names:
                        return " > ".join(str(n) for n in names)
        return None

    def _blocked_page(self, html: str) -> bool:
        """A challenge page served with HTTP 200.

        The worst kind of block, because the status code says everything is
        fine. Without this check a run looks healthy while collecting
        nothing.
        """
        if len(html) < 800:
            lowered = html.lower()
            return any(
                marker in lowered
                for marker in ("captcha", "access denied", "are you a robot",
                               "verify you are human", "cf-browser-verification")
            )
        return False

    def parse_product(self, html: str, url: str, observed_on: date) -> Listing | None:
        """One listing from one page, or None if it is not usable."""
        if self._blocked_page(html):
            raise BlockedError(f"{PLATFORM} served a challenge page for {url}")

        product = self._structured(html)
        if not product:
            return None

        title = (product.get("name") or "").strip()
        if not title:
            return None

        offers = product.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if not isinstance(offers, dict):
            offers = {}

        price = parse_price(str(offers.get("price", "")))
        if price is None:
            return None

        # Only Kenyan shillings. A figure in another currency entering a KES
        # median would move it silently, which is the failure this product
        # exists to prevent.
        currency = (offers.get("priceCurrency") or "KES").upper()
        if currency not in ("KES", "KSH"):
            return None

        sku = str(product.get("sku") or "").strip()
        if not sku:
            match = re.search(r"/listing/(\d+)", url)
            sku = match.group(1) if match else url

        availability = str(offers.get("availability") or "")
        seller = product.get("brand")
        seller_name = (
            seller.get("name") if isinstance(seller, dict) else seller
        ) or None
        # "Kilimall" as a brand means unbranded, not a seller name.
        if seller_name and seller_name.strip().lower() == "kilimall":
            seller_name = None

        rating = None
        aggregate = product.get("aggregateRating")
        if isinstance(aggregate, dict):
            try:
                rating = Decimal(str(aggregate.get("ratingValue")))
            except Exception:
                rating = None

        return Listing(
            platform_code=PLATFORM,
            source_key=f"{PLATFORM}:{sku}",
            source_url=url,
            title=title,
            price_kes=price,
            observed_on=observed_on,
            seller_name=seller_name,
            in_stock="InStock" in availability or "instock" in availability.lower(),
            rating=rating,
            breadcrumb=self._breadcrumb(html),
            raw={
                "sku": sku,
                "currency": currency,
                # Their published shipping rate, which is a real input to a
                # margin rather than an estimate we would otherwise guess.
                "shipping": (offers.get("shippingDetails") or {})
                            .get("shippingRate", {})
                            .get("value"),
            },
        )

    # -- driver contract --------------------------------------------------

    def collect(self, limit: int = 50, observed_on: date | None = None) -> Iterator[Listing]:
        """Yield listings, up to `limit`."""
        day = observed_on or date.today()

        for url in self.product_urls(limit=limit):
            response = self._fetch(url)
            if response is None:
                continue

            listing = self.parse_product(response.text, url, day)
            if listing is not None:
                self.stats.rows_written += 1
                yield listing

        # Deliberately does not raise on an empty harvest here. The caller
        # applies guard_not_empty, because only the caller knows whether this
        # was a full run or a deliberately narrow one, and the same rule is
        # already enforced there for Jumia.

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
