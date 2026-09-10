"""
Browser checks for the access gate and the admin page.

Script-style, like test_render.py and test_engine.py: run it as a program, not
under pytest. It needs a server with SERVE_STATIC=1 on the port below and a
config.js with supabaseUrl/supabasePublishableKey filled in.

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

# The test serves its own config.js rather than relying on the repo's. The
# checked-in default has only Google enabled and no email form - correct for a
# fresh deployment, but it would make the assertions below depend on a value
# that changes for deployment reasons rather than behavioural ones. Here every
# sign-in method is on, so the gate is exercised fully.
CONFIG_JS = """
/* supplied by test_gate.py */
window.MB_CONFIG = {
  apiBase: '',
  supabaseUrl: 'https://test.supabase.co',
  supabasePublishableKey: 'sb_publishable_test',
  supabaseAnonKey: '',
  providers: ['google', 'azure'],
  allowEmailLink: true,
};
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
        '"revoked_at":null,"created_by_email":"per@prosit.no","email_domain":null,'
        '"has_link":true,"redeemed_by":["a@x.no","b@x.no"]},'
        '{"id":"i2","label":"VG","email":null,"email_domain":"vg.no",'
        '"role_granted":"user","max_uses":20,"uses":0,"status":"active",'
        '"expires_at":"2026-10-01T10:00:00Z","created_at":"2026-09-01T10:00:00Z",'
        '"revoked_at":null,"created_by_email":"per@prosit.no",'
        '"has_link":true,"redeemed_by":[]}]}'),
    # A separate pattern: "**/api/invitasjon" is anchored at the end, so it
    # does not match the per-invite link endpoint.
    "**/api/invitasjon/*/lenke":
        '{"url":"https://example.test/?invitasjon=REVEALED-TOKEN"}',
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

# Saved calculations. Same anchoring trick: the list pattern does not match
# "/api/beregninger/c1", so PATCH and DELETE get their own stub.
CALC_ROUTES = {
    "**/api/beregninger": (
        '{"beregninger":[{"id":"c1","name":"Tomt på Storhove",'
        '"summary":{"level":201.15,"area_m2":144270.0,"cut_bank_m3":534278.0,'
        '"fill_void_m3":485707.0,"net_bank_m3":48571.0,"resolution_m":1.0,'
        '"coverage":1.0,"mode":"balansert"},'
        '"created_at":"2026-09-08T09:00:00Z","updated_at":"2026-09-08T09:00:00Z"},'
        '{"id":"c2","name":"Riggplass","summary":{"level":98.0,"area_m2":8000.0,'
        '"cut_bank_m3":1200.0,"fill_void_m3":1200.0,"net_bank_m3":0.0,'
        '"resolution_m":1.0,"coverage":1.0,"mode":"balansert"},'
        '"created_at":"2026-09-07T09:00:00Z","updated_at":"2026-09-07T09:00:00Z"}]}'),
    "**/api/beregninger/*": '{"ok":true}',
}

def beregn_stub():
    """A real /api/beregn body, produced by the real engine.

    Hand-writing this JSON would test the frontend against my idea of the
    response rather than the response, and the save payload is assembled from
    a dozen fields of it. Only the parts that need a job on disk - the overlay
    images and the downloads - are faked.
    """
    import json

    import numpy as np

    import volumes

    z = np.linspace(198.0, 206.0, 900).astype("float64")
    p = volumes.Params(soil_depth=2.0, swell_soil=0.25, swell_rock=0.5,
                       shrinkage=0.1, truck_capacity=15.0)
    terrain = volumes.Terrain.from_values(z, 1.0)
    level = volumes.solve_balanced_level(terrain, p.shrinkage)

    r = volumes.compute(z, 1.0, level, p)
    hist, edges = np.histogram(z, bins=40)
    r.update(
        mode="balansert", resolution_m=1.0, coverage=1.0,
        polygon_area_m2=r["area_m2"],
        sensitivity=volumes.sensitivity(terrain, p),
        balanced_level=level,
        lowest_level=float(z[0]), highest_level=float(z[-1]),
        histogram={"counts": hist.tolist(), "edges": edges.tolist()},
        job_id="job-test",
        overlay={
            "cutfill_url": "/api/jobb/job-test/cutfill.png",
            "hillshade_url": "/api/jobb/job-test/hillshade.png",
            "bounds": [[61.11, 10.46], [61.12, 10.47]],
            "hillshade_bounds": [[61.11, 10.46], [61.12, 10.47]],
            "vmax": 4.0,
        },
        downloads={"geotiff": "/api/jobb/job-test/skjaering_fylling.tif",
                   "csv": "/api/jobb/job-test/resultat.csv"},
    )
    return json.dumps(r)


FAILURES = []


def check(name, ok, extra=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {extra}" if extra != "" else ""))
    if not ok:
        FAILURES.append(name)


def page_for(browser, *, session=False, meg=None, routes=None):
    p = browser.new_page()
    for pat in TILES:
        p.route(pat, lambda route: route.abort())
    p.route("**/config.js", lambda route, *_: route.fulfill(
        status=200, content_type="application/javascript", body=CONFIG_JS))
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
    check("saved calculations stay hidden while gated", page.is_hidden("#saved"))

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
    check("saved calculations available, and collapsed",
          p4.is_visible("#saved") and p4.evaluate(
              "document.getElementById('saved').open") is False)
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
    check("invite rows rendered", p6.locator("#invite-rows tr").count() == 2)
    check("shows uses as 2 / 8", "2 / 8" in p6.inner_text("#invite-rows"))
    check("shows who redeemed", "a@x.no" in p6.inner_text("#invite-rows"))
    check("revoke offered for an active invite",
          p6.locator("#invite-rows [data-revoke]").count() == 2)
    check("open link is described as open",
          "åpen lenke" in p6.inner_text("#invite-rows"))
    check("domain invite names the domain",
          "@vg.no" in p6.inner_text("#invite-rows"))

    # The link is fetched on demand, so it must not be in the list payload -
    # that is loaded on every page view, including over the shoulder.
    check("no token in the list payload",
          "REVEALED-TOKEN" not in p6.content())
    p6.locator("#invite-rows [data-copy]").first.click()
    # Headless Chromium may refuse the clipboard write; either outcome proves
    # the link was fetched and handed to the copy path.
    p6.wait_for_function(
        "() => { const b = document.querySelector('#invite-rows [data-copy]');"
        " return b && /Kopiert|manuelt/.test(b.textContent); }", timeout=5000)
    check("active invite link can be fetched again", True)
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

    # A domain rule is the exception: being usable more than once is the whole
    # point of it, so the same guard must let it through.
    posts = []
    p6.on("request", lambda r: posts.append(r.method + " " + r.url))
    p6.fill("#inv-email", "@vg.no")
    p6.fill("#inv-uses", "5")
    p6.click("#invite-form button[type=submit]")
    p6.wait_for_function(
        "() => !document.querySelector('#invite-out .admin-msg.error')",
        timeout=5000)
    check("domain + multi-use is allowed through",
          any(s.startswith("POST") and s.endswith("/api/invitasjon") for s in posts),
          str([s for s in posts if s.startswith("POST")])[:120])

    # ------------------------------------- 7. normal user on the admin page
    print("\n=== Normal user on the admin page ===")
    p7 = page_for(browser, session=True, meg=MEG_MEMBER_U2)
    p7.goto(f"{BASE}/admin.html")
    p7.wait_for_selector("#not-admin:not([hidden])", timeout=15000)
    check("admin body hidden", p7.is_hidden("#admin-body"))
    check("told it needs superadmin", p7.is_visible("#not-admin"))

    # ------------------------------------------------ 8. mine beregninger
    print("\n=== Mine beregninger ===")
    p8 = page_for(browser, session=True, meg=MEG_MEMBER, routes=CALC_ROUTES)
    errs8 = []
    calls = []
    p8.on("pageerror", lambda e: errs8.append(str(e)))
    p8.on("request", lambda r: "/api/" in r.url
          and calls.append(r.method + " " + r.url.split("/api/")[-1]))
    p8.goto(f"{BASE}/index.html")
    settle(p8)

    # Loaded on first open, not on every page view: most visits never touch it.
    check("list not fetched before the section is opened",
          not any(c.endswith("beregninger") for c in calls))

    p8.click("#saved > summary")
    p8.wait_for_selector(".saved-item", timeout=10000)

    check("no uncaught JS errors", not errs8, errs8[:2])
    check("both saved rows rendered", p8.locator(".saved-item").count() == 2)
    check("newest first", "Storhove" in p8.locator(".saved-name").first.inner_text())
    check("count shown in the summary", "(2)" in p8.inner_text("#saved > summary"))
    check("area and balance summarised",
          "144,3 daa" in p8.inner_text(".saved-item")
          and "overskudd" in p8.inner_text(".saved-item"))
    check("a balanced calculation says so",
          "i balanse" in p8.locator(".saved-item").nth(1).inner_text())

    # Delete is two clicks, not a confirm() dialog - a dialog would block the
    # page, and an accidental single click must not destroy saved work.
    calls.clear()
    p8.locator('.saved-item [data-act=del]').first.click()
    check("first delete click only arms the button",
          "Bekreft" in p8.locator('.saved-item [data-act=del]').first.inner_text()
          and not any(c.startswith("DELETE") for c in calls))
    p8.locator('.saved-item [data-act=del]').first.click()
    p8.wait_for_timeout(500)
    check("second click deletes", any(c.startswith("DELETE") for c in calls),
          str([c for c in calls if c.startswith("DELETE")])[:80])

    # Rename edits in place rather than through prompt(), for the same reason.
    p8.locator('.saved-item [data-act=rename]').first.click()
    p8.wait_for_selector(".saved-rename", timeout=5000)
    check("rename field carries the current name",
          p8.input_value(".saved-rename") == "Tomt på Storhove")
    calls.clear()
    p8.fill(".saved-rename", "Storhove nord")
    p8.locator(".saved-rename").press("Enter")
    p8.wait_for_timeout(400)
    patches = [c for c in calls if c.startswith("PATCH")]
    # Exactly one: Enter re-renders the list, which removes the field and
    # fires blur, and that used to send the rename a second time.
    check("rename is a single PATCH", len(patches) == 1, str(calls)[:110])

    # -------------------------------- 9. save and restore, end to end
    print("\n=== Save, then open a saved calculation ===")
    RING = [[10.46, 61.11], [10.47, 61.11], [10.47, 61.12], [10.46, 61.12]]
    saved_rows = []          # what the server would have stored
    posted = []              # every POST body, for inspection

    p9 = page_for(browser, session=True, meg=MEG_MEMBER)
    errs9 = []
    p9.on("pageerror", lambda e: errs9.append(str(e)))
    # Job files belong to a job that does not exist here.
    p9.route("**/api/jobb/**", lambda route: route.abort())
    p9.route("**/api/beregn", lambda route, *_, b=beregn_stub(): route.fulfill(
        status=200, content_type="application/json", body=b))

    def on_calc_collection(route, request):
        import json as _json
        if request.method == "POST":
            body = request.post_data_json
            posted.append(body)
            saved_rows.append(dict(body, id="c9",
                                   created_at="2026-09-09T09:00:00Z",
                                   updated_at="2026-09-09T09:00:00Z"))
            route.fulfill(status=201, content_type="application/json",
                          body=_json.dumps(saved_rows[-1]))
            return
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(
            {"beregninger": [{k: r[k] for k in
                              ("id", "name", "summary", "created_at", "updated_at")}
                             for r in saved_rows]}))

    def on_calc_item(route, *_):
        import json as _json
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps(saved_rows[-1]) if saved_rows else "{}")

    p9.route("**/api/beregninger", on_calc_collection)
    p9.route("**/api/beregninger/*", on_calc_item)

    p9.goto(f"{BASE}/index.html")
    settle(p9)

    # Draw the polygon the way the map would, then run the real click path.
    # An explicit grid rather than "Automatisk", so the round trip is checked
    # on a value and not on a null.
    p9.evaluate("ring => setPolygon(ring.map(p => [p[1], p[0]]))", RING)
    p9.evaluate("document.querySelector('.advanced').open = true")
    p9.select_option("#resolution", "2")
    p9.click("#btn-calc")
    p9.wait_for_selector("#results:not([hidden])", timeout=15000)

    check("no uncaught JS errors", not errs9, errs9[:2])
    check("result rendered", "Massebalanse" in p9.inner_text("#results"))
    check("save block offered to a signed-in member",
          p9.locator("#btn-save").count() == 1)
    check("a name is suggested", len(p9.input_value("#save-name")) > 3,
          p9.input_value("#save-name"))

    p9.fill("#save-name", "Storhove sør")
    p9.click("#btn-save")
    p9.wait_for_function("() => document.getElementById('btn-save').textContent"
                         " === 'Lagret'", timeout=5000)

    body = posted[0] if posted else {}
    check("saved under the given name", body.get("name") == "Storhove sør")
    # The order matters more than it looks: /api/beregn takes [lon, lat] and
    # Leaflet takes [lat, lon]. Getting this wrong puts the polygon in the sea
    # off Somalia, and only on restore.
    check("polygon stored as [lon, lat], matching /api/beregn",
          body.get("polygon") and
          abs(body["polygon"][0][0] - 10.46) < 1e-6 and
          abs(body["polygon"][0][1] - 61.11) < 1e-6,
          str(body.get("polygon", [])[:1]))
    check("parameters stored", (body.get("params") or {}).get("mode") == "balansert"
          and body["params"]["soil_depth"] == 2
          and body["params"]["resolution"] == 2)
    check("summary carries the headline figures",
          set(("level", "area_m2", "cut_bank_m3", "fill_void_m3", "net_bank_m3"))
          <= set((body.get("summary") or {}).keys()))

    # Now change every parameter, then open the saved row and check they all
    # come back - this is the only thing standing between a restored
    # calculation and a subtly different one.
    p9.evaluate("""() => {
      document.querySelector('input[name=mode][value=fast]').checked = true;
      document.getElementById('fixed-level').value = '150';
      document.getElementById('soil-depth').value = '7';
      document.getElementById('swell-soil').value = '90';
      document.getElementById('swell-rock').value = '99';
      document.getElementById('shrinkage').value = '40';
      document.getElementById('truck').value = '55';
      document.getElementById('resolution').value = '10';
    }""")

    p9.click("#saved > summary")
    p9.wait_for_selector(".saved-item", timeout=10000)
    check("the new row is in the list", "Storhove sør" in p9.inner_text("#saved-list"))

    p9.locator('.saved-item [data-act=open]').first.click()
    p9.wait_for_function(
        "() => document.getElementById('btn-calc').textContent === 'Beregn masser'"
        " && !document.getElementById('results').hidden", timeout=15000)

    check("polygon restored in the right place", p9.evaluate("""() => {
        const b = state.polygon.getBounds();
        return Math.abs(b.getSouth() - 61.11) < 1e-6
            && Math.abs(b.getWest() - 10.46) < 1e-6;
      }"""), p9.evaluate("state.polygon.getBounds().toBBoxString()"))
    check("mode restored", p9.evaluate(
        "document.querySelector('input[name=mode]:checked').value") == "balansert")
    check("fixed-kote panel hidden again", p9.is_hidden("#fixed-opts"))
    check("all parameters restored", p9.evaluate("""() => {
        const v = id => document.getElementById(id).value;
        return v('soil-depth') === '2' && v('swell-soil') === '25'
            && v('swell-rock') === '50' && v('shrinkage') === '10'
            && v('truck') === '15' && v('resolution') === '2';
      }"""), p9.evaluate("""() => ['soil-depth','swell-soil','swell-rock',
        'shrinkage','truck','resolution'].map(i =>
        document.getElementById(i).value).join('/')"""))
    check("slider readout follows the restored value",
          "2,0 m" in p9.inner_text("#soil-out"), p9.inner_text("#soil-out"))
    check("it was re-run, not restored from stored numbers",
          len(posted) == 1 and "Massebalanse" in p9.inner_text("#results"))
    check("name prefilled for the reopened calculation",
          p9.input_value("#save-name") == "Storhove sør")
    check("still no uncaught JS errors", not errs9, errs9[:2])

    browser.close()

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED: {', '.join(FAILURES)}")
    raise SystemExit(1)
print("All frontend gate checks passed.")
