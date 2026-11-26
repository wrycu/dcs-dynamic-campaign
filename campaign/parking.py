"""Choosing parking spots an aircraft fits.

DCS's ``Airbase.getParking`` (the ``parking`` query) lists spots by ``Term_Index`` and type, but not
their size, and a ramp start in a spot too small for the aircraft spawns it damaged: Run 25's Hornet
in one of Khalkhalah's hardened shelters, wing through the wall, aborted and ejected at once. The
Mission Editor checks each stand's ``WIDTH``, ``LENGTH`` and ``HEIGHT`` against the aircraft's span,
length and height (``me_parking.lua``); the Hooks ``stands`` command reads the same stand list,
and the mission ``type_size`` query gives the aircraft's size.

Spots and stands are matched by position. Without stand sizes, shelters are left out.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .model import Pos

SHELTER = 68                      # Term_Type of a hardened shelter
FIGHTER_SPOTS = (68, 104, 72)     # Term_Types a fighter can use, best first
MATCH_M = 10.0                    # a spot and a stand this close are the same place


@dataclass(frozen=True)
class Stand:
    pos: Pos
    shelter: bool
    airplanes: bool
    width: float | None
    length: float | None
    height: float | None = None


@dataclass(frozen=True)
class Size:
    span: float
    length: float
    height: float


def stands_from(raw: dict[str, Any]) -> list[Stand]:
    """Stands from a Hooks ``stands`` reply."""
    out = []
    for s in raw.get("stands") or []:
        def num(k: str) -> float | None:
            v = s.get(k)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        out.append(Stand(Pos(float(s["x"]), float(s["y"])), bool(s.get("shelter")), bool(s.get("airplanes", True)),
                         num("width"), num("length"), num("height")))
    return out


def size_from(raw: dict[str, Any]) -> Size:
    return Size(float(raw["span"]), float(raw["length"]), float(raw["height"]))


def fits(stand: Stand, size: Size) -> bool:
    """The Mission Editor's test: smaller than the stand in span, length and (if given) height."""
    if not stand.airplanes or stand.width is None or stand.length is None:
        return False
    return size.span < stand.width and size.length < stand.length and (stand.height is None or size.height < stand.height)


def _nearest(pos: Pos, stands: list[Stand]) -> Stand | None:
    best = min(stands, key=lambda s: math.hypot(s.pos.x - pos.x, s.pos.y - pos.y), default=None)
    if best is None or math.hypot(best.pos.x - pos.x, best.pos.y - pos.y) > MATCH_M:
        return None
    return best


def usable_spots(spots: list[dict[str, Any]], stands: list[Stand] | None, size: Size | None,
                 types: tuple[int, ...] = FIGHTER_SPOTS) -> list[dict[str, Any]]:
    """The ``parking`` query's spots (``{term, type, pos}``) of the given types the aircraft fits, in
    their order. With stand sizes and the aircraft's size, a spot must match a stand it fits;
    without them, shelters are left out."""
    out = []
    for s in spots:
        if s.get("type") not in types:
            continue
        if stands is None or size is None:
            if s.get("type") != SHELTER:
                out.append(s)
            continue
        stand = _nearest(Pos(s["pos"]["x"], s["pos"]["y"]), stands)
        if stand is not None and fits(stand, size):
            out.append(s)
    return out
