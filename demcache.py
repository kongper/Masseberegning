"""
A small TTL cache in front of the Kartverket DEM fetch.

Users tune the soil-depth slider and switch between target-level modes
repeatedly over the *same* footprint, and each of those was previously a fresh
multi-tile download. Caching on (snapped bounds, resolution) is the single
biggest win available for both latency and politeness toward Kartverket.

Bounded by total bytes rather than entry count, because one entry can be
anything from a few kilobytes to the 16 Mpx ceiling (64 MB at float32).
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from threading import Lock

import kartverket
from config import settings
from kartverket import Dem

log = logging.getLogger(__name__)

# Configurable because it competes directly with the working set. A single
# calculation can hold a 64 MB float32 DEM plus a 128 MB float64 difference
# array, so on a small machine an over-generous cache is what pushes the
# process into the OOM killer. See DEM_CACHE_MB in .env.example.
MAX_BYTES = settings.dem_cache_mb * 1024 * 1024
TTL_SECONDS = 900

_entries: "OrderedDict[tuple, tuple[float, Dem]]" = OrderedDict()
_bytes = 0
_lock = Lock()

hits = 0
misses = 0


def _key(xmin, ymin, xmax, ymax, res) -> tuple:
    # Snap first, so two polygons that resolve to the same raster share an
    # entry instead of missing on sub-metre differences in their bounds.
    sx0, sy0, sx1, sy1 = kartverket.snap_bounds(xmin, ymin, xmax, ymax, res)
    return (round(sx0, 3), round(sy0, 3), round(sx1, 3), round(sy1, 3), round(res, 4))


def _evict_locked() -> None:
    global _bytes
    now = time.monotonic()
    for k in [k for k, (ts, _) in _entries.items() if now - ts > TTL_SECONDS]:
        _bytes -= _entries.pop(k)[1].z.nbytes
    while _bytes > MAX_BYTES and _entries:
        _, dem = _entries.popitem(last=False)
        _bytes -= dem.z.nbytes


def fetch_dem(xmin: float, ymin: float, xmax: float, ymax: float, res: float) -> Dem:
    global _bytes, hits, misses
    key = _key(xmin, ymin, xmax, ymax, res)

    with _lock:
        entry = _entries.get(key)
        if entry is not None and time.monotonic() - entry[0] <= TTL_SECONDS:
            _entries.move_to_end(key)
            hits += 1
            log.info("DEM cache hit (%d hits / %d misses)", hits, misses)
            return entry[1]

    # Fetched outside the lock: a slow download must not block other requests,
    # and a duplicate concurrent fetch of the same tile is cheaper than
    # serialising every calculation in the process.
    dem = kartverket.fetch_dem(xmin, ymin, xmax, ymax, res)

    with _lock:
        misses += 1
        if key not in _entries:
            _entries[key] = (time.monotonic(), dem)
            _bytes += dem.z.nbytes
            _evict_locked()
    return dem


def stats() -> dict:
    with _lock:
        return {"entries": len(_entries), "bytes": _bytes, "hits": hits, "misses": misses}


def clear() -> None:
    global _bytes
    with _lock:
        _entries.clear()
        _bytes = 0
