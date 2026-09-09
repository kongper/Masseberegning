"""
Invitation and membership tests.

The one that matters most is test_concurrent_redeem_never_exceeds_max_uses.
Everything else here fails loudly and reproducibly; that one is the only part
of the design that would fail silently and intermittently in production, and
it is exactly the case a shared team link produces — several people clicking
within the same second.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import pytest

from conftest import needs_db

pytestmark = needs_db


def _admin(db, email="boss@prosit.no"):
    return db.ensure_bootstrap_superadmin(str(uuid.uuid4()), email)


def _invite(db, admin, **kw):
    opts = dict(label="T", email=None, role_granted="user",
                max_uses=1, expires_in_days=14)
    opts.update(kw)
    return db.create_invite(created_by=admin.id, **opts)


# ---------------------------------------------------------------- basics


def test_token_is_never_stored_in_the_clear(database):
    db = database
    admin = _admin(db)
    raw, row = _invite(db, admin)

    with db.pool().connection() as conn:
        stored = conn.execute(
            "select token_hash from invite where id = %s", (row["id"],)
        ).fetchone()["token_hash"]

    assert stored == db.hash_token(raw)
    assert raw.encode() not in bytes(stored)
    # 32 bytes of entropy, URL-safe base64.
    assert len(raw) >= 43


def test_redeem_creates_active_membership(database, new_id):
    db = database
    admin = _admin(db)
    raw, _ = _invite(db, admin)

    r = db.redeem_invite(raw_token=raw, user_id=new_id(), email="kari@firma.no")

    assert r.outcome == db.RedeemResult.OK
    assert r.user.is_active and r.user.role == "user"
    assert db.get_user(r.user.id).email == "kari@firma.no"


def test_invite_granting_superadmin_grants_superadmin(database, new_id):
    db = database
    admin = _admin(db)
    raw, _ = _invite(db, admin, role_granted="superadmin", email="two@prosit.no")

    r = db.redeem_invite(raw_token=raw, user_id=new_id(), email="two@prosit.no")

    assert r.user.is_superadmin


def test_redemption_records_who_invited_whom(database, new_id):
    db = database
    admin = _admin(db)
    raw, row = _invite(db, admin)
    uid = new_id()

    db.redeem_invite(raw_token=raw, user_id=uid, email="kari@firma.no")

    with db.pool().connection() as conn:
        u = conn.execute(
            "select invited_by, invite_id from app_user where id = %s", (uid,)
        ).fetchone()
        red = conn.execute(
            "select count(*) as n from invite_redemption where invite_id = %s", (row["id"],)
        ).fetchone()

    assert str(u["invited_by"]) == admin.id
    assert str(u["invite_id"]) == str(row["id"])
    assert red["n"] == 1


# ------------------------------------------------------------- rejections


def test_unknown_token_is_rejected(database, new_id):
    db = database
    r = db.redeem_invite(raw_token="not-a-real-token-at-all", user_id=new_id(),
                         email="x@y.no")
    assert r.outcome == db.RedeemResult.INVALID


def test_single_use_invite_cannot_be_used_twice(database, new_id):
    db = database
    admin = _admin(db)
    raw, _ = _invite(db, admin, max_uses=1)

    first = db.redeem_invite(raw_token=raw, user_id=new_id(), email="a@firma.no")
    second = db.redeem_invite(raw_token=raw, user_id=new_id(), email="b@firma.no")

    assert first.outcome == db.RedeemResult.OK
    assert second.outcome == db.RedeemResult.INVALID


def test_revoked_invite_is_rejected(database, new_id):
    db = database
    admin = _admin(db)
    raw, row = _invite(db, admin)

    db.revoke_invite(str(row["id"]))
    r = db.redeem_invite(raw_token=raw, user_id=new_id(), email="a@firma.no")

    assert r.outcome == db.RedeemResult.INVALID


def test_revoking_twice_reports_nothing_to_revoke(database):
    db = database
    admin = _admin(db)
    _, row = _invite(db, admin)

    assert db.revoke_invite(str(row["id"])) is not None
    assert db.revoke_invite(str(row["id"])) is None


def test_expired_invite_is_rejected(database, new_id):
    db = database
    admin = _admin(db)
    raw, row = _invite(db, admin)

    with db.pool().connection() as conn:
        conn.execute("update invite set expires_at = %s where id = %s",
                     (datetime.now(timezone.utc) - timedelta(minutes=1), row["id"]))
        conn.commit()

    r = db.redeem_invite(raw_token=raw, user_id=new_id(), email="a@firma.no")
    assert r.outcome == db.RedeemResult.INVALID


def test_suspended_user_redeeming_again_is_reinstated_not_blocked(database, new_id):
    """A suspended user is not 'already a member', so a fresh invite reinstates.

    This is the intended behaviour: suspension removes access, and a
    superadmin issuing a new invite is a deliberate act of letting them back
    in. It must not silently short-circuit as already_member.
    """
    db = database
    admin = _admin(db)
    raw1, _ = _invite(db, admin)
    uid = new_id()
    db.redeem_invite(raw_token=raw1, user_id=uid, email="a@firma.no")
    db.update_user(uid, status="suspended")

    raw2, _ = _invite(db, admin)
    r = db.redeem_invite(raw_token=raw2, user_id=uid, email="a@firma.no")

    assert r.outcome == db.RedeemResult.OK
    assert db.get_user(uid).is_active


# ---------------------------------------------------------- email binding


def test_email_bound_invite_rejects_a_different_address(database, new_id):
    db = database
    admin = _admin(db)
    raw, _ = _invite(db, admin, email="kari@firma.no")

    r = db.redeem_invite(raw_token=raw, user_id=new_id(), email="someone@else.no")

    assert r.outcome == db.RedeemResult.WRONG_EMAIL
    assert r.bound_email == "kari@firma.no"


def test_email_binding_is_case_insensitive(database, new_id):
    db = database
    admin = _admin(db)
    raw, _ = _invite(db, admin, email="kari@firma.no")

    r = db.redeem_invite(raw_token=raw, user_id=new_id(), email="Kari@FIRMA.no")

    assert r.outcome == db.RedeemResult.OK


def test_a_failed_binding_attempt_does_not_consume_a_use(database, new_id):
    db = database
    admin = _admin(db)
    raw, row = _invite(db, admin, email="kari@firma.no")

    db.redeem_invite(raw_token=raw, user_id=new_id(), email="wrong@else.no")
    ok = db.redeem_invite(raw_token=raw, user_id=new_id(), email="kari@firma.no")

    assert ok.outcome == db.RedeemResult.OK
    with db.pool().connection() as conn:
        uses = conn.execute("select uses from invite where id = %s",
                            (row["id"],)).fetchone()["uses"]
    assert uses == 1


def test_expired_bound_invite_does_not_leak_the_bound_address(database, new_id):
    """An expired invite must fall through to the generic error.

    Otherwise the wrong-address message becomes an oracle for which addresses
    have been invited, long after the link stopped working.
    """
    db = database
    admin = _admin(db)
    raw, row = _invite(db, admin, email="kari@firma.no")
    with db.pool().connection() as conn:
        conn.execute("update invite set expires_at = now() - interval '1 hour' where id = %s",
                     (row["id"],))
        conn.commit()

    r = db.redeem_invite(raw_token=raw, user_id=new_id(), email="wrong@else.no")

    assert r.outcome == db.RedeemResult.INVALID
    assert r.bound_email is None


def test_bound_invite_is_forced_single_use_by_the_database(database):
    """The constraint, not just the API check, refuses a multi-use bound invite."""
    import psycopg

    db = database
    admin = _admin(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        _invite(db, admin, email="kari@firma.no", max_uses=5)


# ------------------------------------------------------------ idempotence


def test_active_member_redeeming_again_does_not_burn_a_use(database, new_id):
    db = database
    admin = _admin(db)
    raw, row = _invite(db, admin, max_uses=3, email=None)
    uid = new_id()

    db.redeem_invite(raw_token=raw, user_id=uid, email="a@firma.no")
    again = db.redeem_invite(raw_token=raw, user_id=uid, email="a@firma.no")

    assert again.outcome == db.RedeemResult.ALREADY_MEMBER
    with db.pool().connection() as conn:
        uses = conn.execute("select uses from invite where id = %s",
                            (row["id"],)).fetchone()["uses"]
    assert uses == 1, "clicking your own link twice must not cost a seat"


# ------------------------------------------------------------ concurrency


def test_concurrent_redeem_never_exceeds_max_uses(database):
    """The reason redemption is a single UPDATE rather than check-then-increment.

    A multi-use link shared with a team produces exactly this: many people
    clicking at once. A read-then-write would let more people in than the
    invite allows, and would do it intermittently.
    """
    db = database
    admin = _admin(db)
    max_uses = 3
    attempts = 12
    raw, row = _invite(db, admin, max_uses=max_uses, email=None)

    users = [(str(uuid.uuid4()), f"user{i}@firma.no") for i in range(attempts)]

    def attempt(pair):
        uid, email = pair
        return db.redeem_invite(raw_token=raw, user_id=uid, email=email).outcome

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        outcomes = [f.result() for f in as_completed([pool.submit(attempt, u) for u in users])]

    granted = outcomes.count(db.RedeemResult.OK)
    refused = outcomes.count(db.RedeemResult.INVALID)

    assert granted == max_uses, f"expected exactly {max_uses} grants, got {granted}"
    assert refused == attempts - max_uses

    with db.pool().connection() as conn:
        state = conn.execute(
            "select uses, max_uses from invite where id = %s", (row["id"],)
        ).fetchone()
        members = conn.execute(
            "select count(*) as n from app_user where role = 'user'"
        ).fetchone()["n"]
        redemptions = conn.execute(
            "select count(*) as n from invite_redemption where invite_id = %s",
            (row["id"],),
        ).fetchone()["n"]

    assert state["uses"] == max_uses
    assert state["uses"] <= state["max_uses"]
    assert members == max_uses, "a refused attempt must not leave a membership behind"
    assert redemptions == max_uses


def test_concurrent_redeem_of_a_single_use_invite_admits_exactly_one(database):
    db = database
    admin = _admin(db)
    raw, _ = _invite(db, admin, max_uses=1, email=None)
    users = [(str(uuid.uuid4()), f"u{i}@firma.no") for i in range(8)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = [
            f.result() for f in as_completed([
                pool.submit(lambda p: db.redeem_invite(
                    raw_token=raw, user_id=p[0], email=p[1]).outcome, u)
                for u in users
            ])
        ]

    assert outcomes.count(db.RedeemResult.OK) == 1


# --------------------------------------------------------- superadmin math


def test_last_active_superadmin_is_countable(database, new_id):
    db = database
    admin = _admin(db)
    assert db.count_active_superadmins() == 1
    assert db.count_active_superadmins(exclude_id=admin.id) == 0

    raw, _ = _invite(db, admin, role_granted="superadmin", email="two@prosit.no")
    second = db.redeem_invite(raw_token=raw, user_id=new_id(), email="two@prosit.no").user

    assert db.count_active_superadmins() == 2
    assert db.count_active_superadmins(exclude_id=second.id) == 1


def test_suspended_superadmin_does_not_count_as_a_way_in(database, new_id):
    db = database
    admin = _admin(db)
    raw, _ = _invite(db, admin, role_granted="superadmin", email="two@prosit.no")
    second = db.redeem_invite(raw_token=raw, user_id=new_id(), email="two@prosit.no").user

    db.update_user(second.id, status="suspended")

    assert db.count_active_superadmins() == 1


# ------------------------------------------------------------- bootstrap


def test_bootstrap_is_idempotent_and_promotes(database, new_id):
    db = database
    uid = new_id()

    first = db.ensure_bootstrap_superadmin(uid, "boss@prosit.no")
    db.update_user(uid, role="user", status="suspended")
    again = db.ensure_bootstrap_superadmin(uid, "boss@prosit.no")

    assert first.is_superadmin
    assert again.is_superadmin and again.is_active
    with db.pool().connection() as conn:
        n = conn.execute("select count(*) as n from app_user").fetchone()["n"]
    assert n == 1


def test_listing_shows_computed_invite_status(database, new_id):
    db = database
    admin = _admin(db)
    _invite(db, admin, label="open")
    raw_used, _ = _invite(db, admin, label="used")
    db.redeem_invite(raw_token=raw_used, user_id=new_id(), email="a@firma.no")
    _, revoked = _invite(db, admin, label="gone")
    db.revoke_invite(str(revoked["id"]))

    by_label = {i["label"]: i for i in db.list_invites()}

    assert by_label["open"]["status"] == "active"
    assert by_label["used"]["status"] == "used_up"
    assert by_label["used"]["redeemed_by"] == ["a@firma.no"]
    assert by_label["gone"]["status"] == "revoked"
