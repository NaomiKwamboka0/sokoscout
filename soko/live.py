"""Fetch a category on demand, when a vendor asks for it.

The sitemap crawl walks the catalogue in whatever order the platform
published it, which is by recency or product id and never by category. So a
vendor searching "home electronics" could get nothing while the store held
hundreds of rows about other things. Waiting for a background crawl to
eventually reach their category is not an answer.

This module fetches the category they actually asked for, from the platform's
own category and search pages. One request returns about forty products
instead of one, which is what makes it fast enough to run inside a page load
and gentle enough to be worth doing.

Three restraints, because this runs on a user's click against somebody else's
server:

  bounded    a small number of requests per search, never a crawl
  cached     the same category is not re-fetched for a while
  polite     the platform's own rate limit, and a block ends it immediately

What this does NOT do is lower the evidence bar. Live fetching gets a
category TO thirty observations faster; it does not let a figure out below
thirty. Fresher data and honest data are different problems, and solving the
first must not quietly undo the second.
"""

from __future__ import annotations

import html as html_lib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable
from urllib.parse import quote

import httpx

from soko.normalize import parse_price
from soko.sources.base import (
    BlockedError,
    Listing,
    collection_settings,
    path_allowed,
    platform_entry,
)

# How long a category stays fresh before another live fetch is allowed.
# Marketplace prices do not move minute to minute, and re-fetching on every
# page load would be rude to the platform and slow for the vendor.
CACHE_SECONDS = 15 * 60

# Requests per platform per search. A page of results carries roughly forty
# products, so three pages is enough to clear the thirty-observation floor
# while staying far below anything that looks like a crawl.
MAX_PAGES = 2

# Enough listings to clear the thirty-observation floor with room for the
# ones that will not classify. Reaching this ends the fetch early.
ENOUGH = 45

# Give up rather than make the vendor wait. A comparison that takes half a
# minute is a comparison nobody runs twice.
DEADLINE_SECONDS = 30


@dataclass
class FetchReport:
    """What a live fetch actually did, for showing the vendor."""

    platform_code: str
    platform_name: str
    requested: str
    found: int = 0
    pages: int = 0
    seconds: float = 0.0
    from_cache: bool = False
    blocked: bool = False
    error: str | None = None
    listings: list[Listing] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform_code,
            "platform_name": self.platform_name,
            "found": self.found,
            "pages": self.pages,
            "seconds": round(self.seconds, 1),
            "from_cache": self.from_cache,
            "blocked": self.blocked,
            "error": self.error,
        }


# Per-category, per-platform freshness. Process-local, which is the right
# scope for a prototype: it becomes a table when the app runs on more than
# one machine.
_LAST_FETCH: dict[tuple[str, str], float] = {}
_LOCK = threading.Lock()


def is_fresh(platform_code: str, category_code: str, now: float | None = None) -> bool:
    stamp = _LAST_FETCH.get((platform_code, category_code))
    if stamp is None:
        return False
    return ((now or time.monotonic()) - stamp) < CACHE_SECONDS


def _mark_fetched(platform_code: str, category_code: str) -> None:
    with _LOCK:
        _LAST_FETCH[(platform_code, category_code)] = time.monotonic()


def reset_cache() -> None:
    """Forget freshness. For tests, and for a deliberate refresh."""
    with _LOCK:
        _LAST_FETCH.clear()


# ---------------------------------------------------------------------------
# Where to look, per platform, for a given category
# ---------------------------------------------------------------------------

def _category_paths(platform_code: str, category_code: str) -> list[str]:
    """Category listing URLs to try, most specific first.

    Read from config rather than hardcoded, so a wrong or missing path is
    fixed by editing YAML. A category with no configured path returns
    nothing, which is the honest outcome: we would rather collect nothing for
    it than search a term that returns unrelated products.
    """
    entry = platform_entry(platform_code)
    paths = (entry.get("category_paths") or {}).get(category_code)
    if not paths:
        return []
    if isinstance(paths, str):
        paths = [paths]

    base = entry["base_url"].rstrip("/")
    return [f"{base}{p}" for p in paths]


def _search_urls(platform_code: str, terms: Iterable[str]) -> list[str]:
    """Search URLs, for platforms whose robots permits a search query.

    Jumia is deliberately absent here: its robots disallows a very long list
    of query parameters, so we use clean category paths there and never a
    search string.
    """
    entry = platform_entry(platform_code)
    template = entry.get("search_url")
    if not template:
        return []
    return [template.format(q=quote(t)) for t in terms]


def _search_terms(category_code: str, limit: int = 2) -> list[str]:
    """The category's own keywords, as search terms.

    Taken from the taxonomy so the search term and the classifier agree by
    construction. Searching a word the classifier does not recognise would
    collect rows it then refuses to file.
    """
    from soko.normalize import taxonomy

    for entry in taxonomy()["categories"]:
        if entry["code"] == category_code:
            keywords = entry.get("keywords") or []
            # Longest first: "power bank" is a better query than "bank".
            ranked = sorted(keywords, key=len, reverse=True)
            return ranked[:limit]
    return []


# ---------------------------------------------------------------------------
# Parsing listing pages
# ---------------------------------------------------------------------------

_JUMIA_CARD = re.compile(r"<article[^>]*class=\"prd[^\"]*\".*?</article>", re.S)


def _parse_jumia_cards(html: str, observed_on: date) -> list[Listing]:
    """Products from a Jumia category page.

    Their cards carry the name, price and SKU as attributes, so one page
    yields around forty listings without opening any of them. That is the
    difference between a live fetch that fits in a page load and one that
    does not.
    """
    found: list[Listing] = []

    for card in _JUMIA_CARD.findall(html):
        href = re.search(r'href="(/[^"]+\.html)"', card)
        name = re.search(r'class="name"[^>]*>([^<]+)', card)
        price = re.search(r'class="prc"[^>]*>([^<]+)', card)
        if not (href and name and price):
            continue

        value = parse_price(price.group(1))
        if value is None:
            continue

        sku = re.search(r'data-ga4-item_id="([^"]+)"', card)
        url = f"https://www.jumia.co.ke{href.group(1)}"
        brand = re.search(r'data-ga4-item_brand="([^"]*)"', card)

        found.append(Listing(
            platform_code="jumia_ke",
            source_key=f"jumia_ke:{sku.group(1) if sku else href.group(1)}",
            source_url=url,
            title=_clean(name.group(1)),
            price_kes=value,
            observed_on=observed_on,
            seller_name=(brand.group(1).strip() or None) if brand else None,
            in_stock=True,
        ))
    return found


_KILI_ITEM = re.compile(r'<div class="product-item".*?(?=<div class="product-item"|\Z)', re.S)
_KILI_TITLE = re.compile(r'class="product-title"[^>]*>(.*?)</p>', re.S)
_KILI_PRICE = re.compile(r'class="[^"]*price[^"]*"[^>]*>\s*(KSh[^<]*)<', re.I)
_KILI_LINK = re.compile(r'href="(/listing/(\d+)[^"]*)"')
_TAGS = re.compile(r"<[^>]*>")


def _clean(text: str) -> str:
    """Readable title out of listing markup.

    Entities have to be decoded before the classifier sees the text.
    Listing pages carry titles like 'Amtec 43L12, 43&quot; FHD Smart',
    and an undecoded &quot; splits the words either side of it so the
    keyword match fails and a real product goes unclassified.
    """
    return re.sub(r"\s+", " ", html_lib.unescape(_TAGS.sub(" ", text))).strip()


def _parse_kilimall_cards(html: str, observed_on: date) -> list[Listing]:
    """Products from a Kilimall search or category page.

    Parsed per product block rather than by scanning for the nearest price
    to a link. The first version did the latter and pulled "Flash Sale" off
    a promotional banner as the product title for seven listings in a row,
    which would have fed the classifier garbage and put those prices in
    whatever category the banner text happened to match.

    The title sits in a product-title paragraph padded with the rendering
    framework's comment markers, so tags are stripped and whitespace
    collapsed rather than the text being taken raw.
    """
    found: list[Listing] = []
    seen: set[str] = set()

    for block in _KILI_ITEM.findall(html):
        link = _KILI_LINK.search(block)
        title_match = _KILI_TITLE.search(block)
        price_match = _KILI_PRICE.search(block)
        if not (link and title_match and price_match):
            continue

        path, sku = link.groups()
        if sku in seen:
            continue

        title = _clean(title_match.group(1))
        if len(title) < 6:
            continue

        value = parse_price(price_match.group(1))
        if value is None:
            continue

        seen.add(sku)
        found.append(Listing(
            platform_code="kilimall_ke",
            source_key=f"kilimall_ke:{sku}",
            source_url=f"https://www.kilimall.co.ke{path}",
            title=title,
            price_kes=value,
            observed_on=observed_on,
            in_stock=True,
        ))
    return found


_PARSERS = {
    "jumia_ke": _parse_jumia_cards,
    "kilimall_ke": _parse_kilimall_cards,
}


# ---------------------------------------------------------------------------
# The fetch itself
# ---------------------------------------------------------------------------

def fetch_category(
    platform_code: str,
    category_code: str,
    client: httpx.Client | None = None,
    force: bool = False,
) -> FetchReport:
    """Collect one category from one platform, right now.

    Returns a report rather than raising, because this runs inside a page
    load: a platform being slow should degrade the comparison to whatever is
    already stored, not break the page.
    """
    entry = platform_entry(platform_code)
    report = FetchReport(
        platform_code=platform_code,
        platform_name=entry.get("name", platform_code),
        requested=category_code,
    )

    if not force and is_fresh(platform_code, category_code):
        report.from_cache = True
        return report

    parser = _PARSERS.get(platform_code)
    if parser is None:
        report.error = f"No live collector for {report.platform_name}."
        return report

    urls = _category_paths(platform_code, category_code)
    if not urls:
        urls = _search_urls(platform_code, _search_terms(category_code))
    if not urls:
        report.error = (
            f"We do not have a {report.platform_name} listing page for that "
            f"category, so we cannot fetch it on demand."
        )
        return report

    settings = collection_settings()
    access = entry.get("access") or {}
    delay = float(access.get("delay_seconds", 2.0))

    owns_client = client is None
    client = client or httpx.Client(
        headers={
            "User-Agent": settings["user_agent"],
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-KE,en;q=0.9",
        },
        # Category pages are large, often over half a megabyte, so a tight
        # timeout fails on exactly the pages worth fetching.
        timeout=httpx.Timeout(connect=6.0, read=18.0, write=6.0, pool=6.0),
        follow_redirects=True,
    )

    started = time.monotonic()
    today = date.today()
    collected: dict[str, Listing] = {}

    try:
        for url in urls[:MAX_PAGES]:
            if time.monotonic() - started > DEADLINE_SECONDS:
                break
            if not path_allowed(platform_code, url):
                # Our own policy file refused this URL. That refusal is the
                # point of having the file, so it is not worked around here.
                continue

            if report.pages:
                time.sleep(delay)

            try:
                response = client.get(url)
            except httpx.HTTPError as exc:
                # One slow page must not abandon the rest. The first version
                # broke out of the loop here, so a single timeout on the
                # first of two category pages returned nothing at all even
                # though the second would have served fine.
                report.error = (
                    f"{report.platform_name} was slow to respond, so this may "
                    f"be less than everything they have."
                )
                continue

            report.pages += 1

            if response.status_code in (403, 429):
                # A block ends the fetch immediately. Continuing after a
                # platform says stop turns a throttle into a ban.
                report.blocked = True
                report.error = (
                    f"{report.platform_name} asked us to slow down, so we "
                    f"stopped. Showing what we already had."
                )
                break
            if response.status_code != 200:
                continue

            for listing in parser(response.text, today):
                collected.setdefault(listing.source_key, listing)

            # Stop as soon as there is enough to clear the evidence floor.
            #
            # A listing page carries about forty products and the floor is
            # thirty, so the first page is usually sufficient on its own.
            # Fetching the rest anyway doubled the wait for no change to the
            # answer, and asked twice as much of the platform for nothing.
            if len(collected) >= ENOUGH:
                break

    finally:
        if owns_client:
            client.close()

    report.listings = list(collected.values())
    report.found = len(report.listings)
    report.seconds = time.monotonic() - started

    if report.found:
        _mark_fetched(platform_code, category_code)

    return report


def refresh(
    category_code: str,
    platform_codes: list[str],
    store,
    force: bool = False,
) -> list[FetchReport]:
    """Top up the store for one category across platforms.

    Platforms are fetched concurrently. Sequentially this took about
    twenty-five seconds, which is long enough that nobody runs the search
    twice; in parallel it is bounded by the slowest single platform rather
    than their sum. They are different servers, so this adds no load to
    either one.

    Writes through the same idempotent store the crawler uses, so a live
    fetch and a scheduled crawl cannot double-count the same listing on the
    same day.
    """
    if not platform_codes:
        return []

    def one(code: str) -> FetchReport:
        try:
            return fetch_category(code, category_code, force=force)
        except BlockedError as exc:
            return FetchReport(
                platform_code=code,
                platform_name=code,
                requested=category_code,
                blocked=True,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            # This runs inside a page load. A platform behaving unexpectedly
            # should degrade the comparison to stored data, not 500 the page.
            return FetchReport(
                platform_code=code,
                platform_name=code,
                requested=category_code,
                error=f"{type(exc).__name__} while fetching",
            )

    with ThreadPoolExecutor(max_workers=len(platform_codes)) as pool:
        reports = list(pool.map(one, platform_codes))

    # Written after the fetches rather than inside them, because the store is
    # a single file and concurrent appends would interleave.
    for report in reports:
        if report.listings:
            store.write(report.listings)

    return reports


def summarise(reports: list[FetchReport]) -> str | None:
    """One line telling the vendor what just happened, or None if nothing did."""
    fetched = [r for r in reports if r.found]
    blocked = [r for r in reports if r.blocked]

    if not fetched and not blocked:
        return None

    parts = []
    if fetched:
        total = sum(r.found for r in fetched)
        names = ", ".join(r.platform_name for r in fetched)
        parts.append(f"Checked {names} just now and found {total} listings.")
    if blocked:
        names = ", ".join(r.platform_name for r in blocked)
        parts.append(f"{names} asked us to slow down, so those figures are from our last collection.")

    return " ".join(parts)
