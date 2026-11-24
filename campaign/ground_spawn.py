"""Turn an aggregated GroundUnit into a DCS group table for ``spawn_group``.

The layout is deterministic per formation id, so a unit that despawns and respawns in the
same place looks the same. Before a formation's first spawn the spawner moves each vehicle of
this layout to clear ground nearby (the mission's ``clear_spots`` query, see spawner.py) and keeps
the spots in ``GroundUnit.spots``; from then on vehicles spawn on those spots.
"""
from __future__ import annotations

import math
import random
import zlib
from dataclasses import dataclass

from .catalog import COUNTRY_FOR_SIDE, SAM_CORE
from .model import GroundRole, GroundUnit, Side
from .protocol import PREFIX_GROUND, group_name, unit_name

SPACING_M = 40.0
JITTER_M = 8.0
SAM_RING_M = 150.0

SKILL_FOR_SIDE = {Side.BLUE: "Good", Side.RED: "Average", Side.NEUTRAL: "Average"}


@dataclass
class GroundSpawn:
    data: dict            # the spawn_group command payload
    unit_types: dict[str, str]  # DCS unit name -> type, to attribute kills


def _rng(unit_id: str) -> random.Random:
    return random.Random(zlib.crc32(unit_id.encode()))


def _expand(composition: dict[str, int]) -> list[str]:
    return [t for t in sorted(composition) for _ in range(max(0, composition[t]))]


def _grid(n: int, rng: random.Random) -> list[tuple[float, float]]:
    """Offsets (forward, right) for n vehicles in a loose block centred on the origin."""
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = math.ceil(n / cols)
    out = []
    for i in range(n):
        r, c = divmod(i, cols)
        out.append(((rows - 1) / 2 * SPACING_M - r * SPACING_M + rng.uniform(-JITTER_M, JITTER_M),
                    c * SPACING_M - (cols - 1) / 2 * SPACING_M + rng.uniform(-JITTER_M, JITTER_M)))
    return out


def _sam(types: list[str], rng: random.Random) -> list[tuple[float, float]]:
    """Radars and command posts in the middle, launchers and everything else on a ring."""
    core = [i for i, t in enumerate(types) if t in SAM_CORE]
    ring = [i for i, t in enumerate(types) if t not in SAM_CORE]
    out: list[tuple[float, float]] = [(0.0, 0.0)] * len(types)
    for k, i in enumerate(core):
        a = 2 * math.pi * k / max(1, len(core))
        rad = 0.0 if len(core) == 1 else 30.0
        out[i] = (rad * math.cos(a), rad * math.sin(a))
    radius = max(SAM_RING_M, len(ring) * SPACING_M / (2 * math.pi))
    start = rng.uniform(0, 2 * math.pi)
    for k, i in enumerate(ring):
        a = start + 2 * math.pi * k / max(1, len(ring))
        out[i] = (radius * math.cos(a), radius * math.sin(a))
    return out


def build_ground_spawn(unit: GroundUnit, heading_deg: float | None = None) -> GroundSpawn:
    """Build the spawn_group payload for a formation at its current position."""
    rng = _rng(unit.id)
    types = _expand(unit.composition)
    if not types:
        raise ValueError(f"ground unit {unit.id} has no vehicles")
    hdg = math.radians(heading_deg if heading_deg is not None else rng.uniform(0, 360))
    is_sam = unit.role in (GroundRole.SAM, GroundRole.AAA)
    offsets = _sam(types, rng) if is_sam else _grid(len(types), rng)
    cos_h, sin_h = math.cos(hdg), math.sin(hdg)
    # Sited spots, if they cover every vehicle (after losses the first ones are used).
    spots = unit.spots if unit.sited and len(unit.spots) >= len(types) else []

    units, unit_types = [], {}
    for i, (t, (fwd, right)) in enumerate(zip(types, offsets), start=1):
        name = unit_name(PREFIX_GROUND, unit.id, i)
        if i <= len(spots):
            x, y = spots[i - 1].x, spots[i - 1].y
        else:
            # Rotate (forward, right) by the heading: forward is north (+x), right is east (+y).
            x = unit.pos.x + fwd * cos_h - right * sin_h
            y = unit.pos.y + fwd * sin_h + right * cos_h
        if is_sam:
            # Launchers face outward from the site centre.
            facing = math.atan2(y - unit.pos.y, x - unit.pos.x) if (fwd or right) else hdg
        else:
            facing = hdg + rng.uniform(-0.15, 0.15)
        units.append({
            "name": name, "type": t, "x": round(x, 1), "y": round(y, 1),
            "heading": round(facing % (2 * math.pi), 4),
            "skill": SKILL_FOR_SIDE.get(unit.side, "Average"),
            "playerCanDrive": False,
        })
        unit_types[name] = t

    data = {
        "category": "ground",
        "country": COUNTRY_FOR_SIDE.get(unit.side, COUNTRY_FOR_SIDE[Side.RED]),
        "group": {
            "name": group_name(PREFIX_GROUND, unit.id),
            "task": "Ground Nothing",
            "units": units,
        },
    }
    return GroundSpawn(data, unit_types)
