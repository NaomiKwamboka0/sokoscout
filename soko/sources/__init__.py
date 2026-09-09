"""Platform drivers.

Every source implements the same contract in base.py and returns the same
Listing. Adding or replacing a source is a new file here, never a change to
the pipeline. That is the mitigation for platform access changing under us:
a blocked source is a replaced file rather than a rebuild.
"""

from soko.sources.base import (
    BlockedError,
    EmptyResultError,
    Listing,
    RunStats,
    SourceDriver,
    guard_not_empty,
)

__all__ = [
    "BlockedError",
    "EmptyResultError",
    "Listing",
    "RunStats",
    "SourceDriver",
    "guard_not_empty",
]
