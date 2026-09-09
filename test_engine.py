"""
Verification of the volume engine.

Three levels of check:
  1. analytic  - a synthetic tilted plane and a cone, where the true volume
                 is known from geometry
  2. internal  - the balanced level really does balance, the lowest level
                 really does produce zero fill, soil + rock == total
  3. live      - a real polygon against Kartverket, cross-checked with an
                 independent brute-force recomputation
"""

import sys

import numpy as np
from pyproj import Transformer
from rasterio.features import rasterize
from shapely.geometry import Polygon
from shapely.ops import transform as shapely_transform

import kartverket
import volumes

FAILURES = []


def check(name, got, want, tol, unit=""):
    ok = abs(got - want) <= tol
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name}: got {got:,.4f}{unit}  expected {want:,.4f}{unit}  (tol {tol:g})")
    if not ok:
        FAILURES.append(name)


print("\n=== 1. Analytic: tilted plane ===")
# 100 x 100 m at 1 m cells, surface rising linearly 0 -> 10 m across x.
# Cutting to the mid-level (5 m) removes a wedge: triangular cross-section
# 50 m wide by 5 m tall = 125 m2, extruded 100 m = 12 500 m3. The fill wedge
# is its mirror image.
WEDGE = 0.5 * 50.0 * 5.0 * 100.0
n = 100
xs = (np.arange(n) + 0.5) / n * 10.0
z_plane = np.tile(xs, (n, 1))
t = volumes.Terrain.from_values(z_plane.ravel(), 1.0)

cut, fill = t.cut_fill(5.0)
check("plane cut at mid-level", cut, WEDGE, 60.0, " m3")
check("plane fill at mid-level", fill, WEDGE, 60.0, " m3")

lvl = volumes.solve_balanced_level(t, 0.0)
check("plane balanced level == mean", lvl, float(z_plane.mean()), 1e-3, " m")

cut0, fill0 = t.cut_fill(float(z_plane.min()))
check("plane fill at lowest level", fill0, 0.0, 1e-6, " m3")
check("plane cut at lowest level", cut0, float((z_plane - z_plane.min()).sum()), 1.0, " m3")

print("\n=== 2. Analytic: cone ===")
# Cone radius 50 m, height 20 m, on a 1 m grid, flattened to its base.
# True volume = pi*r^2*h/3 = 52 360 m3. Grid discretisation gives ~0.5 % error.
R, H = 50.0, 20.0
yy, xx = np.mgrid[0:100, 0:100] + 0.5
r = np.hypot(xx - 50.0, yy - 50.0)
z_cone = np.clip(H * (1.0 - r / R), 0.0, None)
inside = r <= R
tc = volumes.Terrain.from_values(z_cone[inside].ravel(), 1.0)
cut_c, fill_c = tc.cut_fill(0.0)
analytic = np.pi * R**2 * H / 3.0
check("cone volume", cut_c, analytic, analytic * 0.01, " m3")
check("cone fill", fill_c, 0.0, 1e-6, " m3")

print("\n=== 3. Internal consistency (synthetic, with bulking) ===")
p = volumes.Params(soil_depth=2.0, swell_soil=0.25, swell_rock=0.5, shrinkage=0.10)
lvl_b = volumes.solve_balanced_level(t, p.shrinkage)
res = volumes.compute(z_plane.ravel(), 1.0, lvl_b, p)

check("soil + rock == total cut",
      res["cut"]["soil_bank_m3"] + res["cut"]["rock_bank_m3"],
      res["cut"]["total_bank_m3"], 1e-6, " m3")
check("balanced -> net bank zero", res["balance"]["net_bank_m3"], 0.0, 1e-3, " m3")
# Compaction shrinkage means a fill eats more bank material than its own
# volume, so the balance point sits BELOW the mean: dig deeper to feed it.
check("balanced level below mean when shrinkage > 0",
      1.0 if lvl_b < z_plane.mean() else 0.0, 1.0, 0.0)
check("balanced level moves down as shrinkage grows",
      1.0 if volumes.solve_balanced_level(t, 0.30) < lvl_b < volumes.solve_balanced_level(t, 0.0)
      else 0.0, 1.0, 0.0)

# Soil cap: with a 2 m soil depth on a plane cut at 5 m, soil volume is
# bounded by 2 m * cut area.
cut_area = res["cut"]["area_m2"]
check("soil volume <= soil_depth * cut area",
      1.0 if res["cut"]["soil_bank_m3"] <= cut_area * 2.0 + 1e-6 else 0.0, 1.0, 0.0)

# Lowest-level mode: no fill at all, everything is export.
res_low = volumes.compute(z_plane.ravel(), 1.0, float(z_plane.min()), p)
check("lowest mode -> zero fill", res_low["fill"]["void_m3"], 0.0, 1e-9, " m3")
check("lowest mode -> no import", res_low["balance"]["import_loose_m3"], 0.0, 1e-9, " m3")

# Independent brute-force recomputation of the same numbers.
d = z_plane.ravel() - lvl_b
bf_cut = float(d[d > 0].sum())
bf_fill = float(-d[d < 0].sum())
bf_soil = float(np.minimum(np.clip(d, 0, None), 2.0).sum())
check("brute-force cut", res["cut"]["total_bank_m3"], bf_cut, 1e-6, " m3")
check("brute-force fill", res["fill"]["void_m3"], bf_fill, 1e-6, " m3")
check("brute-force soil", res["cut"]["soil_bank_m3"], bf_soil, 1e-6, " m3")

print("\n=== 4. Live Kartverket data ===")
# A 400 x 400 m square just south of Lillehammer.
to_utm = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
cx, cy = to_utm.transform(10.4662, 61.1153)
half = 200.0
square = Polygon([(cx - half, cy - half), (cx + half, cy - half),
                  (cx + half, cy + half), (cx - half, cy + half)])

try:
    dem = kartverket.fetch_dem(*square.bounds, 1.0)
except kartverket.KartverketError as exc:
    print(f"  [SKIP] could not reach Kartverket: {exc}")
    dem = None

if dem is not None:
    mask = rasterize([(square, 1)], out_shape=dem.shape, transform=dem.transform,
                     fill=0, all_touched=False, dtype="uint8").astype(bool)
    valid = mask & dem.valid
    z = dem.z[valid].astype("float64")
    area_grid = valid.sum() * dem.res ** 2

    print(f"  cells: {valid.sum():,}  min {z.min():.2f}  max {z.max():.2f}  mean {z.mean():.2f} moh")
    check("rasterised area vs shapely area", area_grid, square.area, square.area * 0.01, " m2")

    tr = volumes.Terrain.from_values(z, dem.res ** 2)

    lvl_bal = volumes.solve_balanced_level(tr, 0.0)
    check("real terrain: balanced level (no shrinkage) == mean",
          lvl_bal, float(z.mean()), 1e-3, " m")

    c, f = tr.cut_fill(lvl_bal)
    check("real terrain: cut == fill at balance", c - f, 0.0, max(c, 1.0) * 1e-6, " m3")

    r = volumes.compute(z, dem.res ** 2, lvl_bal, p)
    dd = z - lvl_bal
    check("real terrain: brute-force cut", r["cut"]["total_bank_m3"],
          float(dd[dd > 0].sum() * dem.res ** 2), 1.0, " m3")
    check("real terrain: brute-force fill", r["fill"]["void_m3"],
          float(-dd[dd < 0].sum() * dem.res ** 2), 1.0, " m3")
    check("real terrain: soil + rock == cut",
          r["cut"]["soil_bank_m3"] + r["cut"]["rock_bank_m3"],
          r["cut"]["total_bank_m3"], 1e-6, " m3")

    r_low = volumes.compute(z, dem.res ** 2, float(z.min()), p)
    check("real terrain: lowest -> zero fill", r_low["fill"]["void_m3"], 0.0, 1e-9, " m3")
    print(f"  lowest-level export: {r_low['balance']['export_loose_m3']:,.0f} m3 løst "
          f"({r_low['balance']['truck_loads']:,.0f} lass)")

    # Sensitivity curve must cross zero exactly at the balanced level.
    curve = volumes.sensitivity(tr, p)
    lvl_p = volumes.solve_balanced_level(tr, p.shrinkage)
    nets = [c_["net_m3"] for c_ in curve]
    sign_changes = sum(1 for a, b in zip(nets, nets[1:]) if a > 0 >= b)
    check("sensitivity curve crosses zero once", sign_changes, 1, 0)
    a, b = next((a, b) for a, b in zip(curve, curve[1:]) if a["net_m3"] > 0 >= b["net_m3"])
    frac = a["net_m3"] / (a["net_m3"] - b["net_m3"])
    crossing = a["level"] + frac * (b["level"] - a["level"])
    check("curve crossing matches solved level", crossing, lvl_p, 0.01, " m")

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("All checks passed.")
