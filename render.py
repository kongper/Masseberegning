"""
Raster rendering for the map overlays.

Everything is computed in EPSG:25833 (where a metre is a metre) and only
reprojected to Web Mercator at the very end, because that is the projection
Leaflet's imageOverlay draws in.
"""

from __future__ import annotations

import numpy as np
import rasterio
from PIL import Image
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rasterio.transform import array_bounds

UTM33 = "EPSG:25833"
WEBMERC = "EPSG:3857"

# Diverging ramp: fill (negative) through cool blues, cut (positive) through
# warm yellows to deep red. Stops are (position in [-1, 1], R, G, B).
RAMP = [
    (-1.00, 8, 48, 107),
    (-0.60, 33, 113, 181),
    (-0.25, 107, 174, 214),
    (-0.06, 198, 219, 239),
    (0.00, 247, 247, 247),
    (0.06, 254, 224, 144),
    (0.25, 253, 174, 97),
    (0.60, 215, 48, 39),
    (1.00, 103, 0, 13),
]


def _colorize(norm: np.ndarray) -> np.ndarray:
    """Map values in [-1, 1] onto the diverging ramp. Returns uint8 RGB."""
    pos = np.array([s[0] for s in RAMP])
    cols = np.array([s[1:] for s in RAMP], dtype="float64")

    flat = np.clip(norm.ravel(), -1.0, 1.0)
    rgb = np.empty((flat.size, 3), dtype="float64")
    for ch in range(3):
        rgb[:, ch] = np.interp(flat, pos, cols[:, ch])
    return rgb.reshape(norm.shape + (3,)).astype("uint8")


def hillshade(z: np.ndarray, res: float, azimuth: float = 315.0, altitude: float = 45.0,
              z_factor: float = 1.0) -> np.ndarray:
    """
    Shaded relief, following the standard Esri formulation.

    np.gradient's first axis is rows, which run north to south - the same
    direction as Esri's dz/dy, so the gradients drop straight into
    aspect = atan2(dz/dy, -dz/dx) with no sign flip.
    """
    zz = np.where(np.isfinite(z), z, np.nan)
    dz_dy, dz_dx = np.gradient(zz * z_factor, res, res)
    slope = np.arctan(np.hypot(dz_dx, dz_dy))
    aspect = np.arctan2(dz_dy, -dz_dx)

    az = np.radians(360.0 - azimuth + 90.0)
    alt = np.radians(altitude)
    shade = (np.sin(alt) * np.cos(slope) +
             np.cos(alt) * np.sin(slope) * np.cos(az - aspect))
    return np.clip(np.nan_to_num(shade, nan=0.5), 0.0, 1.0)


def _to_webmercator(band: np.ndarray, transform, nodata: float):
    """Reproject a single float band from UTM33 to Web Mercator."""
    height, width = band.shape
    bounds = array_bounds(height, width, transform)

    dst_transform, dst_w, dst_h = calculate_default_transform(
        UTM33, WEBMERC, width, height, *bounds
    )
    dst = np.full((dst_h, dst_w), nodata, dtype="float32")
    reproject(
        source=band.astype("float32"),
        destination=dst,
        src_transform=transform,
        src_crs=UTM33,
        dst_transform=dst_transform,
        dst_crs=WEBMERC,
        src_nodata=nodata,
        dst_nodata=nodata,
        resampling=Resampling.bilinear,
    )
    return dst, dst_transform


def cutfill_png(d: np.ndarray, mask: np.ndarray, transform, path,
                vmax: float | None = None) -> dict:
    """
    Render the cut/fill depth raster as a translucent RGBA PNG in Web Mercator.

    `d` is elevation minus target level; `mask` marks cells inside the polygon
    with valid elevation data. Returns the lat/lng bounds Leaflet needs.
    """
    SENTINEL = -1e9
    work = np.where(mask, d, SENTINEL)
    warped, dst_transform = _to_webmercator(work, transform, SENTINEL)

    inside = warped > SENTINEL / 2
    values = warped[inside]

    if vmax is None:
        vmax = float(np.percentile(np.abs(values), 98)) if values.size else 1.0
    vmax = max(vmax, 0.25)

    norm = np.zeros_like(warped, dtype="float64")
    np.divide(warped, vmax, out=norm, where=inside)
    rgb = _colorize(np.where(inside, norm, 0.0))

    alpha = np.where(inside, 205, 0).astype("uint8")
    # Fade out the near-zero band so untouched ground stays readable.
    faint = inside & (np.abs(norm) < 0.03)
    alpha[faint] = 90

    rgba = np.dstack([rgb, alpha])
    Image.fromarray(rgba, mode="RGBA").save(path, optimize=True)

    h, w = warped.shape
    west, south, east, north = array_bounds(h, w, dst_transform)
    return {"bounds_3857": [west, south, east, north], "vmax": vmax,
            "size": [int(w), int(h)]}


def hillshade_png(z: np.ndarray, valid: np.ndarray, res: float, transform, path) -> dict:
    """Render a shaded-relief PNG in Web Mercator for map context."""
    SENTINEL = -1e9
    shade = hillshade(np.where(valid, z, np.nan), res)
    work = np.where(valid, shade, SENTINEL)
    warped, dst_transform = _to_webmercator(work, transform, SENTINEL)

    inside = warped > SENTINEL / 2
    grey = np.clip(np.where(inside, warped, 0.5) * 255.0, 0, 255).astype("uint8")
    rgba = np.dstack([grey, grey, grey, np.where(inside, 255, 0).astype("uint8")])
    Image.fromarray(rgba, mode="RGBA").save(path, optimize=True)

    h, w = warped.shape
    west, south, east, north = array_bounds(h, w, dst_transform)
    return {"bounds_3857": [west, south, east, north], "size": [int(w), int(h)]}


def write_geotiff(d: np.ndarray, mask: np.ndarray, transform, path, nodata: float = -9999.0):
    """Cut/fill depth raster as a GeoTIFF, for import into CAD or GIS."""
    out = np.where(mask, d, nodata).astype("float32")
    with rasterio.open(
        path, "w", driver="GTiff", height=out.shape[0], width=out.shape[1],
        count=1, dtype="float32", crs=UTM33, transform=transform,
        nodata=nodata, compress="deflate", tiled=True,
    ) as dst:
        dst.write(out, 1)
        dst.set_band_description(1, "Skjaering (+) / fylling (-) i meter")
