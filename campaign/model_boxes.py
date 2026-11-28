"""Scenery models' bounding boxes, read from the terrain's model files (Run 32: DCS gives almost
no scenery a box through the Lua API, so a building's size wasn't known, and the ride's Humvee
started inside a terminal).

A terrain keeps its scenery models in ``Mods/terrains/<theatre>/Models/<set>/<set>.ref``
(``landscape4::lReferenceFile``). What this reads (decoded 2026-11-28 from Syria's
AirfieldsBuilding.ref, DCS 2.9.32): each model's name as an int32 length and that many bytes
(the scenery type name, lower case), then 16 bytes, then its box as six float32s, min (x, up, z)
and max (x, up, z), in the model's own axes (x forward, z right, as ``getDesc().box``). Records
are found by scanning for that shape. Checked against the user's screenshots: the airfield HQ is
25 x 77 m, an office 15 x 21 m, a shed 7 x 22 m, an aircraft shelter 59 x 66 m.

``data/theaters/<map>/model_boxes.json``: type name (lower case) -> [min x, min up, min z, max x,
max up, max z]. Written by ``python3 -m tools.extract_model_boxes``.
"""
from __future__ import annotations

import json
import re
import struct
from pathlib import Path

NAME = re.compile(rb"[a-z0-9_\-\.]{3,64}\x00")
MAX_M = 2000.0


def parse(data: bytes) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for m in NAME.finditer(data):
        s = m.start()
        name = m.group()[:-1]
        if s < 4 or struct.unpack_from("<i", data, s - 4)[0] != len(name):
            continue
        o = s + len(name) + 16
        if o + 24 > len(data):
            continue
        v = struct.unpack_from("<6f", data, o)
        if all(abs(x) < MAX_M for x in v) and v[3] > v[0] and v[4] > v[1] and v[5] > v[2]:
            out.setdefault(name.decode(), [round(x, 2) for x in v])
    return out


def read_terrain(terrain_dir: Path) -> dict[str, list[float]]:
    """Every model's box in the terrain's ``Models/*/*.ref`` files."""
    out: dict[str, list[float]] = {}
    for ref in sorted((terrain_dir / "Models").glob("*/*.ref")):
        for k, v in parse(ref.read_bytes()).items():
            out.setdefault(k, v)
    return out


def load(path: Path) -> dict[str, list[float]]:
    return json.loads(path.read_text()) if path.is_file() else {}


def path_for(theatre: str) -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "theaters" / theatre.lower() / "model_boxes.json"
