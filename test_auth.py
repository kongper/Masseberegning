"""
Endpoint-level authorization tests.

These go through the real FastAPI app, so they exercise the actual dependency
chain rather than the functions in isolation. The point is the wall: with no
membership row, a perfectly valid token gets nothing.

None of these reach Kartverket - the guards reject before any handler runs.
"""

from __future__ import annotations

import uuid

import pytest

from conftest import needs_db

pytestmark = needs_db

CALC_BODY = {
    "polygon": [[10.46, 61.11], [10.47, 61.11], [10.47, 61.12], [10.46, 61.12]],
    "mode": "balansert",
}

# Every endpoint that requires an active membership.
MEMBER_ENDPOINTS = [
    ("POST", "/api/beregn", CALC_BODY),
    ("GET", "/api/hoyde?lon=10.46&lat=61.11", None),
    ("GET", "/api/sok?q=lillehammer", None),
]

# Every endpoint that requires superadmin.
ADMIN_ENDPOINTS = [
    ("POST", "/api/invitasjon", {"label": "x"}),
    ("GET", "/api/invitasjon", None),
    ("GET", "/api/brukere", None),
    ("GET", "/api/revisjon", None),
    ("DELETE", f"/api/invitasjon/{uuid.uuid4()}", None),
]


def call(client, method, path, body=None, headers=None):
    return client.request(method, path, json=body, headers=headers or {})


# ------------------------------------------------------------------ no token


@pytest.mark.parametrize("method,path,body", MEMBER_ENDPOINTS + ADMIN_ENDPOINTS)
def test_no_token_is_401(client, method, path, body):
    r = call(client, method, path, body)
    assert r.status_code == 401


def test_garbage_token_is_401(client):
    r = call(client, "GET", "/api/sok?q=x",
             headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


def test_expired_token_is_401(client, token, new_id):
    tok = token(new_id(), "kari@firma.no", expired=True)
    r = call(client, "GET", "/api/sok?q=x", headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 401
    assert "utløpt" in r.json()["detail"].lower()


def test_token_signed_with_the_wrong_key_is_401(client, new_id):
    import time

    import jwt

    tok = jwt.encode(
        {"sub": new_id(), "email": "kari@firma.no", "aud": "authenticated",
         "exp": int(time.time()) + 600},
        "a-different-secret",
        algorithm="HS256",
    )
    r = call(client, "GET", "/api/sok?q=x", headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 401


def test_token_for_the_wrong_audience_is_401(client, token, new_id):
    tok = token(new_id(), "kari@firma.no", audience="some-other-app")
    r = call(client, "GET", "/api/sok?q=x", headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 401


def test_unverified_email_is_refused(client, token, new_id):
    """Invite binding compares against this address, so an explicit false matters."""
    tok = token(new_id(), "kari@firma.no", email_verified=False)
    r = call(client, "GET", "/api/sok?q=x", headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 403
    assert "bekreftet" in r.json()["detail"].lower()


# -------------------------------------------------- valid token, no invite


@pytest.mark.parametrize("method,path,body", MEMBER_ENDPOINTS)
def test_valid_token_without_membership_is_403(client, bearer, new_id, method, path, body):
    """The whole design in one test: authentication is not authorization."""
    r = call(client, method, path, body, bearer(new_id(), "stranger@internet.com"))
    assert r.status_code == 403
    assert "invitasjon" in r.json()["detail"].lower()


def test_meg_reports_no_access_rather_than_failing(client, bearer, new_id):
    """The frontend needs to distinguish 'not signed in' from 'no access'."""
    r = call(client, "GET", "/api/meg", None, bearer(new_id(), "stranger@internet.com"))
    assert r.status_code == 200
    assert r.json() == {"email": "stranger@internet.com", "status": "no_access"}


# ------------------------------------------------------------- bootstrap


def test_configured_superadmin_needs_no_invite(client, bearer, new_id):
    r = call(client, "GET", "/api/meg", None, bearer(new_id(), "boss@prosit.no"))
    assert r.status_code == 200
    assert r.json()["role"] == "superadmin"


def test_bootstrap_is_case_insensitive_on_the_email(client, bearer, new_id):
    r = call(client, "GET", "/api/meg", None, bearer(new_id(), "BOSS@Prosit.NO"))
    assert r.json()["role"] == "superadmin"


# --------------------------------------------------------- role separation


@pytest.fixture
def admin_headers(client, bearer, new_id):
    h = bearer(new_id(), "boss@prosit.no")
    client.get("/api/meg", headers=h)  # triggers the bootstrap
    return h


@pytest.fixture
def member_headers(client, admin_headers, bearer, new_id):
    """An ordinary member, created the way a real one is: via an invitation."""
    made = client.post("/api/invitasjon", json={"label": "member"},
                       headers=admin_headers).json()
    token_value = made["url"].split("invitasjon=")[1]

    h = bearer(new_id(), "kari@firma.no")
    r = client.post("/api/invitasjon/innloes", json={"token": token_value}, headers=h)
    assert r.status_code == 200, r.text
    return h


@pytest.mark.parametrize("method,path,body", ADMIN_ENDPOINTS)
def test_ordinary_member_cannot_reach_admin_endpoints(client, member_headers,
                                                      method, path, body):
    r = call(client, method, path, body, member_headers)
    assert r.status_code == 403
    assert "superadmin" in r.json()["detail"].lower()


def test_member_can_reach_the_app(client, member_headers):
    """Not a full calculation - just proof the guard lets them through."""
    r = call(client, "GET", "/api/meg", None, member_headers)
    assert r.json()["status"] == "active"
    assert r.json()["role"] == "user"


def test_suspended_member_is_locked_out(client, database, member_headers):
    me = client.get("/api/meg", headers=member_headers).json()
    database.update_user(me["id"], status="suspended")

    r = call(client, "GET", "/api/sok?q=x", None, member_headers)
    assert r.status_code == 403
    assert "sperret" in r.json()["detail"].lower()


# ------------------------------------------------- invite flow over HTTP


def test_invite_link_points_at_the_frontend(client, admin_headers):
    made = client.post("/api/invitasjon", json={"label": "x"},
                       headers=admin_headers).json()
    assert made["url"].startswith("https://masseberegning.example.test/?invitasjon=")


def test_created_invite_is_returned_once_and_not_listed_again(client, admin_headers):
    made = client.post("/api/invitasjon", json={"label": "x"},
                       headers=admin_headers).json()
    listed = client.get("/api/invitasjon", headers=admin_headers).json()["invitasjoner"]

    assert "url" in made
    assert all("url" not in i and "token_hash" not in i for i in listed)


def test_bound_invite_with_multiple_uses_is_refused_with_an_explanation(client, admin_headers):
    r = client.post("/api/invitasjon",
                    json={"email": "kari@firma.no", "max_uses": 5},
                    headers=admin_headers)
    assert r.status_code == 400
    assert "én gang" in r.json()["detail"]


def test_expiry_beyond_the_maximum_is_refused(client, admin_headers):
    r = client.post("/api/invitasjon", json={"label": "x", "expires_in_days": 9999},
                    headers=admin_headers)
    assert r.status_code == 400


def test_wrong_address_message_names_the_bound_address(client, admin_headers, bearer, new_id):
    """Deliberate disclosure: whoever holds the link was already told who it is for."""
    made = client.post("/api/invitasjon", json={"email": "kari@firma.no"},
                       headers=admin_headers).json()
    tok = made["url"].split("invitasjon=")[1]

    r = client.post("/api/invitasjon/innloes", json={"token": tok},
                    headers=bearer(new_id(), "someone@else.no"))

    assert r.status_code == 403
    assert "kari@firma.no" in r.json()["detail"]


def test_invalid_token_gives_one_generic_message(client, bearer, new_id):
    r = client.post("/api/invitasjon/innloes", json={"token": "x" * 40},
                    headers=bearer(new_id(), "a@b.no"))
    assert r.status_code == 400
    detail = r.json()["detail"].lower()
    # No hint about which failure it was.
    for leak in ("utløpt", "brukt", "trukket", "finnes ikke"):
        assert leak not in detail


def test_superadmin_cannot_demote_themselves(client, admin_headers):
    me = client.get("/api/meg", headers=admin_headers).json()
    r = client.patch(f"/api/brukere/{me['id']}", json={"role": "user"},
                     headers=admin_headers)
    assert r.status_code == 400
    assert "egen konto" in r.json()["detail"]


def test_superadmin_cannot_suspend_themselves(client, admin_headers):
    me = client.get("/api/meg", headers=admin_headers).json()
    r = client.patch(f"/api/brukere/{me['id']}", json={"status": "suspended"},
                     headers=admin_headers)
    assert r.status_code == 400


def test_superadmin_cannot_delete_themselves(client, admin_headers):
    me = client.get("/api/meg", headers=admin_headers).json()
    r = client.delete(f"/api/brukere/{me['id']}", headers=admin_headers)
    assert r.status_code == 400


def test_one_superadmin_can_suspend_another(client, admin_headers, database, new_id):
    """The last-superadmin guard must not block the ordinary case.

    Note what this implies: because the actor is always an active superadmin,
    removing someone *else* can never bring the count to zero, so the
    count_active_superadmins guard in invites.py is only reachable via the self
    path — which the self-guard catches first. It is kept as defence in depth
    for any future path that is not self-initiated, not because it fires today.
    """
    other = database.ensure_bootstrap_superadmin(new_id(), "temp@prosit.no")

    r = client.patch(f"/api/brukere/{other.id}", json={"status": "suspended"},
                     headers=admin_headers)

    assert r.status_code == 200
    assert database.get_user(other.id).status == "suspended"
    assert database.count_active_superadmins() == 1


def test_rate_limit_applies_per_user(client, member_headers):
    """CALC_PER_MINUTE is 3 in the test config.

    Uses a degenerate (collinear) polygon: it passes Pydantic validation, so
    the handler runs and the limiter counts the call, but it fails at the
    zero-area check before any Kartverket request. No network in this test.
    """
    import app as appmod

    appmod._calc_minute.reset()
    appmod._calc_day.reset()

    flat = {"polygon": [[10.0, 61.0], [10.1, 61.0], [10.2, 61.0]], "mode": "balansert"}
    statuses = [
        client.post("/api/beregn", json=flat, headers=member_headers).status_code
        for _ in range(5)
    ]

    assert statuses[:3] == [400, 400, 400], f"unexpected early failures: {statuses}"
    assert statuses[3] == 429, f"limit fired at the wrong point: {statuses}"

    appmod._calc_minute.reset()
    appmod._calc_day.reset()


def test_junk_forwarded_for_does_not_break_redemption(client, admin_headers,
                                                      bearer, new_id):
    """X-Forwarded-For is client-supplied, and the audit column is `inet`.

    Passing the header value through unchecked made the INSERT fail, which
    turned a junk header into a 500 on the one endpoint a new user must reach.
    """
    made = client.post("/api/invitasjon", json={"label": "x"},
                       headers=admin_headers).json()
    tok = made["url"].split("invitasjon=")[1]

    headers = bearer(new_id(), "kari@firma.no")
    headers["X-Forwarded-For"] = "not-an-ip-address, ; drop table"

    r = client.post("/api/invitasjon/innloes", json={"token": tok}, headers=headers)

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"


@pytest.mark.parametrize("raw,expected", [
    ("203.0.113.7", "203.0.113.7"),
    ("203.0.113.7:41234", "203.0.113.7"),
    ("  203.0.113.7  ", "203.0.113.7"),
    ("[2001:db8::1]:443", "2001:db8::1"),
    ("2001:db8::1", "2001:db8::1"),
    ("testclient", None),
    ("", None),
    (None, None),
    ("999.1.1.1", None),
])
def test_as_inet_normalises_or_discards(database, raw, expected):
    assert database.as_inet(raw) == expected


def test_jwks_path_verifies_an_asymmetric_token(monkeypatch, new_id):
    """The endpoint tests use HS256; production uses RS256 against a JWKS.

    This covers the branch that actually runs in production, with a locally
    generated key standing in for the provider's.
    """
    import time
    import types

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    import auth as authmod
    from config import settings

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        {"sub": new_id(), "email": "kari@firma.no", "aud": "authenticated",
         "iss": "https://test.supabase.co/auth/v1", "exp": int(time.time()) + 600},
        key,
        algorithm="RS256",
    )

    monkeypatch.setattr(settings, "jwks_url", "https://test.supabase.co/jwks")
    monkeypatch.setattr(
        authmod, "_jwks",
        lambda: types.SimpleNamespace(
            get_signing_key_from_jwt=lambda _t: types.SimpleNamespace(key=key.public_key())
        ),
    )

    claims = authmod.decode_token(token)
    assert claims["email"] == "kari@firma.no"

    # And a token signed by a different key is refused on the same path.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {"sub": new_id(), "email": "attacker@evil.test", "aud": "authenticated",
         "iss": "https://test.supabase.co/auth/v1", "exp": int(time.time()) + 600},
        other,
        algorithm="RS256",
    )
    with pytest.raises(Exception) as exc:
        authmod.decode_token(forged)
    assert getattr(exc.value, "status_code", None) == 401


def test_area_cap_rejects_an_oversized_polygon(client, member_headers):
    """A box around a municipality is not a site plan.

    MAX_AREA_M2 defaults to 10 km2; this box is roughly 55 x 110 km. The cap is
    checked before the DEM fetch, so this test makes no network request either.
    """
    import app as appmod

    appmod._calc_minute.reset()
    huge = {
        "polygon": [[10.0, 61.0], [11.0, 61.0], [11.0, 62.0], [10.0, 62.0]],
        "mode": "balansert",
    }
    r = client.post("/api/beregn", json=huge, headers=member_headers)

    assert r.status_code == 422
    assert "km²" in r.json()["detail"]
    appmod._calc_minute.reset()


# ------------------------------------------- domain invites over HTTP


def test_at_domain_in_the_email_field_creates_a_domain_invite(client, admin_headers):
    r = client.post("/api/invitasjon",
                    json={"label": "VG", "email": "@vg.no", "max_uses": 8},
                    headers=admin_headers)
    assert r.status_code == 201, r.text
    assert r.json()["email_domain"] == "vg.no"
    assert r.json()["email"] is None


def test_a_domain_invite_may_be_multi_use(client, admin_headers):
    """The single-use rule is for addresses. A domain link is meant to be shared."""
    r = client.post("/api/invitasjon", json={"email": "@vg.no", "max_uses": 25},
                    headers=admin_headers)
    assert r.status_code == 201
    assert r.json()["max_uses"] == 25


def test_a_bare_word_in_the_email_field_is_refused(client, admin_headers):
    r = client.post("/api/invitasjon", json={"email": "vg"}, headers=admin_headers)
    assert r.status_code == 400
    assert "domene" in r.json()["detail"] or "e-postadresse" in r.json()["detail"]


def test_the_multi_use_error_suggests_the_domain_form(client, admin_headers):
    """The message should teach the feature, not just refuse."""
    r = client.post("/api/invitasjon",
                    json={"email": "kari@vg.no", "max_uses": 5},
                    headers=admin_headers)
    assert r.status_code == 400
    assert "@vg.no" in r.json()["detail"] or "domenet" in r.json()["detail"]


def test_wrong_domain_message_names_the_domain(client, admin_headers, bearer, new_id):
    made = client.post("/api/invitasjon", json={"email": "@vg.no", "max_uses": 5},
                       headers=admin_headers).json()
    tok = made["url"].split("invitasjon=")[1]

    r = client.post("/api/invitasjon/innloes", json={"token": tok},
                    headers=bearer(new_id(), "kari@nrk.no"))

    assert r.status_code == 403
    assert "@vg.no" in r.json()["detail"]


def test_someone_at_the_domain_gets_in(client, admin_headers, bearer, new_id):
    made = client.post("/api/invitasjon", json={"email": "@vg.no", "max_uses": 5},
                       headers=admin_headers).json()
    tok = made["url"].split("invitasjon=")[1]

    r = client.post("/api/invitasjon/innloes", json={"token": tok},
                    headers=bearer(new_id(), "kari@vg.no"))

    assert r.status_code == 200
    assert r.json()["status"] == "active"


# ------------------------------------------- revealing links over HTTP


def test_the_link_can_be_fetched_again_and_is_audited(client, admin_headers):
    made = client.post("/api/invitasjon", json={"label": "team", "max_uses": 4},
                       headers=admin_headers).json()

    again = client.get(f"/api/invitasjon/{made['id']}/lenke", headers=admin_headers)

    assert again.status_code == 200
    assert again.json()["url"] == made["url"], "must be the same link, not a new one"

    events = client.get("/api/revisjon", headers=admin_headers).json()["hendelser"]
    assert any(e["action"] == "invite.reveal" for e in events)


def test_the_list_never_carries_the_token(client, admin_headers):
    """The list is fetched on every page load; secrets must not ride along."""
    client.post("/api/invitasjon", json={"label": "team", "max_uses": 4},
                headers=admin_headers)
    body = client.get("/api/invitasjon", headers=admin_headers).text

    listed = client.get("/api/invitasjon", headers=admin_headers).json()["invitasjoner"][0]
    assert listed["has_link"] is True
    assert "token" not in body and "url" not in body


def test_a_revoked_invite_offers_no_link(client, admin_headers):
    made = client.post("/api/invitasjon", json={"label": "x", "max_uses": 4},
                       headers=admin_headers).json()
    client.delete(f"/api/invitasjon/{made['id']}", headers=admin_headers)

    r = client.get(f"/api/invitasjon/{made['id']}/lenke", headers=admin_headers)
    assert r.status_code == 404


def test_an_ordinary_member_cannot_fetch_a_link(client, admin_headers, member_headers):
    made = client.post("/api/invitasjon", json={"label": "x", "max_uses": 4},
                       headers=admin_headers).json()

    r = client.get(f"/api/invitasjon/{made['id']}/lenke", headers=member_headers)
    assert r.status_code == 403


# --------------------------------------- saved calculations over HTTP


CALC = {
    "name": "Storhove",
    "polygon": [[10.46, 61.11], [10.47, 61.11], [10.47, 61.12], [10.46, 61.12]],
    "params": {"mode": "balansert", "soil_depth": 2.0},
    "summary": {"level": 201.15, "area_m2": 144270.0, "cut_bank_m3": 534278.0,
                "fill_void_m3": 485707.0, "net_bank_m3": 0.0},
}


def test_save_list_open_delete(client, member_headers):
    saved = client.post("/api/beregninger", json=CALC, headers=member_headers)
    assert saved.status_code == 201, saved.text
    cid = saved.json()["id"]

    listed = client.get("/api/beregninger", headers=member_headers).json()["beregninger"]
    assert [c["name"] for c in listed] == ["Storhove"]

    opened = client.get(f"/api/beregninger/{cid}", headers=member_headers).json()
    assert opened["polygon"] == CALC["polygon"]
    assert opened["params"]["mode"] == "balansert"

    assert client.delete(f"/api/beregninger/{cid}", headers=member_headers).status_code == 200
    assert client.get(f"/api/beregninger/{cid}", headers=member_headers).status_code == 404


def test_unknown_parameters_are_dropped_not_stored(client, member_headers):
    """A jsonb column that gets fed back to the calculator needs a whitelist."""
    body = dict(CALC)
    body["params"] = {"mode": "laveste", "evil": "'; drop table calculation --",
                      "resolution": 2}
    cid = client.post("/api/beregninger", json=body, headers=member_headers).json()["id"]

    opened = client.get(f"/api/beregninger/{cid}", headers=member_headers).json()
    assert opened["params"] == {"mode": "laveste", "resolution": 2}


def test_saving_requires_membership(client, bearer, new_id):
    r = client.post("/api/beregninger", json=CALC,
                    headers=bearer(new_id(), "stranger@internet.com"))
    assert r.status_code == 403


def test_a_calculation_is_invisible_to_another_member(client, member_headers,
                                                      admin_headers):
    """Superadmin is not a back door into someone's saved work."""
    cid = client.post("/api/beregninger", json=CALC,
                      headers=member_headers).json()["id"]

    assert client.get(f"/api/beregninger/{cid}", headers=admin_headers).status_code == 404
    assert client.delete(f"/api/beregninger/{cid}", headers=admin_headers).status_code == 404
    assert client.get("/api/beregninger", headers=admin_headers).json()["beregninger"] == []


def test_rename(client, member_headers):
    cid = client.post("/api/beregninger", json=CALC, headers=member_headers).json()["id"]
    r = client.patch(f"/api/beregninger/{cid}", json={"name": "Storhove sør"},
                     headers=member_headers)
    assert r.status_code == 200 and r.json()["name"] == "Storhove sør"
