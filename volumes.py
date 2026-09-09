"""
Cut/fill volume engine.

All volumes are computed on the raster grid: every cell contributes
(elevation - target level) * cell area. Positive is cut (skjæring),
negative is fill (fylling).

Volume vocabulary used throughout:

  fast / anbrakt (bank)  in-situ volume, straight off the terrain model
  løst (loose)           bank volume after excavation swell - what gets trucked
  komprimert (compacted) volume in a finished fill

A fill of V m3 needs more than V m3 of bank material, because material loses
volume when compacted. That is the `shrinkage` parameter.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np


@dataclass
class Params:
    soil_depth: float = 2.0      # løsmassedybde, m - cut above this is soil
    swell_soil: float = 0.25     # 25 % volume gain when soil is excavated
    swell_rock: float = 0.50     # 50 % for blasted rock
    shrinkage: float = 0.10      # bank volume needed per unit of finished fill
    truck_capacity: float = 15.0  # m3 loose per lorry load


@dataclass
class Terrain:
    """Pre-sorted elevations inside the polygon, for fast level sweeps."""

    z_sorted: np.ndarray
    cumsum: np.ndarray
    cell_area: float

    @classmethod
    def from_values(cls, z: np.ndarray, cell_area: float) -> "Terrain":
        zs = np.sort(np.asarray(z, dtype="float64"))
        return cls(z_sorted=zs, cumsum=np.concatenate([[0.0], np.cumsum(zs)]), cell_area=cell_area)

    @property
    def n(self) -> int:
        return self.z_sorted.size

    def cut_fill(self, level: float) -> tuple[float, float]:
        """(cut, fill) geometric volumes in m3 at the given target level."""
        k = int(np.searchsorted(self.z_sorted, level, side="right"))
        below_sum = self.cumsum[k]
        total_sum = self.cumsum[-1]

        fill = (k * level - below_sum) * self.cell_area
        cut = ((total_sum - below_sum) - (self.n - k) * level) * self.cell_area
        return max(cut, 0.0), max(fill, 0.0)

    def net_bank(self, level: float, shrinkage: float) -> float:
        """Surplus bank material at this level: cut minus what the fill consumes."""
        cut, fill = self.cut_fill(level)
        return cut - fill * (1.0 + shrinkage)


def solve_balanced_level(terrain: Terrain, shrinkage: float) -> float:
    """
    Target level where the cut exactly feeds the fill - nothing on or off site.

    net_bank is strictly decreasing in level, so bisection is safe. With
    shrinkage = 0 this converges on the mean elevation, which is the textbook
    balance point. A positive shrinkage pushes the level DOWN: a fill swallows
    more bank material than its own volume, so the design has to dig deeper to
    feed it.
    """
    lo = float(terrain.z_sorted[0])
    hi = float(terrain.z_sorted[-1])

    if terrain.net_bank(lo, shrinkage) <= 0:
        return lo
    if terrain.net_bank(hi, shrinkage) >= 0:
        return hi

    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if terrain.net_bank(mid, shrinkage) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return 0.5 * (lo + hi)


def resolve_level(terrain: Terrain, mode: str, fixed_level: float | None, shrinkage: float) -> float:
    if mode == "fast":
        if fixed_level is None:
            raise ValueError("Fast kotehøyde mangler.")
        return float(fixed_level)
    if mode == "laveste":
        return float(terrain.z_sorted[0])
    if mode == "balansert":
        return solve_balanced_level(terrain, shrinkage)
    raise ValueError(f"Ukjent modus: {mode}")


def compute(z_inside: np.ndarray, cell_area: float, level: float, p: Params) -> dict:
    """
    Full volume breakdown at a given target level.

    z_inside is the flat array of valid elevations inside the polygon.
    """
    z = np.asarray(z_inside, dtype="float64")
    d = z - level

    cut_depth = np.clip(d, 0.0, None)
    fill_depth = np.clip(-d, 0.0, None)

    # Depth-based material split: the first `soil_depth` metres below the
    # existing surface is loose material, everything deeper is rock.
    soil_depth_cell = np.minimum(cut_depth, p.soil_depth)
    rock_depth_cell = cut_depth - soil_depth_cell

    cut_soil = float(soil_depth_cell.sum() * cell_area)
    cut_rock = float(rock_depth_cell.sum() * cell_area)
    cut_total = cut_soil + cut_rock
    fill_void = float(fill_depth.sum() * cell_area)

    fill_bank_needed = fill_void * (1.0 + p.shrinkage)
    net_bank = cut_total - fill_bank_needed

    # Material moved into the fill is taken from the cut in the same proportion
    # as the cut's own composition - a neutral assumption. Swap this if the
    # project specifies rock fill.
    if cut_total > 0:
        soil_share = cut_soil / cut_total
    else:
        soil_share = 1.0

    used_for_fill = min(fill_bank_needed, cut_total)
    fill_from_soil = used_for_fill * soil_share
    fill_from_rock = used_for_fill * (1.0 - soil_share)

    surplus_soil = max(cut_soil - fill_from_soil, 0.0)
    surplus_rock = max(cut_rock - fill_from_rock, 0.0)
    deficit_bank = max(fill_bank_needed - cut_total, 0.0)

    loose_soil = surplus_soil * (1.0 + p.swell_soil)
    loose_rock = surplus_rock * (1.0 + p.swell_rock)
    loose_export = loose_soil + loose_rock
    loose_import = deficit_bank * (1.0 + p.swell_soil)

    n = z.size
    area = n * cell_area

    return {
        "level": level,
        "area_m2": area,
        "cells": int(n),
        "cell_area_m2": cell_area,
        "terrain": {
            "min": float(z.min()) if n else None,
            "max": float(z.max()) if n else None,
            "mean": float(z.mean()) if n else None,
            "median": float(np.median(z)) if n else None,
        },
        "cut": {
            "total_bank_m3": cut_total,
            "soil_bank_m3": cut_soil,
            "rock_bank_m3": cut_rock,
            "max_depth_m": float(cut_depth.max()) if n else 0.0,
            "mean_depth_m": float(cut_depth.mean()) if n else 0.0,
            "area_m2": float((d > 0).sum() * cell_area),
        },
        "fill": {
            "void_m3": fill_void,
            "bank_needed_m3": fill_bank_needed,
            "max_depth_m": float(fill_depth.max()) if n else 0.0,
            "mean_depth_m": float(fill_depth.mean()) if n else 0.0,
            "area_m2": float((d < 0).sum() * cell_area),
        },
        "balance": {
            "net_bank_m3": net_bank,
            "surplus_soil_bank_m3": surplus_soil,
            "surplus_rock_bank_m3": surplus_rock,
            "deficit_bank_m3": deficit_bank,
            "export_loose_m3": loose_export,
            "export_loose_soil_m3": loose_soil,
            "export_loose_rock_m3": loose_rock,
            "import_loose_m3": loose_import,
            "truck_loads": (loose_export if loose_export > 0 else loose_import) / p.truck_capacity
            if p.truck_capacity > 0 else 0.0,
        },
        "params": asdict(p),
    }


def sensitivity(terrain: Terrain, p: Params, steps: int = 80) -> list[dict]:
    """Net bank volume as a function of target level, for the sweep chart."""
    lo = float(terrain.z_sorted[0])
    hi = float(terrain.z_sorted[-1])
    if hi - lo < 1e-9:
        return []

    out = []
    for level in np.linspace(lo, hi, steps):
        cut, fill = terrain.cut_fill(float(level))
        out.append({
            "level": float(level),
            "cut_m3": cut,
            "fill_m3": fill,
            "net_m3": cut - fill * (1.0 + p.shrinkage),
        })
    return out
