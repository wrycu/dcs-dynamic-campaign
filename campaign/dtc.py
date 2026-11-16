"""Write native DCS data cartridges (.dtc) so players can load campaign waypoints in the jet.

A .dtc is JSON: ``{"type": <aircraft type>, "name": <name>, "data": {...}}``. ``data`` follows
the per-aircraft template in ``CoreMods/aircraft/<module>/DTC/<type>_DTC.lua``. The in-sim DTC
manager (briefing screen, dynamic-slot dialog, or in the cockpit, e.g. the Hornet's
RShift+RCtrl+K "Open DTC Interface") can import any .dtc from ``Saved Games/DCS/DTC/``.

Formats were read from the DCS 2.9.31 install; only the F/A-18C is implemented so far.
Coordinates are DCS map metres (x north, y east), the same as ``model.Pos``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .model import Pos


@dataclass
class DtcWaypoint:
    pos: Pos
    elev_m: float      # terrain elevation at the point (the Hornet stores waypoint elevation)
    note: str = ""


def _hornet_nav_point(num: int, wp: DtcWaypoint, route: int | None = None) -> dict:
    # Field set copied from WYPT_NAV.lua (the record the DTC map editor creates).
    pt = {
        "id": f"STPT{num}", "idOA": f"OA{num}", "idOA_Line": f"OA{num}Line",
        "wypt_num": num, "x": wp.pos.x, "y": wp.pos.y, "alt": wp.elev_m,
        "note": wp.note, "text_note": wp.note,
        "R1": False, "R2": False, "R3": False,
        "altitudeType": 1, "velocityType": 3,
        "isOA": False, "OA_Range": 0.0, "OA_Bearing": 0.0, "OA_X": 0.0, "OA_Y": 0.0, "OA_Alt": 0.0,
        "OA_DeltaX": 0.0, "OA_DeltaY": 0.0,
        "OA_Bearing_Units": 1, "OA_Range_Units": 1, "OA_Elevation_Units": 1,
    }
    if route is not None:
        pt[f"R{route}"] = True
        pt[f"R{route}_order"] = num
    return pt


def _route_plan(route: int, num: int, wp: DtcWaypoint) -> dict:
    # What ROUTE_SEQ.lua creates for each point of a sequence (speed in km/h, alt in m).
    return {"route_num": route, "wypt_num": num, "alt": wp.elev_m, "altitudeType": 1,
            "speed": 463.0, "ETA": 0, "FIX_Time": False, "TGT": False}


def hornet_dtc(name: str, theatre: str, waypoints: list[DtcWaypoint], route: int | None = None) -> dict:
    """An F/A-18C cartridge whose waypoints 1..n are the given points.

    By default the points are in no route sequence, like points made in the DTC editor. With
    ``route`` (1-3) they also form that sequence, with the per-point planning data
    (``NAV_ROUTE``) the editor writes for one.

    Only the WYPT partition is written. On import (``setData`` in FA-18C_hornet_DTC.lua) every
    partition present replaces the jet's defaults and every partition left out keeps them, so
    stub partitions are harmful: an empty ``SA.FAOR_FLOT`` crashes the importer and breaks the
    DTC window. ``theatre`` must equal the terrain id (``env.mission.theatre``, e.g. "Syria"),
    or the waypoints are silently dropped.
    """
    if not 1 <= len(waypoints) <= 59:
        raise ValueError("the Hornet takes 1..59 waypoints")
    if route is not None and route not in (1, 2, 3):
        raise ValueError("route sequence must be 1, 2 or 3")
    wypt: dict = {
        "NAV_PTS": [_hornet_nav_point(i, wp, route) for i, wp in enumerate(waypoints, start=1)],
        "mirror_NAV_PTS": False,  # use these points, not the mission editor route
    }
    if route is not None:
        # The importer indexes NAV_ROUTE[1..3]; a JSON array decodes to exactly that.
        routes: list[dict] = [{}, {}, {}]
        routes[route - 1] = {f"STPT{i}": _route_plan(route, i, wp) for i, wp in enumerate(waypoints, start=1)}
        wypt["NAV_ROUTE"] = routes
    data = {
        "type": "FA-18C_hornet", "name": name, "terrain": theatre,
        "WYPT": wypt,
    }
    return {"type": "FA-18C_hornet", "name": name, "data": data}


def write_dtc(directory: Path, dtc: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{dtc['name']}.dtc"
    path.write_text(json.dumps(dtc, indent=4))
    return path
