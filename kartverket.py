"""
Kartverket DTM access.

Fetches bare-earth elevation from Kartverket's public ArcGIS ImageServer
(hoydedata.no) as float32 rasters in EPSG:25833 (UTM zone 33N, ETRS89).

The service caps a single request at 15000x15000 px, but large requests are
slow and fragile, so anything bigger than MAX_TILE_PX in either dimension is
split into tiles and mosaicked locally.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import rasterio
import requests
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds

log = logging.getLogger(__name__)

DTM_EXPORT = "https://hoydedata.no/arcgis/rest/services/DTM/ImageServer/exportImage"
DTM_IDENTIFY = "https://hoydedata.no/arcgis/rest/services/DTM/ImageServer/identify"

UTM33 = "EPSG:25833"
NODATA = -9999.0

# Native resolution of the national terrain model. The service advertises
# 0.25 m because some project areas were flown denser, but 1 m is the
# realistic floor for most of the country.
NATIVE_RES = 1.0

MAX_TILE_PX = 2000          # per-request tile size
MAX_TOTAL_PX = 16_000_000   # guard against absurd areas
REQUEST_TIMEOUT = 180


class KartverketError(RuntimeError):
    """Raised when the elevation service cannot be reached or returns junk."""


@dataclass
class Dem:
    """A bare-earth elevation raster in EPSG:25833."""

    z: np.ndarray            # float32, NODATA where the model has no data
    transform: rasterio.Affine
    res: float

    @property
    def shape(self) -> tuple[int, int]:
        return self.z.shape

    @property
    def valid(self) -> np.ndarray:
        return np.isfinite(self.z) & (self.z > NODATA + 1.0)


def choose_resolution(width_m: float, height_m: float, requested: float | None) -> float:
    """Pick a grid resolution that keeps the raster under the pixel budget."""
    if requested and requested > 0:
        res = max(float(requested), 0.25)
    else:
        res = NATIVE_RES

    while (width_m / res) * (height_m / res) > MAX_TOTAL_PX:
        res *= 2.0
    return res


def snap_bounds(xmin: float, ymin: float, xmax: float, ymax: float, res: float):
    """Expand bounds outward to whole multiples of the grid resolution."""
    xmin = math.floor(xmin / res) * res
    ymin = math.floor(ymin / res) * res
    xmax = math.ceil(xmax / res) * res
    ymax = math.ceil(ymax / res) * res
    return xmin, ymin, xmax, ymax


def _fetch_tile(xmin, ymin, xmax, ymax, width, height, session) -> np.ndarray:
    params = {
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "bboxSR": 25833,
        "imageSR": 25833,
        "size": f"{width},{height}",
        "format": "tiff",
        "pixelType": "F32",
        "noData": NODATA,
        "noDataInterpretation": "esriNoDataMatchAny",
        "interpolation": "RSP_BilinearInterpolation",
        "f": "image",
    }
    try:
        r = session.get(DTM_EXPORT, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise KartverketError(f"Fikk ikke kontakt med hoydedata.no: {exc}") from exc

    if r.status_code != 200:
        raise KartverketError(f"hoydedata.no svarte HTTP {r.status_code}")

    ctype = r.headers.get("content-type", "")
    if "tiff" not in ctype:
        # The service reports errors as JSON with a 200 status.
        snippet = r.text[:300] if "json" in ctype or "html" in ctype else ctype
        raise KartverketError(f"Uventet svar fra hoydedata.no: {snippet}")

    with MemoryFile(r.content) as mem, mem.open() as src:
        arr = src.read(1).astype("float32")
        if src.nodata is not None:
            arr[arr == np.float32(src.nodata)] = NODATA

    # ArcGIS sometimes encodes no-data as a large negative sentinel rather than
    # the value we asked for.
    arr[~np.isfinite(arr)] = NODATA
    arr[arr < -1000.0] = NODATA
    return arr


def fetch_dem(xmin: float, ymin: float, xmax: float, ymax: float, res: float) -> Dem:
    """Fetch a DTM covering the given EPSG:25833 bounds at the given resolution."""
    xmin, ymin, xmax, ymax = snap_bounds(xmin, ymin, xmax, ymax, res)
    width = int(round((xmax - xmin) / res))
    height = int(round((ymax - ymin) / res))

    if width < 2 or height < 2:
        raise KartverketError("Området er for lite til å beregnes.")
    if width * height > MAX_TOTAL_PX:
        raise KartverketError("Området er for stort. Velg grovere oppløsning eller mindre polygon.")

    z = np.full((height, width), NODATA, dtype="float32")
    transform = from_bounds(xmin, ymin, xmax, ymax, width, height)

    n_cols = math.ceil(width / MAX_TILE_PX)
    n_rows = math.ceil(height / MAX_TILE_PX)
    log.info("Henter DTM: %dx%d px @ %.2f m (%d tiles)", width, height, res, n_cols * n_rows)

    with requests.Session() as session:
        for row in range(n_rows):
            r0 = row * MAX_TILE_PX
            r1 = min(r0 + MAX_TILE_PX, height)
            for col in range(n_cols):
                c0 = col * MAX_TILE_PX
                c1 = min(c0 + MAX_TILE_PX, width)

                # Raster rows run north to south; bbox y runs south to north.
                tile_xmin = xmin + c0 * res
                tile_xmax = xmin + c1 * res
                tile_ymax = ymax - r0 * res
                tile_ymin = ymax - r1 * res

                z[r0:r1, c0:c1] = _fetch_tile(
                    tile_xmin, tile_ymin, tile_xmax, tile_ymax,
                    c1 - c0, r1 - r0, session,
                )

    return Dem(z=z, transform=transform, res=res)


def point_elevation(x: float, y: float) -> float | None:
    """Elevation at a single EPSG:25833 point, or None where there is no data."""
    geom = f'{{"x":{x},"y":{y},"spatialReference":{{"wkid":25833}}}}'
    try:
        r = requests.get(
            DTM_IDENTIFY,
            params={
                "geometry": geom,
                "geometryType": "esriGeometryPoint",
                "returnGeometry": "false",
                "f": "json",
            },
            timeout=30,
        )
        value = r.json().get("value")
    except (requests.RequestException, ValueError) as exc:
        raise KartverketError(f"Fikk ikke kontakt med hoydedata.no: {exc}") from exc

    try:
        return float(value)
    except (TypeError, ValueError):
        return None
