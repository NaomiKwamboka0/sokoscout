"""The source driver contract, and the collection discipline every driver
inherits.

Every platform sits behind this one interface. That is the mitigation for the
risk the proposal names first: platform access changing under us. A blocked
source becomes a replaced file, not a rebuild.

Three rules are enforced here rather than left to each driver, because each
one cost the predecessor project real time when it was left optional:

  1. An empty result set raises. Anti bot systems signal throttling by
     returning nothing successfully rather than by returning an error. A
     collector without this check looks perfectly healthy while collecting
     zero rows, and the failure is invisible until somebody asks why the
     median stopped moving.

  2. Reruns resume. Raw records are unique on (platform, key, date) and the
     crawler checkpoints after each unit, so an interrupted run continues
     rather than starting over.

  3. Rate limits are honoured whether or not the platform declares one. Being
     permitted to go faster is not a reason to.
"""

from __future__ import annotations

import functools
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Protocol
from urllib.parse import urlparse

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class EmptyResultError(RuntimeError):
    """A driver returned nothing where rows were expected.

    This is deliberately an exception and not a warning. The whole point is
    that a silent zero looks identical to a healthy run, so it must be loud
    enough to stop a scheduled job and show up in the run record.
    """


class BlockedError(RuntimeError):
    """The platform refused us: 403, a challenge page, or a robots exclusion."""


@dataclass(frozen=True)
class Listing:
    """One product observation, as every driver returns it.

    Frozen because a driver must not be able to mutate what it already
    emitted, and because these get put in sets during deduplication.

    `raw` carries the untouched payload. Storing it means extraction can be
    improved and re-run over data already collected, without re-crawling
    anybody. That has already paid for itself once in the predecessor.
    """

    platform_code: str
    source_key: str
    source_url: str
    title: str
    price_kes: Any                      # Decimal, after normalisation
    observed_on: date
    was_price_kes: Any = None
    seller_name: str | None = None
    brand: str | None = None
    breadcrumb: str | None = None
    in_stock: bool | None = None
    rating: float | None = None
    rating_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunStats:
    """What happened during one collection run.

    Written to crawl_run so that a run producing zero rows is a visible,
    queryable event rather than a gap somebody notices weeks later.
    """

    platform_code: str
    urls_seen: int = 0
    urls_ok: int = 0
    urls_failed: int = 0
    rows_written: int = 0
    blocked: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform_code,
            "urls_seen": self.urls_seen,
            "urls_ok": self.urls_ok,
            "urls_failed": self.urls_failed,
            "rows_written": self.rows_written,
            "blocked": self.blocked,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
        }


class SourceDriver(Protocol):
    """What every platform driver implements.

    Adding or replacing a source is a new file in this package, never a change
    to the pipeline. That is what makes a blocked platform survivable.
    """

    platform_code: str

    def collect(self, limit: int) -> Iterator[Listing]:
        """Yield listings, newest first where the platform allows it."""
        ...


@functools.lru_cache(maxsize=1)
def platform_config() -> dict[str, Any]:
    with open(CONFIG_DIR / "platforms.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def platform_entry(code: str) -> dict[str, Any]:
    for entry in platform_config()["platforms"]:
        if entry["code"] == code:
            return entry
    raise KeyError(f"No platform configured with code {code!r}")


def collection_settings() -> dict[str, Any]:
    return platform_config()["collection"]


def path_allowed(platform_code: str, url: str) -> bool:
    """Whether our own policy file permits fetching this URL.

    This is our read of the platform's published robots policy, kept in
    config/platforms.yaml so it is reviewable as a document rather than
    buried in request code. A URL that fails this check is not fetched, and
    that is a refusal we want to be able to point at.

    Three kinds of rule, because that is what the real policies use. Reading
    Jumia's live file showed that a prefix-only check, which is the obvious
    implementation, would have missed almost every rule they actually wrote
    while looking perfectly thorough:

      prefixes   /en/, /mobapi/ - the path starts with this
      fragments  */seller/, */brands/ - appears anywhere in the path
      query      any facet or tracking parameter

    Explicit allows win over everything, because that is how a robots group
    resolves and because their allowed endpoints sit underneath a path that
    is otherwise uninteresting to us.
    """
    entry = platform_entry(platform_code)
    access = entry.get("access") or {}
    parsed = urlparse(url)
    path = parsed.path or "/"

    # Facets are checked before the allow list, not after.
    #
    # This ordering is the whole rule, and getting it backwards is a silent
    # violation rather than a visible bug: /phones.html?color=black matches
    # the "/*.html" allow, so an allow-first check returns True and the
    # crawler walks straight into the facet URLs their robots file spends two
    # hundred lines asking us not to touch. The allow covers the product page;
    # it does not cover every parameterised view of it.
    if access.get("disallow_query_strings") and parsed.query:
        return False

    # An explicit allow is the most specific remaining rule and wins outright.
    for allowed in access.get("allowed_paths") or []:
        if allowed.startswith("/*."):
            if path.endswith(allowed[2:]):
                return True
        elif not allowed.startswith("/*") and path.startswith(allowed):
            return True

    for prefix in access.get("disallowed_prefixes") or []:
        if path.startswith(prefix):
            return False

    for fragment in access.get("disallowed_fragments") or []:
        if fragment in path:
            return False

    return True


class RateLimiter:
    """A minimum gap between requests to one host.

    Deliberately a sleep and not a token bucket. Concurrency against these
    platforms buys us very little and is precisely the behaviour that gets a
    crawler blocked, so the simplest correct thing is the right one.
    """

    def __init__(self, delay_seconds: float) -> None:
        self.delay = max(0.0, float(delay_seconds or 0))
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            elapsed = time.monotonic() - self._last
            remaining = self.delay - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last = time.monotonic()


def guard_not_empty(rows: list[Listing], platform_code: str, context: str) -> list[Listing]:
    """Rule 1, applied. Raise on an empty harvest rather than returning it.

    Call this at the end of any unit of work that should have produced rows.
    The context string goes into the message because "jumia returned nothing"
    is much less useful at three in the morning than knowing which sitemap
    section it was working through.
    """
    minimum = collection_settings().get("min_rows_per_run", 1)
    if len(rows) < minimum:
        raise EmptyResultError(
            f"{platform_code}: {context} produced {len(rows)} rows, expected at "
            f"least {minimum}. This is usually throttling, which these "
            f"platforms signal by returning nothing successfully rather than "
            f"by returning an error. Do not treat it as an empty catalogue."
        )
    return rows
