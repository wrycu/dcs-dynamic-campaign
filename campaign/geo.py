"""DCS map coordinates from latitude/longitude, without DCS.

Each DCS terrain is a transverse Mercator projection on WGS84, with map x = northing and
y = easting (metres). The parameters are per terrain (the same ones pydcs and other tools use),
and are checked against DCS's own ``coord.LLtoLO`` in tests/test_geo.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .model import Pos

A = 6378137.0                 # WGS84
F = 1 / 298.257223563
E2 = F * (2 - F)
EP2 = E2 / (1 - E2)


@dataclass(frozen=True)
class TM:
    lon0: float          # central meridian, degrees
    k0: float
    false_easting: float
    false_northing: float


TERRAINS = {
    "Syria": TM(39.0, 0.9996, 282801.00000003993, -3879865.9999999935),
    "Caucasus": TM(33.0, 0.9996, -99516.9999999732, -4998114.999999984),
    "PersianGulf": TM(57.0, 0.9996, 75755.99999999645, -2894933.0000000377),
    "MarianaIslands": TM(147.0, 0.9996, 238417.99999989968, -1491840.000000048),
    "Nevada": TM(-117.0, 0.9996, -193996.80999964548, -4410028.063999966),
    "SinaiMap": TM(33.0, 0.9996, 169221.9999999585, -3325312.9999999693),
    "Kola": TM(21.0, 0.9996, -62702.00000000087, -7543624.999999979),
    "Afghanistan": TM(63.0, 0.9996, -300149.9999999864, -3759657.000000049),
}


def _meridian_arc(phi: float) -> float:
    e4, e6 = E2 * E2, E2 * E2 * E2
    return A * ((1 - E2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi
                - (3 * E2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * phi)
                + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * phi)
                - (35 * e6 / 3072) * math.sin(6 * phi))


def ll_to_xy(terrain: str, lat: float, lon: float) -> Pos:
    """Map position (x north, y east) of a latitude/longitude, as DCS's coord.LLtoLO gives it."""
    p = TERRAINS[terrain]
    phi, lam = math.radians(lat), math.radians(lon - p.lon0)
    n = A / math.sqrt(1 - E2 * math.sin(phi) ** 2)
    t = math.tan(phi) ** 2
    c = EP2 * math.cos(phi) ** 2
    a = lam * math.cos(phi)
    easting = p.k0 * n * (a + (1 - t + c) * a ** 3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * EP2) * a ** 5 / 120)
    northing = p.k0 * (_meridian_arc(phi) + n * math.tan(phi) * (
        a * a / 2 + (5 - t + 9 * c + 4 * c * c) * a ** 4 / 24
        + (61 - 58 * t + t * t + 600 * c - 330 * EP2) * a ** 6 / 720))
    return Pos(northing + p.false_northing, easting + p.false_easting)
