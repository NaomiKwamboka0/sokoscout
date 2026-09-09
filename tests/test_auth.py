"""Authentication and tier gating tests.

The predecessor shipped with every route open. These tests exist so that the
replacement cannot regress to it quietly.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from soko.auth import (
    Account,
    AuthError,
    TIERS,
    account_ref,
    constant_time_equal,
    gate_answer,
    hash_password,
    hash_token,
    new_session_token,
    session_expiry,
    start_trial,
    verify_password,
)

TODAY = date(2026, 9, 9)


def account(**overrides) -> Account:
    base = dict(
        id=1,
        email="vendor@example.com",
        password_hash="x",
        email_verified=True,
        tier="starter",
        trial_ends_on=None,
        disabled_at=None,
    )
    base.update(overrides)
    return Account(**base)


class TestPasswordHashing:
    def test_hash_and_verify_round_trip(self):
        stored = hash_password("a long enough passphrase")
        assert verify_password(stored, "a long enough passphrase")

    def test_wrong_password_fails(self):
        stored = hash_password("a long enough passphrase")
        assert not verify_password(stored, "a different passphrase")

    def test_hash_is_not_the_password(self):
        stored = hash_password("a long enough passphrase")
        assert "a long enough passphrase" not in stored

    def test_uses_argon2id(self):
        # Not a general purpose hash. Hashing a password with SHA-256 is not a
        # slower version of the right thing, it is the wrong thing.
        assert hash_password("a long enough passphrase").startswith("$argon2id$")

    def test_same_password_hashes_differently(self):
        # Per-hash salt. Two users with the same password must not be
        # detectable as such from the stored hashes.
        assert hash_password("a long enough passphrase") != hash_password(
            "a long enough passphrase"
        )

    def test_short_password_is_rejected(self):
        with pytest.raises(AuthError):
            hash_password("short")

    def test_verify_never_raises_on_bad_input(self):
        # A mismatch is a return value, not an exception, or every call site
        # needs a try block and one of them will eventually forget.
        assert not verify_password("", "anything")
        assert not verify_password("not-a-hash", "anything")
        assert not verify_password("$argon2id$garbage", "anything")


class TestSessionTokens:
    def test_token_and_hash_differ(self):
        # Only the hash is stored, so a leaked database does not hand over
        # live sessions.
        token, stored = new_session_token()
        assert token != stored
        assert hash_token(token) == stored

    def test_tokens_are_unique(self):
        assert len({new_session_token()[0] for _ in range(50)}) == 50

    def test_token_has_enough_entropy(self):
        token, _ = new_session_token()
        assert len(token) >= 40

    def test_expiry_is_in_the_future(self):
        now = datetime(2026, 9, 9, tzinfo=timezone.utc)
        assert session_expiry(now) > now

    def test_constant_time_comparison(self):
        assert constant_time_equal("abc", "abc")
        assert not constant_time_equal("abc", "abd")
        assert not constant_time_equal("", "abc")


class TestQuestionLogPrivacy:
    """What a vendor asks reveals what they are about to invest in."""

    def test_ref_is_not_the_email(self):
        ref = account_ref("vendor@example.com")
        assert "vendor" not in ref
        assert "@" not in ref

    def test_ref_is_stable(self):
        assert account_ref("vendor@example.com") == account_ref("vendor@example.com")

    def test_ref_ignores_case_and_whitespace(self):
        assert account_ref("Vendor@Example.com ") == account_ref("vendor@example.com")

    def test_different_accounts_get_different_refs(self):
        assert account_ref("a@example.com") != account_ref("b@example.com")


class TestAccountState:
    def test_verified_active_account_is_active(self):
        assert account().active(TODAY)

    def test_unverified_account_is_not_active(self):
        assert not account(email_verified=False).active(TODAY)

    def test_disabled_account_is_not_active(self):
        assert not account(disabled_at=datetime.now(timezone.utc)).active(TODAY)

    def test_expired_trial_is_not_active(self):
        expired = account(tier="trial", trial_ends_on=TODAY - timedelta(days=1))
        assert expired.trial_expired(TODAY)
        assert not expired.active(TODAY)

    def test_trial_on_its_last_day_is_still_active(self):
        # An off by one here bills somebody a day early.
        current = account(tier="trial", trial_ends_on=TODAY)
        assert not current.trial_expired(TODAY)
        assert current.active(TODAY)

    def test_a_paid_tier_does_not_expire_as_a_trial(self):
        paid = account(tier="starter", trial_ends_on=TODAY - timedelta(days=100))
        assert not paid.trial_expired(TODAY)

    def test_trial_length(self):
        assert start_trial(TODAY) == TODAY + timedelta(days=14)


class TestTierEntitlements:
    def test_starter_sees_two_platforms(self):
        assert account(tier="starter").may_see_platform("jumia_ke")
        assert not account(tier="starter").may_see_platform("glovo_ke")

    def test_growth_sees_all_four(self):
        acct = account(tier="growth")
        for platform in ("jumia_ke", "kilimall_ke", "glovo_ke", "ubereats_ke"):
            assert acct.may_see_platform(platform)

    def test_history_depth_climbs_with_tier(self):
        assert account(tier="starter").history_cutoff(TODAY) == TODAY - timedelta(days=30)
        assert account(tier="growth").history_cutoff(TODAY) == TODAY - timedelta(days=365)
        # Pro and above get everything.
        assert account(tier="pro").history_cutoff(TODAY) is None

    def test_the_ladder_gates_breadth_not_volume(self):
        # Answering costs nothing at the margin, so quotas exist for capacity
        # and fairness rather than to control a bill.
        assert TIERS["pro"]["question_quota"] is None
        assert TIERS["premium"]["question_quota"] is None

    def test_prices_match_the_proposal(self):
        assert TIERS["starter"]["price_kes"] == 200
        assert TIERS["growth"]["price_kes"] == 1200
        assert TIERS["pro"]["price_kes"] == 4500
        assert TIERS["premium"]["price_kes"] == 15000


class TestGating:
    def test_active_account_is_not_gated(self):
        assert gate_answer(account(), "jumia_ke") is None

    def test_expired_trial_is_told_what_to_do(self):
        # A locked door with no sign on it loses a customer who was ready to
        # pay. The gate returns the reason, not a boolean.
        expired = account(tier="trial", trial_ends_on=TODAY - timedelta(days=1))
        reason = gate_answer(expired, "jumia_ke")
        assert reason is not None
        assert "200" in reason

    def test_platform_outside_the_plan_names_the_upgrade(self):
        reason = gate_answer(account(tier="starter"), "glovo_ke")
        assert reason is not None
        assert "1,200" in reason

    def test_unverified_email_is_told_to_verify(self):
        reason = gate_answer(account(email_verified=False), "jumia_ke")
        assert "verify" in reason.lower()
