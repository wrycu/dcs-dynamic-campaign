"""Borders, rivers, lakes and water for a map: ``data/theaters/<map>/geography.json`` and ``water.json``.

- ``geography.json`` (``tools/import_geography.py``): countries, rivers and lakes from Natural Earth
  (public domain), converted to map coordinates with ``campaign.geo``.
- ``water.json`` (``tools/extract_theater.py``, from DCS): where DCS has water, on a grid
  (``land.getSurfaceType``). Exact for the sea and large lakes; rivers narrower than a cell are
  only in geography.json.

Positions are map x (north) / y (east), metres.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from .model import Pos

Ring = list[tuple[float, float]]


def _in_ring(x: float, y: float, ring: Ring) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


@dataclass
class Area:
    """A country or lake: polygons, each an outer ring then its holes."""
    name: str
    polygons: list[list[Ring]]
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)

    def __post_init__(self) -> None:
        pts = [p for poly in self.polygons for ring in poly for p in ring]
        if pts:
            self.bbox = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))

    def contains(self, p: Pos) -> bool:
        b = self.bbox
        if not (b[0] <= p.x <= b[2] and b[1] <= p.y <= b[3]):
            return False
        for poly in self.polygons:
            if _in_ring(p.x, p.y, poly[0]) and not any(_in_ring(p.x, p.y, h) for h in poly[1:]):
                return True
        return False


@dataclass
class WaterGrid:
    """DCS surface types on a grid: True where there's water (shallow or deep)."""
    x0: float
    y0: float
    cell_m: float
    rows: list[list[int]]   # per row (north), run lengths alternating land, water, land... starting with land

    def is_water(self, p: Pos) -> bool | None:
        i = round((p.x - self.x0) / self.cell_m)
        j = round((p.y - self.y0) / self.cell_m)
        if not (0 <= i < len(self.rows)) or j < 0:
            return None
        k, water = 0, False
        for run in self.rows[i]:
            k += run
            if j < k:
                return water
            water = not water
        return None


@dataclass
class Geography:
    countries: list[Area] = field(default_factory=list)
    lakes: list[Area] = field(default_factory=list)
    rivers: dict[str, list[Ring]] = field(default_factory=dict)   # name -> polylines
    water: WaterGrid | None = None

    def country_at(self, p: Pos) -> str | None:
        return next((c.name for c in self.countries if c.contains(p)), None)

    def country_near(self, p: Pos, max_m: float = 3_000) -> str | None:
        """The country at p, or, for a point just off a coastline, the nearest within max_m."""
        hit = self.country_at(p)
        r = 500.0
        while hit is None and r <= max_m:
            for k in range(8):
                q = Pos(p.x + r * math.cos(k * math.pi / 4), p.y + r * math.sin(k * math.pi / 4))
                hit = hit or self.country_at(q)
            r *= 2
        return hit

    def is_water(self, p: Pos) -> bool:
        if self.water is not None:
            w = self.water.is_water(p)
            if w is not None:
                return w
        return any(lake.contains(p) for lake in self.lakes)


def load_geography(directory: Path) -> Geography:
    """What's there of geography.json and water.json in a theater directory (either may be missing)."""
    g = Geography()
    gp = directory / "geography.json"
    if gp.exists():
        d = json.loads(gp.read_text(encoding="utf-8"))
        g.countries = [Area(c["name"], [[[tuple(p) for p in r] for r in poly] for poly in c["polygons"]])
                       for c in d.get("countries", [])]
        g.lakes = [Area(c["name"], [[[tuple(p) for p in r] for r in poly] for poly in c["polygons"]])
                   for c in d.get("lakes", [])]
        g.rivers = {r["name"]: [[tuple(p) for p in ln] for ln in r["lines"]] for r in d.get("rivers", [])}
    wp = directory / "water.json"
    if wp.exists():
        w = json.loads(wp.read_text())
        g.water = WaterGrid(w["x0"], w["y0"], w["cell_m"], w["rows"])
    return g


def water_rows(rows: list[str]) -> list[list[int]]:
    """Run lengths (land first) from DCS surface-type digit rows (2, 3: water)."""
    out = []
    for row in rows:
        runs, cur, n = [], False, 0
        for ch in row:
            w = ch in "23"
            if w != cur:
                runs.append(n)
                cur, n = w, 0
            n += 1
        runs.append(n)
        out.append(runs)
    return out
