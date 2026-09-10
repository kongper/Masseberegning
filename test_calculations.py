"""
Saved calculations.

The tests that matter are the ownership ones. Saved work is private per user,
and the only thing enforcing that is `user_id` being in the WHERE clause of
every query — no framework, no policy, one column. So each operation is tested
against another user's row, not just against a missing one.
"""

from __future__ import annotations

import uuid

import pytest

from conftest import needs_db

pytestmark = needs_db

POLY = [[10.46, 61.11], [10.47, 61.11], [10.47, 61.12], [10.46, 61.12]]
PARAMS = {"mode": "balansert", "soil_depth": 2.0}
SUMMARY = {"level": 201.15, "area_m2": 144270.0, "cut_bank_m3": 534278.0,
           "fill_void_m3": 485707.0, "net_bank_m3": 0.0,
           "resolution_m": 1.0, "coverage": 1.0, "mode": "balansert"}


def _admin(db):
    """The bootstrap superadmin, created once per test.

    Reused rather than re-created, because `app_user.email` is unique and
    `ensure_bootstrap_superadmin` upserts on `id` — calling it twice with the
    same address and different ids is a unique violation. Which is a real (if
    unlikely) production edge: a Supabase user deleted and signed in again gets
    a new `sub` with the same address.
    """
    with db.pool().connection() as conn:
        row = conn.execute(
            "select id from app_user where email = %s", ("boss@prosit.no",)
        ).fetchone()
    if row:
        return db.get_user(str(row["id"]))
    return db.ensure_bootstrap_superadmin(str(uuid.uuid4()), "boss@prosit.no")


def _member(db, email):
    """A member created the way a real one is: by redeeming an invitation."""
    admin = _admin(db)
    raw, _ = db.create_invite(created_by=admin.id, label="t", email=None,
                              role_granted="user", max_uses=1, expires_in_days=7)
    return db.redeem_invite(raw_token=raw, user_id=str(uuid.uuid4()), email=email).user


def _save(db, user, name="Tomt på Storhove"):
    return db.save_calculation(user_id=user.id, name=name, polygon=POLY,
                               params=PARAMS, summary=SUMMARY)


# -------------------------------------------------------------- round trip


def test_save_then_open_returns_the_inputs(database):
    db = database
    user = _member(db, "kari@firma.no")

    saved = _save(db, user)
    got = db.get_calculation(user.id, str(saved["id"]))

    assert got["name"] == "Tomt på Storhove"
    assert got["polygon"] == POLY
    assert got["params"] == PARAMS
    assert got["summary"]["net_bank_m3"] == 0.0


def test_the_list_is_newest_first_and_omits_the_heavy_fields(database):
    db = database
    user = _member(db, "kari@firma.no")
    _save(db, user, "first")
    _save(db, user, "second")

    rows = db.list_calculations(user.id)

    assert [r["name"] for r in rows] == ["second", "first"]
    # The list is for choosing, not for restoring; polygon and params are
    # fetched only when one is opened.
    assert "polygon" not in rows[0] and "params" not in rows[0]
    assert rows[0]["summary"]["area_m2"] == 144270.0


def test_rename_and_delete(database):
    db = database
    user = _member(db, "kari@firma.no")
    saved = _save(db, user)

    renamed = db.rename_calculation(user.id, str(saved["id"]), "Nytt navn")
    assert renamed["name"] == "Nytt navn"

    assert db.delete_calculation(user.id, str(saved["id"])) is True
    assert db.get_calculation(user.id, str(saved["id"])) is None
    assert db.delete_calculation(user.id, str(saved["id"])) is False


def test_a_blank_name_is_refused_by_the_database(database):
    import psycopg

    db = database
    user = _member(db, "kari@firma.no")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.save_calculation(user_id=user.id, name="   ", polygon=POLY,
                            params=PARAMS, summary=SUMMARY)


# ---------------------------------------------------------------- ownership


def test_another_user_cannot_open_it(database):
    db = database
    kari = _member(db, "kari@firma.no")
    ola = _member(db, "ola@firma.no")
    saved = _save(db, kari)

    assert db.get_calculation(ola.id, str(saved["id"])) is None


def test_another_user_cannot_rename_it(database):
    db = database
    kari = _member(db, "kari@firma.no")
    ola = _member(db, "ola@firma.no")
    saved = _save(db, kari)

    assert db.rename_calculation(ola.id, str(saved["id"]), "hijacked") is None
    assert db.get_calculation(kari.id, str(saved["id"]))["name"] == "Tomt på Storhove"


def test_another_user_cannot_delete_it(database):
    db = database
    kari = _member(db, "kari@firma.no")
    ola = _member(db, "ola@firma.no")
    saved = _save(db, kari)

    assert db.delete_calculation(ola.id, str(saved["id"])) is False
    assert db.get_calculation(kari.id, str(saved["id"])) is not None


def test_one_users_list_never_shows_anothers(database):
    db = database
    kari = _member(db, "kari@firma.no")
    ola = _member(db, "ola@firma.no")
    _save(db, kari, "kari sin")
    _save(db, ola, "ola sin")

    assert [r["name"] for r in db.list_calculations(kari.id)] == ["kari sin"]
    assert [r["name"] for r in db.list_calculations(ola.id)] == ["ola sin"]
    assert db.count_calculations(kari.id) == 1


def test_deleting_the_member_removes_their_saved_work(database):
    """The GDPR path: DELETE /api/brukere/{id} must not leave orphans."""
    db = database
    kari = _member(db, "kari@firma.no")
    _save(db, kari)

    db.delete_user(kari.id)

    with db.pool().connection() as conn:
        n = conn.execute("select count(*) as n from calculation").fetchone()["n"]
    assert n == 0
