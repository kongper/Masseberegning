# Masseberegning

Web application for estimating how much material must be moved to flatten an
area of Norwegian terrain. Draw a polygon on the map, choose a target surface
level, set the soil depth, and the app pulls Kartverket's 1 m bare-earth
terrain model for that footprint and computes cut and fill volumes.

The interface is Norwegian; the code and comments are English.

---

## Running it

There are two ways to run this, and they are quite different.

### Locally, one user, no sign-in

On Windows, double-click `start.bat`. Otherwise:

```bash
pip install -r requirements.txt
LOCAL_SINGLE_USER=1 SERVE_STATIC=1 python app.py
```

Then open <http://127.0.0.1:8000>. This is the app in its original shape: one
person, one machine, no accounts and no database. `LOCAL_SINGLE_USER=1`
switches access control off entirely, so the app refuses to accept that flag
on anything but a loopback address, and refuses it alongside `DATABASE_URL` or
`ALLOWED_ORIGINS`.

### Deployed, invite-only

The static frontend goes on GitHub Pages, the API runs as a container, and
Supabase provides both Postgres and sign-in with Google, Microsoft or an email
link. Registration is invite-only: only superadmins can issue invitation
links. See **[README-DEPLOY.md](README-DEPLOY.md)**.

On Windows, `rasterio` and `pyproj` install from wheels without needing GDAL
separately. If `pip` tries to build from source, upgrade pip first
(`python -m pip install --upgrade pip`).

The app needs outbound HTTPS to:

| Host | Purpose |
|---|---|
| `hoydedata.no` | elevation data (DTM ImageServer) |
| `cache.kartverket.no` | basemap tiles |
| `ws.geonorge.no` | place-name search |
| `opencache.statkart.no` | aerial imagery (optional layer) |

Leaflet is vendored under `static/vendor/`, so no CDN access is required.

Kartverket's tile cache holds nothing beyond zoom 18 and answers deeper
requests with HTTP 400, so the base layers declare `maxNativeZoom: 18`.
Zooming in further upscales the deepest real tile — it goes soft, but the map
stays there. The cut/fill overlay is drawn at full resolution regardless.

---

## How to use it

**1. Find the area.** Search for a place name, or paste coordinates straight
from Google Maps — both `61.1153, 10.4662` and `61°06'55.1"N 10°27'58.6"E`
are understood. Then either draw a polygon, or press **Rute rundt punkt** to
drop a square of a given side length around the map centre.

**2. Choose the finished level.**

| Mode | What it does |
|---|---|
| **Balansert** | Solves for the level where the cut exactly feeds the fill — no material on or off site |
| **Laveste punkt** | Cuts everything down to the lowest point in the polygon — no fill, maximum export |
| **Fast kote** | You give the finished level in metres above sea level |

**3. Set the soil depth.** The slider says how deep the loose material goes.
Everything cut within that depth of the existing surface counts as soil;
anything deeper counts as rock. This is applied per grid cell, so a shallow
cut is all soil while a deep cut passes through soil into rock.

Under **Avanserte parametere**: excavation swell for soil and rock, compaction
shrinkage, lorry capacity, and grid resolution.

---

## Method

The polygon is reprojected to EPSG:25833 (UTM 33N, ETRS89) and snapped to a
regular grid. Kartverket's DTM is sampled onto that grid, the polygon is
rasterised, and every cell inside contributes `(elevation − target) × cell area`.

**Target level.** `Fast` is taken as given and `laveste` is the minimum
elevation. `Balansert` solves `cut(L) = fill(L) × (1 + shrinkage)` by bisection.
With zero shrinkage this converges on the mean elevation, which is the textbook
balance point. With a positive shrinkage the level sits slightly *below* the
mean, because a fill consumes more bank material than its own volume, so the
design has to dig deeper to feed it.

**Material split.** For each cell, `soil = min(cut depth, soil depth)` and
`rock = cut depth − soil`.

**Volume vocabulary.**

- *Fast* (bank) — in-situ volume straight off the terrain model. The primary number.
- *Løst* (loose) — bank volume after excavation swell. This is what gets trucked.
  Defaults: 25 % for soil, 50 % for blasted rock.
- *Massebehov fylling* — the bank volume needed to build the fill, i.e. the
  geometric void plus compaction shrinkage (default 10 %).

Material moved into the fill is drawn from the cut in the same proportion as
the cut's own soil/rock composition — a neutral assumption. If the project
specifies rock fill, change `compute()` in `volumes.py`.

**Resolution.** Defaults to 1 m, coarsening automatically for large polygons to
stay under 16 million cells. At 1 m a 1 km² site is a million cells, which
takes roughly 15–30 seconds, mostly waiting on Kartverket.

---

## What this does not tell you

The calculation is geometric. It gives the volume between today's terrain and a
horizontal plane, and nothing more.

- **The soil/rock split is your estimate, not measured data.** It moves the cost
  by an order of magnitude, and only a ground investigation settles it.
- **No side slopes.** Real excavations batter outward, which adds volume around
  the whole perimeter.
- **No drainage falls, no building pits, no topsoil stripping, no existing
  structures, no groundwater.**
- **DTM accuracy** is typically ±0.1 m vertically in laser-scanned areas, but
  older or lower-density areas are worse. Coverage gaps are reported in the
  result panel.
- **A flat plane is rarely the real design.** Use this for early feasibility and
  order-of-magnitude pricing, not for tender documents.

---

## Tests

```bash
pytest -q test_invites.py test_auth.py   # access control, no network
python test_engine.py     # volumes: analytic + live Kartverket data
python test_render.py     # georeferencing, hillshade orientation, colour ramp
python test_gate.py       # sign-in gate + admin page, needs a server on :8011
python test_ui.py         # browser test, needs a running server + playwright
```

`test_invites.py` and `test_auth.py` are pytest suites covering invitations and
the authorization wall, run against a local Postgres; the rest are
script-style, run as programs.

`test_engine.py` checks the engine against a tilted plane and a cone whose
volumes are known from geometry, then cross-checks live Kartverket data against
an independent brute-force recomputation. `test_render.py` verifies that the map
overlay is not flipped north/south and that the hillshade is lit from the
north-west. `test_gate.py` drives the four sign-in states and the admin page in
Chromium with the API stubbed. `test_ui.py` drives the real interface.

---

## Files

```
app.py             FastAPI app, endpoints, CSV export
kartverket.py      DTM fetching, tiling, resolution selection
demcache.py        TTL cache in front of the DTM fetch
volumes.py         cut/fill engine, level solving, sensitivity sweep
render.py          colour ramp, hillshade, PNG overlays, GeoTIFF export
storage.py         job output on disk; the seam for blob storage
config.py          environment-driven settings and startup guards
db.py              connection pool, all SQL, atomic invite redemption
auth.py            JWT verification, require_user/member/superadmin
invites.py         invitation + user administration endpoints
ratelimit.py       in-process sliding window
migrations/        schema, applied at startup
static/            frontend (index.html, admin.html, main.js, auth.js, vendor/)
Dockerfile         API image
.github/workflows/ pages.yml publishes static/, api.yml tests and deploys
```

### Exports

- **CSV** — the full result table, semicolon-separated with Norwegian decimal
  commas, opens straight in Excel.
- **GeoTIFF** — cut/fill depth per cell in metres, EPSG:25833, positive is cut.
  Loads into QGIS, AutoCAD Civil 3D or Gemini for further work.

---

Elevation data: © Kartverket, [DTM 1 m](https://hoydedata.no), CC BY 4.0.
