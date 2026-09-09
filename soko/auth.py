"""Accounts, sessions and tier gating.

The predecessor to this product shipped with no authentication at all. Every
route on its public URL was open: any visitor could trigger a collection run,
read the whole database, and export it. It was written to run on a laptop and
then deployed to the internet, and the assumption did not survive the move.

That is why this module exists before the web application does, rather than
after it.

Three decisions worth stating:

  argon2id for passwords, not a general purpose hash. Hashing a password with
  SHA-256 is not a slower version of the right thing, it is the wrong thing.

  Server held sessions, not a self contained token. A subscription that lapses
  or a payment that fails must revoke access immediately, and a self contained
  token cannot be revoked before it expires without keeping a server side list
  anyway. So we keep the list and skip the indirection.

  Identical responses whether or not an account exists. A login form that
  answers differently for a real address is a customer enumeration endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
    _HASHER: Any = PasswordHasher()
    _ARGON2_AVAILABLE = True
except ImportError:  # pragma: no cover - argon2 is a hard requirement in prod
    _HASHER = None
    _ARGON2_AVAILABLE = False


SESSION_LIFETIME = timedelta(days=30)
TRIAL_DAYS = 14

# What each tier may see. The ladder gates breadth and depth rather than
# question volume, because answering a question costs nothing at the margin
# and rationing questions would punish exactly the engaged users we want.
TIERS: dict[str, dict[str, Any]] = {
    "trial": {
        "price_kes": 0,
        "platforms": ["jumia_ke", "kilimall_ke"],
        "history_days": 30,
        "watch_categories": 1,
        "question_quota": 40,
        "export": False,
    },
    "starter": {
        "price_kes": 200,
        "platforms": ["jumia_ke", "kilimall_ke"],
        "history_days": 30,
        "watch_categories": 1,
        "question_quota": 300,
        "export": False,
    },
    "growth": {
        "price_kes": 1200,
        "platforms": ["jumia_ke", "kilimall_ke", "glovo_ke", "ubereats_ke"],
        "history_days": 365,
        "watch_categories": 5,
        "question_quota": 2000,
        "export": True,
    },
    "pro": {
        "price_kes": 4500,
        "platforms": ["jumia_ke", "kilimall_ke", "glovo_ke", "ubereats_ke"],
        "history_days": None,          # full history
        "watch_categories": 25,
        "question_quota": None,        # fair use
        "export": True,
    },
    "premium": {
        "price_kes": 15000,
        "platforms": ["jumia_ke", "kilimall_ke", "glovo_ke", "ubereats_ke"],
        "history_days": None,
        "watch_categories": None,
        "question_quota": None,
        "export": True,
    },
}


class AuthError(Exception):
    """Authentication or authorisation failure.

    Deliberately one exception for every failure mode. Distinguishing "no such
    account" from "wrong password" in a type the caller can branch on is how
    an enumeration bug gets written by accident later.
    """


def hash_password(password: str) -> str:
    """argon2id hash of a password.

    Raises rather than falling back to a weaker hash when argon2 is missing.
    A silent downgrade to SHA-256 would produce a system that appears to work
    and stores passwords badly, which is the worst of both.
    """
    if not _ARGON2_AVAILABLE:
        raise RuntimeError(
            "argon2-cffi is not installed. Refusing to hash passwords with a "
            "general purpose hash: that is not a slower version of the right "
            "thing, it is the wrong thing. pip install argon2-cffi"
        )
    if not password or len(password) < 10:
        # Length is the only requirement worth enforcing. Composition rules
        # push people toward Password1! and away from a long passphrase.
        raise AuthError("Password must be at least 10 characters.")
    return _HASHER.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Check a password against its hash. Never raises on a mismatch."""
    if not _ARGON2_AVAILABLE or not stored_hash or not password:
        return False
    try:
        return bool(_HASHER.verify(stored_hash, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Whether a stored hash used weaker parameters than we now use."""
    if not _ARGON2_AVAILABLE or not stored_hash:
        return False
    try:
        return bool(_HASHER.check_needs_rehash(stored_hash))
    except InvalidHashError:
        return True


def new_session_token() -> tuple[str, str]:
    """A session token and the hash to store against it.

    The raw token goes to the browser; only its hash is stored. A leaked
    database therefore does not hand over live sessions.
    """
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    """SHA-256 of a session token.

    A fast hash is correct here and wrong for passwords. A session token is
    256 bits of entropy from a CSPRNG, so there is nothing to brute force;
    a password is not.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a or "", b or "")


def account_ref(email: str, salt: str = "sokoscout") -> str:
    """A pseudonymous, stable reference for the question log.

    What a vendor asks reveals exactly what they are about to invest in, which
    is among the most commercially sensitive things a business has. The
    question log therefore carries this rather than an email address, so the
    log is useful for finding gaps without being a record of who asked what.
    """
    digest = hashlib.sha256(f"{salt}:{email.strip().lower()}".encode("utf-8"))
    return digest.hexdigest()[:16]


@dataclass
class Account:
    """A subscriber. Mirrors the `account` table in schema.sql."""

    id: int
    email: str
    password_hash: str
    email_verified: bool = False
    tier: str = "trial"
    trial_ends_on: date | None = None
    disabled_at: datetime | None = None

    @property
    def ref(self) -> str:
        return account_ref(self.email)

    def entitlements(self) -> dict[str, Any]:
        return dict(TIERS.get(self.tier, TIERS["trial"]))

    def trial_expired(self, today: date | None = None) -> bool:
        if self.tier != "trial" or self.trial_ends_on is None:
            return False
        return (today or date.today()) > self.trial_ends_on

    def active(self, today: date | None = None) -> bool:
        """Whether this account may currently be served.

        A disabled account and an expired trial both fail here, and the caller
        cannot forget to check one of them separately.
        """
        if self.disabled_at is not None:
            return False
        if not self.email_verified:
            return False
        return not self.trial_expired(today)

    def may_see_platform(self, platform_code: str) -> bool:
        return platform_code in self.entitlements()["platforms"]

    def history_cutoff(self, today: date | None = None) -> date | None:
        """The earliest date this account may query, or None for full history."""
        days = self.entitlements()["history_days"]
        if days is None:
            return None
        return (today or date.today()) - timedelta(days=days)


def start_trial(today: date | None = None) -> date:
    return (today or date.today()) + timedelta(days=TRIAL_DAYS)


def session_expiry(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + SESSION_LIFETIME


def gate_answer(account: Account, platform_code: str) -> str | None:
    """Why this account may not have this answer, or None if it may.

    Returns the reason as text rather than a boolean, so the product can tell
    a vendor what to upgrade to instead of showing them a locked door.
    """
    if not account.active():
        if account.trial_expired():
            return (
                "Your free trial has ended. Starter is KSh 200 a month and "
                "covers Jumia and Kilimall with thirty days of price history."
            )
        if not account.email_verified:
            return "Please verify your email address before asking questions."
        return "This account is not active."

    if not account.may_see_platform(platform_code):
        return (
            f"Your plan does not include that platform. Growth is KSh 1,200 a "
            f"month and covers all four."
        )
    return None
