"""
Browser test: drives the real UI against a running server.

Catches the failures unit tests cannot see - JavaScript exceptions, a control
that is wired to nothing, a result panel that renders "undefined".

Run the server first, then:  python test_ui.py
"""

import re
import sys

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8000/static/index.html"
FAILURES = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1500, "height": 950})

    errors, console = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: console.append((m.type, m.text)) if m.type == "error" else None)

    # Keep the test hermetic: only the local server is allowed. Basemap tiles
    # come from Kartverket and are irrelevant to what is being verified, but
    # left unblocked they hang the page load in a sandbox with no egress.
    # The URLs are still recorded, so the zoom levels requested can be checked.
    tile_requests = []

    def block(route, request):
        tile_requests.append(request.url)
        route.abort()

    page.route(re.compile(r"^https?://(?!127\.0\.0\.1:8000)"), block)

    print("\n=== Load ===")
    page.goto(URL, wait_until="load", timeout=60000)
    page.wait_for_timeout(1200)
    check("page title", "Masseberegning" in page.title())
    check("no JS exceptions on load", not errors, "; ".join(errors[:2]))
    check("map container rendered", page.locator("#map .leaflet-pane").count() > 0)
    check("calculate button starts disabled", page.locator("#btn-calc").is_disabled())

    print("\n=== Controls ===")
    page.locator("#soil-depth").fill("6.5")
    page.locator("#soil-depth").dispatch_event("input")
    check("soil slider updates its readout", "6,5" in page.locator("#soil-out").inner_text(),
          page.locator("#soil-out").inner_text())

    page.locator("input[name=mode][value=fast]").check()
    check("fast kote reveals the level input", page.locator("#fixed-opts").is_visible())
    page.locator("input[name=mode][value=balansert]").check()
    check("balansert hides it again", page.locator("#fixed-opts").is_hidden())

    print("\n=== Place search ===")
    page.locator("#search").fill("Lillehammer")
    page.wait_for_timeout(1800)
    hits = page.locator("#search-results button")
    check("place search returns hits", hits.count() > 0, f"{hits.count()} treff")

    page.locator("#search").fill("61.1153, 10.4662")
    page.wait_for_timeout(400)
    check("coordinate paste is recognised",
          "Gå til" in page.locator("#search-results").inner_text())
    page.locator("#search-results button").first.click()
    page.wait_for_timeout(1200)

    print("\n=== Square + calculate ===")
    page.evaluate("map.setView([61.1153, 10.4662], 15)")
    page.wait_for_timeout(600)
    page.locator("#btn-square").click()          # first click reveals options
    page.locator("#square-size").fill("600")
    page.locator("#btn-square").click()          # second click builds the square
    page.wait_for_timeout(900)

    readout = page.locator("#area-readout").inner_text()
    check("area readout populated", "m²" in readout, readout.replace("\n", " "))
    check("calculate button enabled", page.locator("#btn-calc").is_enabled())

    # Kartverket's cache has no tiles past zoom 18 and answers deeper requests
    # with HTTP 400, which blanks the whole basemap. fitBounds must stay within
    # range, and the layer must declare maxNativeZoom so manual zooming upscales
    # rather than requesting tiles that do not exist.
    print("\n=== Basemap stays alive after drawing ===")
    z = page.evaluate("map.getZoom()")
    check("fitBounds does not exceed the basemap's real zoom", z <= 18, f"zoom {z}")

    native = page.evaluate(
        "Object.values(baseLayers).map(l => l.options.maxNativeZoom)")
    check("every base layer declares maxNativeZoom",
          all(n is not None for n in native), str(native))

    kv = [u for u in tile_requests if "cache.kartverket.no" in u]
    zooms = [int(m.group(1)) for m in
             (re.search(r"/webmercator/(\d+)/", u) for u in kv) if m]
    check("no Kartverket tile requested beyond zoom 18",
          not zooms or max(zooms) <= 18,
          f"{len(zooms)} tiles, max z={max(zooms) if zooms else 'n/a'}")

    # Zooming past the cache limit must still not produce a deeper request.
    page.evaluate("map.setZoom(21)")
    page.wait_for_timeout(1200)
    deep = [u for u in tile_requests if "cache.kartverket.no" in u]
    zooms2 = [int(m.group(1)) for m in
              (re.search(r"/webmercator/(\d+)/", u) for u in deep) if m]
    check("zooming to 21 still requests no tile beyond 18",
          not zooms2 or max(zooms2) <= 18,
          f"max z={max(zooms2) if zooms2 else 'n/a'}")
    page.evaluate("map.setZoom(16)")
    page.wait_for_timeout(500)

    page.locator("#btn-calc").click()
    page.wait_for_selector("#results:not([hidden])", timeout=180000)
    page.wait_for_timeout(1500)

    res = page.locator("#results").inner_text()
    check("no JS exceptions during calculation", not errors, "; ".join(errors[:2]))
    check("no undefined/NaN in output",
          not re.search(r"undefined|NaN|\[object", res),
          (re.search(r".{25}(undefined|NaN|\[object).{25}", res) or [""])[0])
    check("headline volume present", re.search(r"m³", res) is not None)
    check("planum level shown", "moh" in res)
    check("material table present", "Løsmasse" in res and "Fjell" in res)
    check("sensitivity chart drawn", page.locator("#results svg.chart path.curve").count() == 1)
    check("caveat shown", "Forbehold" in res)

    # This run is in local single-user mode, which has no accounts - so there
    # is nothing to own a saved calculation, and neither piece of that UI
    # should appear. The invariant is easy to break from the other side, where
    # everything is visible.
    check("no save block without an account", page.locator("#btn-save").count() == 0)
    check("mine beregninger hidden without an account", page.is_hidden("#saved"))

    print("\n=== Map overlay ===")
    check("cut/fill overlay added", page.locator("#map img.leaflet-image-layer").count() >= 1)
    check("legend visible", page.locator("#legend").is_visible())
    lmin = page.locator("#legend-min").inner_text()
    check("legend labelled with metres", "m" in lmin, lmin)

    page.locator("#opacity").fill("40")
    page.locator("#opacity").dispatch_event("input")
    opacity = page.evaluate("cutfillLayer.options.opacity")
    check("opacity slider drives the overlay", abs(opacity - 0.4) < 0.01, str(opacity))

    page.locator("#toggle-shade").check()
    page.wait_for_timeout(700)
    check("hillshade toggle adds a second overlay",
          page.locator("#map img.leaflet-image-layer").count() >= 2)

    print("\n=== Downloads ===")
    for label in ("Resultat (CSV)", "Dybdekart (GeoTIFF)"):
        href = page.locator(f"#results a:has-text('{label}')").get_attribute("href")
        r = page.request.get("http://127.0.0.1:8000" + href)
        check(f"{label} downloads", r.ok and len(r.body()) > 500,
              f"{r.status}, {len(r.body())} bytes")

    page.screenshot(path="/tmp/ui_result.png", full_page=False)
    page.locator(".panel").screenshot(path="/tmp/ui_panel.png")

    print("\n=== Reset ===")
    page.locator("#btn-clear").click()
    page.wait_for_timeout(500)
    check("reset hides results", page.locator("#results").is_hidden())
    check("reset removes overlays", page.locator("#map img.leaflet-image-layer").count() == 0)
    check("reset disables calculate", page.locator("#btn-calc").is_disabled())

    if console:
        print("\n  console errors:", console[:5])

    browser.close()

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("All UI checks passed.")
