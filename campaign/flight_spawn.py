"""Build the DCS group table for a campaign ``Flight`` (a ``spawn_group`` command with category airplane).

Names: group ``fl:<flight id>``, units ``fl:<flight id>#<n>``.

Route: an optional start point (takeoff from an airbase, or the airborne spawn position), then the
flight's waypoints from ``next_wp`` on. Waypoint actions:

- ``None``    fly through
- ``ORBIT``   circle the point for ``params.duration_s`` (default 1200) at the waypoint's altitude
- ``CAS``     engage ground units within ``params.radius_m`` (default 9 km) of the point
- ``SEAD``    engage air defences within ``params.radius_m`` (default 30 km) of the point
- ``BOMB``    bomb the point
- ``EXIT``    fly there; the air spawner despawns the flight on arrival (leaving the map)
- ``LAND``    land at ``params.airdrome_id``; must be last

Every flight also gets, at its first point: ROE (fighters weapons free, ferries weapons hold, others
open fire: only what their tasks name), RTB on bingo fuel, and an en-route
"engage air targets within ``AIR_ENGAGE_M``" for fighter missions (CAP, SWEEP, ESCORT, INTERCEPT).
"""
from __future__ import annotations

import math
import zlib
from dataclasses import dataclass

from .catalog import COUNTRY_FOR_SIDE
from .model import Flight, Pos, Side, StartMode, Waypoint
from .payloads import dcs_payload
from .protocol import PREFIX_FLIGHT, group_name, unit_name

AIR_ENGAGE_M = 60_000      # fighters engage air targets this far from their route
CRUISE_MS = 220.0          # default route speed (about 430 kt TAS)
ORBIT_DEFAULT_S = 1200
# Waypoint actions that attack the ground, and their default zone radius in metres (BOMB: the point).
ATTACK_RADIUS_M = {"CAS": 9_000, "SEAD": 30_000, "BOMB": 2_000}
FORMATION_M = 150          # spacing between aircraft at an air start

# Flight.mission -> DCS group task (determines which tasks and options DCS allows the group)
GROUP_TASK = {
    "BARCAP": "CAP", "CAP": "CAP", "TARCAP": "CAP", "SWEEP": "Fighter Sweep", "ESCORT": "Escort",
    "INTERCEPT": "Intercept", "SEAD": "SEAD", "DEAD": "SEAD", "CAS": "CAS", "BAI": "CAS",
    "STRIKE": "Ground Attack",
}
FIGHTER_MISSIONS = {"BARCAP", "CAP", "TARCAP", "SWEEP", "ESCORT", "INTERCEPT"}
NON_COMBAT_MISSIONS = {"FERRY", "TRANSPORT"}   # weapons hold
SKILL = {Side.BLUE: "High", Side.RED: "Good"}
# Western callsign names every NATO-side type has (index = DCS callsign number).
CALLSIGN_NAMES = ("Enfield", "Springfield", "Uzi", "Colt", "Dodge", "Ford", "Chevy", "Pontiac")
WEAPON_AUTO = 1073741822   # "any suitable weapon" flag the Mission Editor writes for bombing tasks

# DCS AI option ids/values (AI.Option.Air)
OPT_ROE, ROE_WEAPON_FREE, ROE_OPEN_FIRE, ROE_WEAPON_HOLD = 0, 0, 2, 4
OPT_RTB_ON_BINGO = 6


@dataclass
class Takeoff:
    """Where a ramp/runway start happens."""
    airdrome_id: int        # Airbase:getID()
    pos: Pos                # airbase reference point
    elev_m: float
    parking: list[tuple[int, Pos]] | None = None  # (Term_Index, position) per aircraft; ramp starts only


@dataclass
class FlightSpawn:
    data: dict              # the spawn_group command payload
    unit_names: list[str]


def flight_number_hint(flight_id: str) -> int:
    """Stable 1-89 starting guess for a flight's number (see ``unit_identity``)."""
    return zlib.crc32(flight_id.encode()) % 89 + 1


def unit_identity(side: Side, number: int, i: int) -> dict:
    """DCS ``callsign`` and ``onboard_num`` for aircraft ``i`` (1-based) of flight ``number`` (1-89).

    Blue: "<name><flight><i>", name and flight digit both from ``number``, e.g. 13 -> Uzi 4-1, 4-2.
    Red: numeric callsigns <number><i> (101, 102, ...), as Russian-side units use.
    Board numbers are <number><i> for both, so they are unique while flight numbers are.
    """
    if not 1 <= number <= 89:
        raise ValueError(f"flight number {number} out of range 1-89")
    board = f"{number:02d}{i}"
    if side is Side.RED:
        return {"callsign": int(f"{number + 10}{i}"), "onboard_num": board}
    name_ix, flight_no = divmod(number - 1, 9)
    name_ix %= len(CALLSIGN_NAMES)
    name = CALLSIGN_NAMES[name_ix]
    return {"callsign": {1: name_ix + 1, 2: flight_no + 1, 3: i, "name": f"{name}{flight_no + 1}{i}"},
            "onboard_num": board}


def _heading(a: Pos, b: Pos) -> float:
    """Radians clockwise from north (x north, y east)."""
    return math.atan2(b.y - a.y, b.x - a.x) % (2 * math.pi)


def _task(n: int, task_id: str, params: dict) -> dict:
    return {"number": n, "auto": False, "enabled": True, "id": task_id, "params": params}


def _option(n: int, name: int, value) -> dict:
    return _task(n, "WrappedAction", {"action": {"id": "Option", "params": {"name": name, "value": value}}})


def _wp_tasks(wp: Waypoint, orbit_s: float | None = None) -> list[dict]:
    p = wp.params
    pt = {"x": wp.pos.x, "y": wp.pos.y}
    if wp.action == "ORBIT":
        orbit = {"id": "Orbit", "params": {"pattern": "Circle", "point": pt, "altitude": wp.alt_m,
                                           "speed": p.get("speed_ms", CRUISE_MS * 0.8)}}
        return [_task(1, "ControlledTask", {"task": orbit,
                                            "stopCondition": {"duration": round(
                                                orbit_s if orbit_s is not None
                                                else p.get("duration_s", ORBIT_DEFAULT_S))}})]
    if wp.action == "CAS":
        return [_task(1, "EngageTargetsInZone", {
            "point": pt, "zoneRadius": p.get("radius_m", ATTACK_RADIUS_M["CAS"]), "priority": 0,
            "targetTypes": ["Ground Units", "Helicopters", "Light armed ships"]})]
    if wp.action == "SEAD":
        return [_task(1, "EngageTargetsInZone", {
            "point": pt, "zoneRadius": p.get("radius_m", ATTACK_RADIUS_M["SEAD"]), "priority": 0,
            "targetTypes": ["Air Defence"]})]
    if wp.action == "BOMB":
        return [_task(1, "Bombing", {
            "point": pt, "weaponType": WEAPON_AUTO, "expend": "Auto", "attackQtyLimit": False,
            "groupAttack": True, "altitudeEnabled": False, "directionEnabled": False})]
    return []


def _point(wp: Waypoint, speed: float, orbit_s: float | None = None) -> dict:
    pt = {"x": wp.pos.x, "y": wp.pos.y, "alt": wp.alt_m, "alt_type": "BARO", "speed": speed,
          "speed_locked": True, "type": "Turning Point", "action": "Turning Point"}
    if wp.action == "LAND":
        pt.update(type="Land", action="Landing", airdromeId=wp.params["airdrome_id"], alt_type="RADIO")
    tasks = _wp_tasks(wp, orbit_s)
    if tasks:
        pt["task"] = {"id": "ComboTask", "params": {"tasks": tasks}}
    return pt


def _first_tasks(flight: Flight) -> list[dict]:
    # Fighters are weapons free. Everything else fires only at what its tasks name (open fire):
    # weapons free, Run 15's CAS lead left its attack to go after an SA-6 32 nm off its route.
    mission = flight.mission.upper()
    roe = (ROE_WEAPON_HOLD if mission in NON_COMBAT_MISSIONS else
           ROE_WEAPON_FREE if mission in FIGHTER_MISSIONS else ROE_OPEN_FIRE)
    tasks = [_option(1, OPT_ROE, roe), _option(2, OPT_RTB_ON_BINGO, True)]
    if flight.mission.upper() in FIGHTER_MISSIONS:
        tasks.append(_task(3, "EngageTargets", {"targetTypes": ["Air"], "priority": 0,
                                                "maxDistEnabled": True, "maxDist": AIR_ENGAGE_M}))
    return tasks


def build_flight_spawn(flight: Flight, aircraft_type: str, side: Side, *,
                       takeoff: Takeoff | None = None, uncontrolled: bool = False,
                       speed_ms: float = CRUISE_MS, number: int | None = None,
                       orbit_left_s: float | None = None) -> FlightSpawn:
    """DCS spawn for ``flight``.

    AIR starts begin at the flight's last known position (``pos``/``alt_m``/``hdg``/``speed_ms``,
    set when it was aggregated) or, if it has none, at its next waypoint. Other starts need
    ``takeoff``; RAMP_* starts also need one parking spot per aircraft. ``uncontrolled`` parks a
    RAMP_COLD flight until a ``Start`` command. ``number`` (1-89) sets callsigns and board numbers;
    by default it comes from the flight id. ``orbit_left_s`` shortens an ORBIT the flight is
    already in (its next waypoint) to what's left of it.
    """
    if flight.aircraft < 1:
        raise ValueError(f"flight {flight.id} has no aircraft")
    route = flight.waypoints[flight.next_wp:]
    if not route:
        raise ValueError(f"flight {flight.id} has no waypoints left")
    if any(wp.action == "LAND" for wp in route[:-1]):
        raise ValueError("LAND must be the last waypoint")
    gname = group_name(PREFIX_FLIGHT, flight.id)
    names = [unit_name(PREFIX_FLIGHT, flight.id, i) for i in range(1, flight.aircraft + 1)]
    payload = dcs_payload(aircraft_type, flight.loadout, flight.fuel)

    points: list[dict] = []
    units: list[dict] = []
    if flight.start is StartMode.AIR:
        start = flight.pos or route[0].pos
        alt = flight.alt_m if flight.alt_m is not None else route[0].alt_m
        hdg = math.radians(flight.hdg) if flight.hdg is not None else _heading(start, route[0].pos)
        spd = flight.speed_ms or speed_ms
        if flight.pos is not None:  # resuming: a start point where it is, then the rest of the route
            points.append({"x": start.x, "y": start.y, "alt": alt, "alt_type": "BARO", "speed": spd,
                           "speed_locked": True, "type": "Turning Point", "action": "Turning Point"})
        # Echelon right, behind the leader, along the heading.
        fwd = (math.cos(hdg), math.sin(hdg))
        right = (-fwd[1], fwd[0])
        for i, name in enumerate(names):
            back, side_off = -FORMATION_M * i, FORMATION_M * i
            units.append({"name": name, "type": aircraft_type, "skill": SKILL[side],
                          "x": start.x + fwd[0] * back + right[0] * side_off,
                          "y": start.y + fwd[1] * back + right[1] * side_off,
                          "alt": alt, "alt_type": "BARO", "speed": spd, "heading": hdg, "payload": payload})
    else:
        if takeoff is None:
            raise ValueError(f"{flight.start.name} start needs a takeoff airbase")
        kind = {StartMode.RUNWAY: ("TakeOff", "From Runway"),
                StartMode.RAMP_HOT: ("TakeOffParkingHot", "From Parking Area Hot"),
                StartMode.RAMP_COLD: ("TakeOffParking", "From Parking Area")}[flight.start]
        points.append({"x": takeoff.pos.x, "y": takeoff.pos.y, "alt": takeoff.elev_m, "alt_type": "BARO",
                       "speed": 0, "type": kind[0], "action": kind[1], "airdromeId": takeoff.airdrome_id})
        if flight.start is not StartMode.RUNWAY:
            if not takeoff.parking or len(takeoff.parking) < len(names):
                raise ValueError(f"need {len(names)} parking spots, got {len(takeoff.parking or [])}")
        for i, name in enumerate(names):
            u = {"name": name, "type": aircraft_type, "skill": SKILL[side], "x": takeoff.pos.x,
                 "y": takeoff.pos.y, "alt": takeoff.elev_m, "alt_type": "BARO", "speed": 0,
                 "heading": _heading(takeoff.pos, route[0].pos), "payload": payload}
            if flight.start is not StartMode.RUNWAY:
                term, spot = takeoff.parking[i]
                u.update(x=spot.x, y=spot.y, parking=term)
            units.append(u)

    points += [_point(wp, speed_ms, orbit_left_s if i == 0 else None) for i, wp in enumerate(route)]
    first = points[0]
    first["task"] = {"id": "ComboTask", "params": {"tasks": _first_tasks(flight) + [
        dict(t, number=t["number"] + 3) for t in (first.get("task") or {}).get("params", {}).get("tasks", [])]}}

    group = {"name": gname, "task": GROUP_TASK.get(flight.mission.upper(), "Nothing"),
             "route": {"points": points}, "units": units, "communication": True, "start_time": 0}
    if uncontrolled:
        if flight.start is not StartMode.RAMP_COLD:
            raise ValueError("only RAMP_COLD flights can be held uncontrolled")
        group["uncontrolled"] = True
    number = number if number is not None else flight_number_hint(flight.id)
    for i, u in enumerate(units, 1):
        u.update(unit_identity(side, number, i))
    return FlightSpawn({"category": "airplane", "country": COUNTRY_FOR_SIDE[side], "group": group}, names)
