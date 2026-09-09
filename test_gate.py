"""
Browser checks for the access gate and the admin page.

Script-style, like test_render.py and test_engine.py: run it as a program, not
under pytest. It needs a server with SERVE_STATIC=1 on the port below and a
config.js with supabaseUrl/supabaseAnonKey filled in.

    SERVE_STATIC=1 PORT=8011 python app.py &
    python test_gate.py

The API is stubbed with request interception, so this checks the frontend's own
logic - token stashing and stripping, the four gate states, role-dependent UI -
without needing a real Supabase project. External tile hosts are blocked so the
page reaches a quiet state.

The `hidden` checks here are not busywork: every element the gate toggles also
carries a class that sets `display`, and a class selector beats the browser's
built-in `[hidden] { display: none }`. That bug shipped once already.
"""

import os

from playwright.sync_api import sync_playwright

BASE = os.environ.get("GATE_TEST_BASE", "http://127.0.0.1:8011/static")
TILES = ["**cache.kartverket.no**", "**opencache.statkart.no**",
         "**tile.openstreetmap.org**", "**hoydedata.no**"]

SESSION = """
  localStorage.setItem('mb.session', JSON.stringify({
    access_token: 'fake', refresh_token: 'r', expires_at: Date.now() + 3600000 }));
"""

MEG_STRANGER = '{"email":"stranger@internet.com","status":"no_access"}'
MEG_MEMBER = '{"id":"u1","email":"kari@firma.no","role":"user","status":"active"}'
MEG_ADMIN = '{"id":"a1","email":"per@prosit.no","role":"superadmin","status":"active"}'
MEG_MEMBER_U2 = '{"id":"u2","email":"kari@firma.no","role":"user","status":"active"}'

ADMIN_ROUTES = {
    "**/api/invitasjon": (
        '{"invitasjoner":[{"id":"i1","label":"Veidekke pilot","email":null,'
        '"role_granted":"user","max_uses":8,"uses":2,"status":"active",'
        '"expires_at":"2026-10-01T10:00:00Z","created_at":"2026-09-01T10:00:00Z",'
        '"revoked_at":null,"created_by_email":"per@prosit.no",'
        '"redeemed_by":["a@x.no","b@x.no"]}]}'),
    "**/api/brukere": (
        '{"brukere":[{"id":"a1","email":"per@prosit.no","role":"superadmin",'
        '"status":"active","created_at":"2026-09-01T10:00:00Z",'
        '"last_seen_at":"2026-09-09T10:00:00Z","invited_by_email":null},'
        '{"id":"u2","email":"kari@firma.no","role":"user","status":"active",'
        '"created_at":"2026-09-02T10:00:00Z","last_seen_at":null,'
        '"invited_by_email":"per@prosit.no"}]}'),
    "**/api/revisjon": (
        '{"hendelser":[{"actor_email":"per@prosit.no","action":"invite.create",'
        '"target":"Veidekke pilot","detail":null,"created_at":"2026-09-01T10:00:00Z"}]}'),
}

FAILURES = []


def check(name, ok, extra=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {extra}" if extra != "" else ""))
    if not ok:
        FAILURES.append(name)


def page_for(browser, *, session=False, meg=None, routes=None):
    p = browser.new_page()
    for pat in TILES:
        p.route(pat, lambda route: route.abort())
    if session:
        p.add_init_script(SESSION)
    # The *_ matters: Playwright inspects the handler's arity and passes
    # (route, request) to anything that takes two arguments, so a bare
    # `lambda route, b=body` gets the Request bound to b.
    if meg:
        p.route("**/api/meg", lambda route, *_, b=meg: route.fulfill(
            status=200, content_type="application/json", body=b))
    for pat, body in (routes or {}).items():
        p.route(pat, lambda route, *_, b=body: route.fulfill(
            status=200, content_type="application/json", body=b))
    return p


def settle(page):
    """Wait until the gate has decided what to show."""
    page.wait_for_function(
        "() => { const g = document.getElementById('gate');"
        " return g && (g.hidden || g.innerHTML.trim().length > 0); }",
        timeout=15000)


with sync_playwright() as pw:
    browser = pw.chromium.launch()

    # --------------------------------------------- 1. invited, not signed in
    print("\n=== Invited visitor, not signed in ===")
    page = page_for(browser)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{BASE}/index.html?invitasjon=SECRET-TOKEN-abc123")
    settle(page)

    check("no uncaught JS errors", not errors, errors[:2])
    check("token stripped from the URL", "SECRET-TOKEN" not in page.url, page.url)
    stashed = page.evaluate("sessionStorage.getItem('mb.invite')")
    check("token stashed in sessionStorage", stashed == "SECRET-TOKEN-abc123")
    check("gate is visible", page.is_visible("#gate"))
    check("shows the invited wording", "Du er invitert" in page.inner_text("#gate"))
    check("app behind the gate is inert",
          page.evaluate("document.querySelector('.app').inert") is True)
    width = page.evaluate("document.getElementById('map').clientWidth")
    check("map sized correctly behind the gate", width > 200, f"{width}px")
    check("Google and Microsoft buttons present",
          page.locator("[data-provider=google]").count() == 1
          and page.locator("[data-provider=azure]").count() == 1)
    check("email-link form present", page.locator("#gate-email-form").count() == 1)
    check("referrer policy is no-referrer",
          page.evaluate("document.querySelector('meta[name=referrer]').content")
          == "no-referrer")
    check("account chip stays hidden while gated", page.is_hidden("#account"))

    # ------------------------------------------------- 2. uninvited visitor
    print("\n=== Uninvited visitor ===")
    p2 = page_for(browser)
    p2.goto(f"{BASE}/index.html")
    settle(p2)
    text = p2.inner_text("#gate")
    check("gate visible", p2.is_visible("#gate"))
    check("says access needs an invitation", "krever invitasjon" in text)
    check("does not claim they are invited", "Du er invitert" not in text)

    # -------------------------------------------- 3. signed in, no membership
    print("\n=== Signed in, no membership ===")
    p3 = page_for(browser, session=True, meg=MEG_STRANGER)
    p3.goto(f"{BASE}/index.html")
    settle(p3)
    t3 = p3.inner_text("#gate")
    check("shows the no-access screen", "Ingen tilgang" in t3)
    check("names the signed-in address", "stranger@internet.com" in t3)
    check("offers a sign-out", p3.locator("#gate-signout").count() == 1)
    check("app stays inert", p3.evaluate("document.querySelector('.app').inert") is True)

    # ------------------------------------------------------ 4. active member
    print("\n=== Active member ===")
    p4 = page_for(browser, session=True, meg=MEG_MEMBER)
    p4.goto(f"{BASE}/index.html")
    settle(p4)
    check("gate hidden", p4.is_hidden("#gate"))
    check("app is interactive again",
          p4.evaluate("document.querySelector('.app').inert") is False)
    check("account chip shows the address", "kari@firma.no" in p4.inner_text("#account"))
    check("no admin link for a normal user", p4.is_hidden("#account-admin"))
    check("no superadmin badge", p4.is_hidden("#account-badge"))
    # The regression test for the [hidden]-vs-class bug: a gate that is
    # "hidden" but still painted swallows every click on the map.
    check("nothing invisible covers the map",
          p4.evaluate("""() => {
            const r = document.getElementById('map').getBoundingClientRect();
            const el = document.elementFromPoint(r.left + r.width / 2,
                                                 r.top + r.height / 2);
            return !!el && !!el.closest('.map-wrap');
          }"""))

    # ---------------------------------------------------------- 5. superadmin
    print("\n=== Superadmin ===")
    p5 = page_for(browser, session=True, meg=MEG_ADMIN)
    p5.goto(f"{BASE}/index.html")
    settle(p5)
    check("admin link visible", p5.is_visible("#account-admin"))
    check("superadmin badge visible", p5.is_visible("#account-badge"))

    # --------------------------------------------------------- 6. admin page
    print("\n=== Admin page ===")
    p6 = page_for(browser, session=True, meg=MEG_ADMIN, routes=ADMIN_ROUTES)
    errs6 = []
    p6.on("pageerror", lambda e: errs6.append(str(e)))
    p6.goto(f"{BASE}/admin.html")
    p6.wait_for_selector("#invite-rows tr", timeout=15000)

    check("no uncaught JS errors", not errs6, errs6[:2])
    check("admin body visible", p6.is_visible("#admin-body"))
    check("invite row rendered", p6.locator("#invite-rows tr").count() == 1)
    check("shows uses as 2 / 8", "2 / 8" in p6.inner_text("#invite-rows"))
    check("shows who redeemed", "a@x.no" in p6.inner_text("#invite-rows"))
    check("revoke offered for an active invite",
          p6.locator("#invite-rows [data-revoke]").count() == 1)
    check("user rows rendered", p6.locator("#user-rows tr").count() == 2)
    check("own row marked, and not suspendable",
          "(deg)" in p6.inner_text("#user-rows")
          and p6.locator("#user-rows [data-toggle]").count() == 1)
    check("audit row rendered", p6.locator("#audit-rows tr").count() == 1)

    p6.fill("#inv-email", "kari@firma.no")
    p6.fill("#inv-uses", "5")
    p6.click("#invite-form button[type=submit]")
    # Scoped to #invite-out: the always-present "krever superadmin" notice in
    # #not-admin carries the same class, and matches first.
    p6.wait_for_selector("#invite-out .admin-msg.error", timeout=5000)
    check("bound + multi-use refused before any round-trip",
          "én gang" in p6.inner_text("#invite-out"))

    # ------------------------------------- 7. normal user on the admin page
    print("\n=== Normal user on the admin page ===")
    p7 = page_for(browser, session=True, meg=MEG_MEMBER_U2)
    p7.goto(f"{BASE}/admin.html")
    p7.wait_for_selector("#not-admin:not([hidden])", timeout=15000)
    check("admin body hidden", p7.is_hidden("#admin-body"))
    check("told it needs superadmin", p7.is_visible("#not-admin"))

    browser.close()

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED: {', '.join(FAILURES)}")
    raise SystemExit(1)
print("All frontend gate checks passed.")
