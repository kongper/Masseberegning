"""
Masseberegning - cut/fill volume calculator for Norwegian terrain.

Draw a polygon, pick a target surface level, set the soil depth, and the app
pulls Kartverket's 1 m bare-earth terrain model for that footprint and works
out how much material has to move.

This process serves the API only. The frontend lives on GitHub Pages; set
SERVE_STATIC=1 to have it served from here as well, which is how local
development works.

Run locally:  python app.py       then open http://127.0.0.1:8000
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import requests
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pyproj import Transformer
from rasterio.features import rasterize
from shapely.geometry import Polygon
from shapely.ops import transform as shapely_transform

import auth
import db
import demcache
import invites
import kartverket
import render
import storage
import volumes
from config import settings
from kartverket import KartverketError
from ratelimit import SlidingWindow

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
log = logging.getLogger("masseberegning")

BASE_DIR = Path(__file__).parent

to_utm = Transformer.from_crs("EPSG:4326", kartverket.UTM33, always_xy=True)
to_wgs = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

_calc_minute = SlidingWindow(settings.calc_per_minute, 60)
_calc_day = SlidingWindow(settings.calc_per_day, 86400)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Raises if the local auth bypass is enabled anywhere it could matter.
    settings.guard_local_mode()
    if settings.local_single_user:
        log.warning("=" * 68)
        log.warning("LOCAL_SINGLE_USER=1 - access control is OFF and there is no")
        log.warning("database. Every request is treated as a local superadmin.")
        log.warning("Loopback only. Never use this for a deployment.")
        log.warning("=" * 68)

    problems = settings.require_for_production()
    if problems and os.environ.get("ALLOW_INCOMPLETE_CONFIG") != "1":
        for p in problems:
            log.error("Configuration: %s", p)
        raise RuntimeError(
            "Refusing to start with an incomplete configuration: "
            + "; ".join(problems)
            + ". Set ALLOW_INCOMPLETE_CONFIG=1 to override (development only)."
        )
    for p in problems:
        log.warning("Configuration: %s (running anyway)", p)

    storage.init(settings.job_root, settings.job_ttl_minutes)

    if settings.database_url:
        # Non-fatal on purpose. If this raised, the process would exit, the
        # platform would have no healthy machine, and every request would get
        # its bodiless 503 - which a browser reports as a CORS error, because
        # an edge error page carries no Access-Control-Allow-Origin. The app
        # would be both broken and unable to say why. Starting anyway means
        # /healthz and /readyz can report the real error, and db.ensure_pool
        # retries, so a database that comes back needs no redeploy.
        strict = os.environ.get("DB_REQUIRED_AT_STARTUP") == "1"
        if db.init_pool(settings.database_url, required=strict) is not None:
            db.run_migrations()
        else:
            log.error("=" * 68)
            log.error("Starting WITHOUT a database. /api/* will answer 503.")
            log.error("Check DATABASE_URL, then curl /readyz for the error.")
            log.error("=" * 68)
    yield
    db.close_pool()


app = FastAPI(title="Masseberegning", docs_url=None, redoc_url=None, lifespan=lifespan)

if settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=3600,
    )

app.include_router(invites.router)


class Request_(BaseModel):
    polygon: list[list[float]] = Field(..., min_length=3, max_length=2000,
                                       description="[[lon, lat], ...]")
    mode: str = Field("balansert", pattern="^(balansert|laveste|fast)$")
    fixed_level: float | None = None
    soil_depth: float = Field(2.0, ge=0.0, le=100.0)
    swell_soil: float = Field(0.25, ge=0.0, le=2.0)
    swell_rock: float = Field(0.50, ge=0.0, le=2.0)
    shrinkage: float = Field(0.10, ge=0.0, le=1.0)
    truck_capacity: float = Field(15.0, gt=0.0)
    resolution: float | None = Field(None, description="Grid resolution in m; None = auto")


def _latlng_bounds(bounds_3857):
    west, south, east, north = bounds_3857
    w, s = to_wgs.transform(west, south)
    e, n = to_wgs.transform(east, north)
    return [[s, w], [n, e]]


def _rate_limit(user_id: str) -> None:
    for window, unit in ((_calc_minute, "minutt"), (_calc_day, "døgn")):
        ok, retry = window.check(user_id)
        if not ok:
            raise HTTPException(
                429,
                f"Du har nådd grensen på {window.limit} beregninger per {unit}. "
                "Prøv igjen senere.",
                headers={"Retry-After": str(int(retry) + 1)},
            )


@app.post("/api/beregn")
def beregn(req: Request_, user: db.User = Depends(auth.require_member)):
    _rate_limit(user.id)

    ring = [(float(lon), float(lat)) for lon, lat in req.polygon]
    poly_wgs = Polygon(ring)
    if not poly_wgs.is_valid:
        poly_wgs = poly_wgs.buffer(0)
    if poly_wgs.is_empty or poly_wgs.area <= 0:
        raise HTTPException(400, "Ugyldig polygon.")

    poly = shapely_transform(lambda x, y: to_utm.transform(x, y), poly_wgs)
    xmin, ymin, xmax, ymax = poly.bounds

    # Nothing else stops someone drawing a box around a whole municipality.
    # choose_resolution would coarsen the grid to fit the pixel budget, but the
    # DEM fetch behind it would still be enormous.
    if poly.area > settings.max_area_m2:
        raise HTTPException(
            422,
            f"Flaten er {poly.area / 1e6:.1f} km². Grensen er "
            f"{settings.max_area_m2 / 1e6:.0f} km² — tegn et mindre område.",
        )

    res = kartverket.choose_resolution(xmax - xmin, ymax - ymin, req.resolution)

    try:
        dem = demcache.fetch_dem(xmin, ymin, xmax, ymax, res)
    except KartverketError as exc:
        raise HTTPException(502, str(exc)) from exc

    mask = rasterize(
        [(poly, 1)], out_shape=dem.shape, transform=dem.transform,
        fill=0, all_touched=False, dtype="uint8",
    ).astype(bool)

    valid = mask & dem.valid
    n_valid = int(valid.sum())
    n_inside = int(mask.sum())

    if n_valid < 10:
        raise HTTPException(
            422,
            "Ingen høydedata for dette området. Kartverkets terrengmodell dekker "
            "kun Norge - kontroller at polygonet ligger på land i Norge.",
        )

    coverage = n_valid / n_inside if n_inside else 0.0
    cell_area = dem.res * dem.res
    z_inside = dem.z[valid].astype("float64")

    terrain = volumes.Terrain.from_values(z_inside, cell_area)
    params = volumes.Params(
        soil_depth=req.soil_depth,
        swell_soil=req.swell_soil,
        swell_rock=req.swell_rock,
        shrinkage=req.shrinkage,
        truck_capacity=req.truck_capacity,
    )

    try:
        level = volumes.resolve_level(terrain, req.mode, req.fixed_level, params.shrinkage)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    result = volumes.compute(z_inside, cell_area, level, params)
    result["mode"] = req.mode
    result["resolution_m"] = dem.res
    result["coverage"] = coverage
    result["polygon_area_m2"] = float(poly.area)
    result["sensitivity"] = volumes.sensitivity(terrain, params)
    result["balanced_level"] = volumes.solve_balanced_level(terrain, params.shrinkage)
    result["lowest_level"] = float(terrain.z_sorted[0])
    result["highest_level"] = float(terrain.z_sorted[-1])

    hist, edges = np.histogram(z_inside, bins=40)
    result["histogram"] = {"counts": hist.tolist(), "edges": edges.tolist()}

    job_id, job_dir = storage.new_job()
    d = np.where(valid, dem.z.astype("float64") - level, 0.0)

    overlay = render.cutfill_png(d, valid, dem.transform, job_dir / "cutfill.png")
    shade = render.hillshade_png(dem.z, valid, dem.res, dem.transform, job_dir / "hillshade.png")
    render.write_geotiff(d, valid, dem.transform, job_dir / "skjaering_fylling.tif")
    _write_csv(job_dir / "resultat.csv", result)

    result["job_id"] = job_id
    result["overlay"] = {
        "cutfill_url": f"/api/jobb/{job_id}/cutfill.png",
        "hillshade_url": f"/api/jobb/{job_id}/hillshade.png",
        "bounds": _latlng_bounds(overlay["bounds_3857"]),
        "hillshade_bounds": _latlng_bounds(shade["bounds_3857"]),
        "vmax": overlay["vmax"],
    }
    result["downloads"] = {
        "geotiff": f"/api/jobb/{job_id}/skjaering_fylling.tif",
        "csv": f"/api/jobb/{job_id}/resultat.csv",
    }
    log.info("Beregning for %s: %.1f daa, %s", user.email, poly.area / 1000, req.mode)
    return result


def _write_csv(path: Path, r: dict) -> None:
    rows = [
        ("Kotehøyde ferdig planum", r["level"], "moh"),
        ("Areal", r["area_m2"], "m2"),
        ("Oppløsning", r.get("resolution_m"), "m"),
        ("Terreng lavest", r["terrain"]["min"], "moh"),
        ("Terreng høyest", r["terrain"]["max"], "moh"),
        ("Terreng middel", r["terrain"]["mean"], "moh"),
        ("", "", ""),
        ("Skjæring totalt (fast)", r["cut"]["total_bank_m3"], "m3"),
        ("  herav løsmasse (fast)", r["cut"]["soil_bank_m3"], "m3"),
        ("  herav fjell (fast)", r["cut"]["rock_bank_m3"], "m3"),
        ("Skjæring maks dybde", r["cut"]["max_depth_m"], "m"),
        ("Skjæringsareal", r["cut"]["area_m2"], "m2"),
        ("", "", ""),
        ("Fylling geometrisk volum", r["fill"]["void_m3"], "m3"),
        ("Fylling massebehov (fast)", r["fill"]["bank_needed_m3"], "m3"),
        ("Fylling maks høyde", r["fill"]["max_depth_m"], "m"),
        ("Fyllingsareal", r["fill"]["area_m2"], "m2"),
        ("", "", ""),
        ("Netto masseoverskudd (fast)", r["balance"]["net_bank_m3"], "m3"),
        ("Overskudd løsmasse (fast)", r["balance"]["surplus_soil_bank_m3"], "m3"),
        ("Overskudd fjell (fast)", r["balance"]["surplus_rock_bank_m3"], "m3"),
        ("Transport ut (løst)", r["balance"]["export_loose_m3"], "m3"),
        ("Transport inn (løst)", r["balance"]["import_loose_m3"], "m3"),
        ("Antall lastebillass", r["balance"]["truck_loads"], "lass"),
        ("", "", ""),
        ("Løsmassedybde", r["params"]["soil_depth"], "m"),
        ("Utlastingsfaktor løsmasse", r["params"]["swell_soil"], "andel"),
        ("Utlastingsfaktor fjell", r["params"]["swell_rock"], "andel"),
        ("Komprimeringssvinn", r["params"]["shrinkage"], "andel"),
    ]
    lines = ["Post;Verdi;Enhet"]
    for name, value, unit in rows:
        if isinstance(value, (int, float)):
            value = f"{value:.2f}".replace(".", ",")
        lines.append(f"{name};{value};{unit}")
    path.write_text("\n".join(lines), encoding="utf-8-sig")


@app.get("/api/hoyde")
def hoyde(lon: float = Query(..., ge=-180, le=180),
          lat: float = Query(..., ge=-90, le=90),
          user: db.User = Depends(auth.require_member)):
    x, y = to_utm.transform(lon, lat)
    try:
        z = kartverket.point_elevation(x, y)
    except KartverketError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"lon": lon, "lat": lat, "z": z}


@app.get("/api/sok")
def sok(q: str = Query(..., min_length=2, max_length=100),
        user: db.User = Depends(auth.require_member)):
    """Place-name lookup, proxied through the backend to sidestep CORS."""
    try:
        r = requests.get(
            "https://ws.geonorge.no/stedsnavn/v1/navn",
            params={"sok": q if q.endswith("*") else q + "*",
                    "treffPerSide": 8, "utkoordsys": 4258},
            timeout=15,
        )
        data = r.json()
    except (requests.RequestException, ValueError):
        return {"treff": []}

    hits = []
    for item in data.get("navn", []):
        pt = item.get("representasjonspunkt") or {}
        if "nord" not in pt or "øst" not in pt:
            continue
        kommuner = item.get("kommuner") or [{}]
        fylker = item.get("fylker") or [{}]
        hits.append({
            "navn": item.get("skrivemåte"),
            "type": item.get("navneobjekttype") or "Sted",
            "kommune": kommuner[0].get("kommunenavn"),
            "fylke": fylker[0].get("fylkesnavn"),
            "lat": pt["nord"],
            "lon": pt["øst"],
        })
    return {"treff": hits}


@app.get("/api/jobb/{job_id}/{name}")
def job_file(job_id: str, name: str):
    """Job output. Unauthenticated by design — the URL is the credential.

    See the note at the top of storage.py: an <img src> and an <a download>
    cannot send an Authorization header, so token auth is not available here.
    """
    path = storage.job_file(job_id, name)
    if path is None:
        raise HTTPException(404, "Ikke funnet")
    return FileResponse(path)


@app.get("/healthz")
def healthz():
    """Liveness only. Touches nothing outside this process.

    This must stay trivial. The platform health check has a five-second
    timeout, and it used to run a database round-trip: on a cold start - the
    machine woken from auto-stop, the pool still dialling the database - that
    blew the timeout, the machine was marked unhealthy, and routing stopped.
    A liveness probe that can be dragged down by a dependency will eventually
    take the app down over something the app could have survived.
    """
    detail: dict = {
        "jobs": str(storage.root()),
        "dem_cache": demcache.stats(),
        "db": db.status(),          # cached state; no connection attempt
    }
    if settings.local_single_user:
        detail["mode"] = "local_single_user"
    return {"status": "ok", **detail}


@app.get("/readyz")
def readyz():
    """Readiness: can this process actually serve requests?

    Unlike /healthz this really does talk to the database, so it is the one to
    curl when something is wrong. Deliberately NOT the platform health check -
    a slow database here should show up as a clear error, not as a dead app.
    """
    if settings.local_single_user or not settings.database_url:
        return {"status": "ready", "db": db.status()}
    try:
        with db.pool().connection() as conn:
            conn.execute("select 1")
        return {"status": "ready", "db": {"configured": True, "connected": True}}
    except Exception as exc:  # noqa: BLE001 - a probe must not raise
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready",
                     "db": {"configured": True, "connected": False,
                            "error": f"{type(exc).__name__}: {exc}"}},
        )


# Serving the UI from the API is for local development. In production the
# frontend is on GitHub Pages and this stays off.
if settings.serve_static:
    @app.get("/")
    def index():
        return RedirectResponse("/static/index.html")

    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


if __name__ == "__main__":
    shown = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
    print(f"\n  Masseberegning kjører på http://{shown}:{settings.port}\n")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
