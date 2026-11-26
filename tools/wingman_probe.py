"""Proof P20: cartridges built into the mission, and AI wingmen for Client slots. Host the mission
from your DCS (MULTIPLAYER > New server), as for P5.

    python3 -m tools.wingman_probe

The probe builds campaign-dev/campaign-p20.miz with two Client Hornet groups parked at Khalkhalah,
hot (engines running) unless --cold is given, as the campaign's player_start setting. Each Client
unit has its own cartridge in the .miz (DTC/), set to load itself:

  P20 Mixed   2-ship: #1 Client, #2 AI in the same group (the native wingman).
              Cartridge p20_mixed: 4 waypoints east of the field.
  P20 Solo    1-ship Client. Cartridge p20_solo: 6 waypoints south of the field.

Both groups' Mission Editor route is 2 waypoints north of the field, so the waypoint count on the
HSI shows which one the jet loaded: 2 = the route (cartridge not loaded), 4 or 6 = the cartridge.

In P20 Solo, F10 > Other > Campaign adds a separate AI Hornet group told to stay with you, the way
an empty seat in a player flight would be filled:

  Wingman parked, starts when you move   parked cold and switched off (uncontrolled) on the ramp,
                                         told to Follow you; when you start to taxi it is started,
                                         and starts up, taxis and takes off behind you. (Run 25: a
                                         wingman spawned ready on the ramp taxis out at once and holds
                                         on the runway for you, blocking it.)
  Wingman on the ramp, Follow     spawned on the ramp at Khalkhalah now, hot or cold like you
  Wingman on the ramp, Escort
  Wingman in the air, Follow      spawned 150 m behind and right of you, at your speed
  Wingman in the air, Escort
  Remove wingman

Follow only holds formation; Escort also engages enemy aircraft that threaten you (60 km).
Printed: births, takeoffs, landings and losses of P20 aircraft, and each wingman spawn.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
from pathlib import Path
from typing import Any

from campaign.bridge import Bridge, BridgeError, Connection
from campaign.dtc import DtcWaypoint, hornet_dtc
from campaign.flight_spawn import Takeoff, build_flight_spawn
from campaign.model import Flight, Pos, Side, StartMode, Waypoint
from campaign.protocol import Envelope, Hello, Src
from campaign.parking import SHELTER, size_from, stands_from, usable_spots
from tools.build_test_missions import AIRBASE_NAME, AIRBASE_XY, AIRDROME_ID, DEFAULT_TEMPLATE, SAVED_GAMES, build

HORNET = "FA-18C_hornet"
LOADOUT = "AIM-9M*2, AIM-7M*2, FUEL*2"
FT, NM = 0.3048, 1852.0
FIELD_ELEV_M = 700.0      # about Khalkhalah's elevation; the cartridge points only need to be near it
WINGMAN_GROUP = "P20 Wingman"
SOLO, MIXED = "P20 Solo", "P20 Mixed"
FOLLOW_POS = {"x": -150.0, "y": 0.0, "z": 150.0}   # behind (x<0) and right (z>0) of the lead, metres
ESCORT_DIST_M = 60_000
EVENT_KINDS = {"birth", "takeoff", "land", "unit_killed", "crash", "ejection", "engine_startup"}
MOVING_MS = 2.0          # the lead is taxiing once faster than this (about 4 kt)
WATCH_S = 2.0            # how often a parked wingman checks whether the lead is moving
MENU = [{"label": "Wingman parked, starts when you move", "key": "parked_follow"},
        {"label": "Wingman on the ramp, Follow", "key": "ramp_follow"},
        {"label": "Wingman on the ramp, Escort", "key": "ramp_escort"},
        {"label": "Wingman in the air, Follow", "key": "air_follow"},
        {"label": "Wingman in the air, Escort", "key": "air_escort"},
        {"label": "Remove wingman", "key": "remove"}]


def offset(nm_north: float, nm_east: float) -> Pos:
    return Pos(AIRBASE_XY[0] + nm_north * NM, AIRBASE_XY[1] + nm_east * NM)


def cartridge(name: str, points: list[Pos], label: str) -> dict:
    return hornet_dtc(name, "Syria", [DtcWaypoint(p, FIELD_ELEV_M, f"{label} {i}")
                                      for i, p in enumerate(points, 1)], route=1)


def client_group(name: str, units: list[dict[str, Any]], start: str) -> dict[str, Any]:
    route = [offset(15, 0), offset(30, 0)]
    return {"name": name, "airdrome_id": AIRDROME_ID, "x": AIRBASE_XY[0], "y": AIRBASE_XY[1], "start": start,
            "waypoints": [{"x": p.x, "y": p.y, "alt": 15_000 * FT} for p in route], "units": units}


def stay_task(kind: str, group_id: int) -> dict[str, Any]:
    params: dict[str, Any] = {"groupId": group_id, "pos": FOLLOW_POS, "lastWptIndexFlag": False}
    if kind == "escort":
        params.update(engagementDistMax=ESCORT_DIST_M, targetTypes=["Planes"])
    return {"number": 1, "auto": False, "enabled": True, "id": kind.capitalize(), "params": params}


class Probe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.mission: Connection | None = None
        self.hooks: Connection | None = None
        self.start = "cold" if args.cold else "hot"
        self.watch = 0     # bumped on every wingman change, so an old parked wingman's watcher stops

    def build_mission(self) -> None:
        mixed = cartridge("p20_mixed", [offset(5, 5 * i) for i in range(1, 5)], "MIXED")
        solo = cartridge("p20_solo", [offset(-5 * i, 0) for i in range(1, 7)], "SOLO")
        groups = [
            client_group(MIXED, [{"name": f"{MIXED}-1", "skill": "Client", "dtc": "p20_mixed"},
                                 {"name": f"{MIXED}-2", "skill": "High"}], self.start),
            client_group(SOLO, [{"name": f"{SOLO}-1", "skill": "Client", "dtc": "p20_solo"}], self.start),
        ]
        files = {f"DTC/{c['name']}.dtc": json.dumps(c, indent=4).encode() for c in (mixed, solo)}
        path = build(self.args.template, self.args.missions_dir, "campaign-p20.miz",
                     "lua/mission/campaign_bridge.lua", "", "Load campaign bridge", None, True, None,
                     {"client_groups": groups}, files)
        print(f"MISSION {path}, {self.start} start: {MIXED} (Client + AI, cartridge p20_mixed, 4 points), "
              f"{SOLO} (Client, cartridge p20_solo, 6 points); route in the .miz: 2 points", flush=True)

    def on_hello(self, conn: Connection, hello: Hello, new_run: bool) -> None:
        print(f"HELLO   {conn.src} new_run={new_run}", flush=True)
        if conn.src == Src.MISSION:
            self.mission = conn
            if new_run:
                asyncio.create_task(self.setup(conn))
        elif conn.src == Src.HOOKS:
            self.hooks = conn

    def on_disconnect(self, conn: Connection) -> None:
        print(f"DISCONNECT {conn.src}", flush=True)
        if conn is self.mission:
            self.mission = None
        if conn is self.hooks:
            self.hooks = None

    def on_message(self, conn: Connection, env: Envelope) -> None:
        if env.type not in ("event", "player"):
            return
        kind = env.data.get("kind")
        if kind == "menu_select":
            asyncio.create_task(self.wingman(conn, str(env.data.get("key"))))
            return
        unit = env.data.get("unit") or env.data.get("initiator") or {}
        name = str(unit.get("name") or "")
        if kind in EVENT_KINDS and name.startswith("P20"):
            who = f" ({unit.get('player')})" if unit.get("player") else ""
            print(f"EVENT   {kind} {name}{who}", flush=True)

    async def start_when_moving(self, conn: Connection, token: int) -> None:
        """Start the parked wingman once the lead starts to taxi."""
        try:
            while token == self.watch:
                lead = await conn.call("query", {"what": "group", "group": SOLO})
                if not lead.get("exists"):
                    return
                spd = float(lead["units"][0].get("spd") or 0.0)
                if spd > MOVING_MS:
                    if token != self.watch:
                        return
                    await conn.call("set_command", {"group": WINGMAN_GROUP, "command": {"id": "Start", "params": {}}})
                    await self.say(conn, f"You're moving ({spd / 0.5144:.0f} kt): wingman started.")
                    return
                await asyncio.sleep(WATCH_S)
        except (BridgeError, asyncio.TimeoutError, KeyError, TypeError, ValueError) as e:
            await self.say(conn, f"Wingman start failed: {e}")

    async def say(self, conn: Connection, text: str) -> None:
        print(f"SAY     {text}", flush=True)
        try:
            await conn.call("message", {"audience": "all", "text": text, "duration": 15})
        except (BridgeError, asyncio.TimeoutError):
            pass

    async def setup(self, conn: Connection) -> None:
        try:
            await conn.call("menu", {"audience": "all", "items": MENU})
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"menu failed: {e}", flush=True)
        await self.say(conn, "P20 probe. Take P20 Mixed or P20 Solo. Count the waypoints on the HSI: "
                             "2 = mission route, 4 (Mixed) or 6 (Solo) = the cartridge loaded itself.")

    async def wingman(self, conn: Connection, key: str) -> None:
        try:
            self.watch += 1
            await conn.call("despawn_group", {"group": WINGMAN_GROUP})
            if key == "remove":
                await self.say(conn, "Wingman removed.")
                return
            where, kind = key.split("_")
            lead = await conn.call("query", {"what": "group", "group": SOLO})
            if not lead.get("exists"):
                await self.say(conn, f"{SOLO} isn't in the game: take the slot first.")
                return
            data = await self.wingman_group(conn, where, lead)
            first = data["group"]["route"]["points"][0]["task"]["params"]["tasks"]
            for t in first:
                t["number"] += 1
            first.insert(0, stay_task(kind, int(lead["id"])))
            await conn.call("spawn_group", data)
            if where == "parked":
                asyncio.create_task(self.start_when_moving(conn, self.watch))
                await self.say(conn, "Wingman parked and switched off. It starts when you start to taxi.")
                return
            await self.say(conn, f"Wingman spawned {f'{self.start} on the ramp' if where == 'ramp' else 'beside you'}, "
                                 f"task {kind.capitalize()} on group {lead['id']}.")
        except (BridgeError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as e:
            await self.say(conn, f"Wingman failed: {e}")

    async def wingman_group(self, conn: Connection, where: str, lead: dict[str, Any]) -> dict[str, Any]:
        u = lead["units"][0]
        here = Pos(u["pos"]["x"], u["pos"]["y"])
        hdg = float(u.get("hdg") or 0.0)
        ahead = Pos(here.x + 20 * NM * math.cos(math.radians(hdg)), here.y + 20 * NM * math.sin(math.radians(hdg)))
        alt = max(float(u.get("alt") or 0.0), 3_000.0)
        wps = [Waypoint(ahead, alt), Waypoint(offset(0, 0), alt, action="LAND", params={"airdrome_id": AIRDROME_ID})]
        if where == "air":
            back = math.radians(hdg)
            pos = Pos(here.x - 150 * math.cos(back) - 150 * math.sin(back),
                      here.y - 150 * math.sin(back) + 150 * math.cos(back))
            f = Flight(id="p20wing", package_id="p20", squadron_id="p20", callsign="Wingman", mission="CAS",
                       aircraft=1, loadout=LOADOUT, takeoff_time=0, start=StartMode.AIR, waypoints=wps,
                       pos=pos, alt_m=float(u.get("alt") or alt), hdg=hdg, speed_ms=max(float(u.get("spd") or 0), 120.0))
            data = build_flight_spawn(f, HORNET, Side.BLUE).data
        else:
            ab = await conn.call("query", {"what": "airbase", "airbase": AIRBASE_NAME})
            spots = await conn.call("query", {"what": "parking", "airbase": AIRBASE_NAME, "free_only": True})
            # Run 25: the first free spot was a hardened shelter too small for a Hornet (wing through
            # the wall): it spawned damaged, aborted and ejected. Only spots it fits, nearest the lead.
            stands, why = None, "no Hooks connection"
            if self.hooks is not None:
                try:
                    stands = stands_from(await self.hooks.call("stands", {"airdrome_id": int(ab["id"])}))
                except (BridgeError, asyncio.TimeoutError) as e:
                    why = str(e)
            size = size_from(await conn.call("query", {"what": "type_size", "type": HORNET}))
            all_spots = spots.get("spots") or []
            fits = usable_spots(all_spots, stands, size)

            def dist(s: dict[str, Any]) -> float:
                return math.hypot(s["pos"]["x"] - here.x, s["pos"]["y"] - here.y)
            fits.sort(key=dist)
            if not fits:
                raise ValueError(f"no free parking at {AIRBASE_NAME} that a Hornet fits")
            spot = fits[0]
            shelters = sum(1 for s in all_spots if s.get("type") == SHELTER)
            print(f"PARKING Hornet {size.span:.1f} m span, {size.length:.1f} m long, {size.height:.1f} m high; "
                  + (f"{len(stands)} stands with sizes" if stands is not None else f"stand sizes unknown ({why}), no shelters")
                  + f"; {len(fits)} of {len(all_spots)} free spots fit ({shelters} free shelters); wingman spot "
                  f"term {spot['term']}, type {spot['type']}, {dist(spot):.0f} m from you", flush=True)
            f = Flight(id="p20wing", package_id="p20", squadron_id="p20", callsign="Wingman", mission="CAS",
                       aircraft=1, loadout=LOADOUT, takeoff_time=0, waypoints=wps,
                       start=StartMode.RAMP_HOT if self.start == "hot" and where == "ramp" else StartMode.RAMP_COLD)
            takeoff = Takeoff(int(ab["id"]), Pos(ab["pos"]["x"], ab["pos"]["y"]), float(ab["alt"]),
                              [(int(spot["term"]), Pos(spot["pos"]["x"], spot["pos"]["y"]))])
            data = build_flight_spawn(f, HORNET, Side.BLUE, takeoff=takeoff, uncontrolled=where == "parked").data
        data["group"]["name"] = WINGMAN_GROUP
        data["group"]["units"][0]["name"] = f"{WINGMAN_GROUP}-1"
        return data


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    ap.add_argument("--missions-dir", type=Path, default=SAVED_GAMES / "Missions" / "campaign-dev")
    ap.add_argument("--port", type=int, default=7341)
    ap.add_argument("--cold", action="store_true", help="park the Client groups cold (default: hot, engines running)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    probe = Probe(args)
    probe.build_mission()
    bridge = Bridge(probe.on_message, probe.on_hello, probe.on_disconnect, port=args.port)
    await bridge.start()
    print(f"listening on 127.0.0.1:{bridge.port}; host campaign-dev/campaign-p20.miz (MULTIPLAYER > New server)",
          flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
