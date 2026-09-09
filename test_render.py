"""
Verification of the rendering layer.

The two things that go wrong silently in this kind of app are overlay
georeferencing (a north/south flip puts the map picture in the wrong place but
still looks plausible) and hillshade orientation (inverted relief reads as a
valley where there is a ridge). Both are checked here against ground truth.
"""

import sys

import numpy as np
from PIL import Image

import render

FAILURES = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


print("\n=== Hillshade orientation ===")
# Row 0 is north. Terrain rising toward the south-east faces north-west, so
# with the default light from the north-west it must be brightly lit; the
# mirror image must be in shadow.
rows, cols = np.mgrid[0:60, 0:60].astype("float64")
lit = render.hillshade(cols + rows, 1.0).mean()
dark = render.hillshade(-(cols + rows), 1.0).mean()
print(f"  NW-facing {lit:.3f}   SE-facing {dark:.3f}")
check("NW-facing slope is lit", lit > 0.75)
check("SE-facing slope is shaded", dark < 0.35)
check("lit and shaded are distinguishable", lit - dark > 0.5, f"delta {lit - dark:.3f}")

flat = render.hillshade(np.zeros((20, 20)), 1.0).mean()
check("flat ground equals sin(altitude)", abs(flat - np.sin(np.radians(45))) < 1e-3,
      f"{flat:.4f}")

# A slope facing straight east, lit from the east, must be fully lit.
east = render.hillshade(-cols, 1.0, azimuth=90.0, altitude=45.0)
check("east-facing slope lit from east is bright", east[5:-5, 5:-5].mean() > 0.95,
      f"{east[5:-5, 5:-5].mean():.3f}")
check("east-facing slope lit from west is dark",
      render.hillshade(-cols, 1.0, azimuth=270.0)[5:-5, 5:-5].mean() < 0.05)

print("\n=== Colour ramp ===")
ramp = render._colorize(np.array([[-1.0, -0.5, 0.0, 0.5, 1.0]]))[0]
check("fill end is blue", ramp[0][2] > ramp[0][0] + 60, str(tuple(ramp[0])))
check("cut end is red", ramp[4][0] > ramp[4][2] + 60, str(tuple(ramp[4])))
check("midpoint is neutral", abs(int(ramp[2][0]) - int(ramp[2][2])) < 12, str(tuple(ramp[2])))

# The two properties that make a diverging scale readable: sign is unambiguous
# either side of zero, and magnitude reads as darkness. Note that red-minus-blue
# is NOT monotone across the whole ramp - both ends are deliberately dark, so it
# peaks mid-limb. Lightness is the right thing to test.
fine = render._colorize(np.linspace(-1, 1, 41).reshape(1, -1))[0].astype(int)
neg, pos = fine[:19], fine[22:]
check("everything below zero is blue-dominant", all(c[2] > c[0] for c in neg))
check("everything above zero is red-dominant", all(c[0] > c[2] for c in pos))

light = fine.mean(axis=1)
mid = len(light) // 2
check("lightness rises toward zero", all(a < b for a, b in zip(light[:mid], light[1:mid + 1])))
check("lightness falls away from zero", all(a > b for a, b in zip(light[mid:], light[mid + 1:])))
check("zero is the lightest point", int(np.argmax(light)) == mid)

print("\n=== Overlay georeferencing (synthetic) ===")
# Build a raster whose north half is deep cut and south half is deep fill,
# render it, and confirm the PNG keeps north at the top after reprojection.
import rasterio
from rasterio.transform import from_bounds

H = W = 200
d = np.zeros((H, W), dtype="float64")
d[: H // 2, :] = 10.0     # north half: cut
d[H // 2:, :] = -10.0     # south half: fill
mask = np.ones((H, W), dtype=bool)
transform = from_bounds(255000, 6783000, 256000, 6784000, W, H)

info = render.cutfill_png(d, mask, transform, "/tmp/_geo_test.png")
png = np.array(Image.open("/tmp/_geo_test.png").convert("RGBA"))
h, w = png.shape[:2]

top = png[int(h * 0.15), w // 2]
bottom = png[int(h * 0.85), w // 2]
top_warm = int(top[0]) - int(top[2])
bot_warm = int(bottom[0]) - int(bottom[2])
print(f"  top rgba {tuple(top)}   bottom rgba {tuple(bottom)}")
check("north half renders as cut (red)", top_warm > 40)
check("south half renders as fill (blue)", bot_warm < -40)
check("bounds are ordered W<E, S<N",
      info["bounds_3857"][0] < info["bounds_3857"][2]
      and info["bounds_3857"][1] < info["bounds_3857"][3])
check("vmax reflects the data", abs(info["vmax"] - 10.0) < 0.5, f"{info['vmax']:.2f}")

print("\n=== Edge artefacts ===")
# Reprojection must not blend real values with the no-data sentinel: if it did,
# a uniform cut would grow a false blue fill fringe along the polygon boundary.
# This works only because cutfill_png passes src_nodata to the warper.
rows_t, cols_t = np.mgrid[0:300, 0:300]
diag = (rows_t + cols_t) < 300
tf = from_bounds(255000, 6783000, 256000, 6784000, 300, 300)

render.cutfill_png(np.full((300, 300), 5.0), diag, tf, "/tmp/_edge_cut.png")
p = np.array(Image.open("/tmp/_edge_cut.png").convert("RGBA")).astype(int)
vis = p[..., 3] > 0
check("uniform cut renders no fill-coloured pixels",
      (vis & (p[..., 2] > p[..., 0] + 40)).sum() == 0)

render.cutfill_png(np.full((300, 300), -5.0), diag, tf, "/tmp/_edge_fill.png")
p = np.array(Image.open("/tmp/_edge_fill.png").convert("RGBA")).astype(int)
vis = p[..., 3] > 0
check("uniform fill renders no cut-coloured pixels",
      (vis & (p[..., 0] > p[..., 2] + 40)).sum() == 0)

print("\n=== Masking ===")
# Cells outside the polygon mask must be fully transparent.
mask2 = np.zeros((H, W), dtype=bool)
mask2[50:150, 50:150] = True
render.cutfill_png(d, mask2, transform, "/tmp/_mask_test.png")
p2 = np.array(Image.open("/tmp/_mask_test.png").convert("RGBA"))
alpha = p2[..., 3]
covered = (alpha > 0).mean()
check("masked-out area is transparent", alpha[2, 2] == 0)
check("coverage is about a quarter of the frame", 0.20 < covered < 0.30,
      f"{covered * 100:.1f}%")

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("All rendering checks passed.")
