"""In-game test of AI flights: air and ramp starts, tasking, takeoff/landing, losses, warehouse
stock, and taking an airborne flight out of DCS and putting it back.

    python3 -m tools.air_demo

Then start build/campaign-smoke.miz (only one demo can listen on the port). Everything is driven
from F10 > Other > Campaign. Distances are nm, altitudes ft, bearings magnetic.
See docs/testing-in-dcs.md, Run 5.

The campaign (squadron stock, flights, losses) is saved to build/saves/air.json and loaded at
startup. Restarting this script while the mission runs carries on with the flights in DCS;
--new starts over.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
import math
from typing import Any

from campaign.air import AirSpawner
from campaign.bridge import Bridge, BridgeError, Connection
from campaign.flight_spawn import Takeoff
from campaign.ground_spawn import build_ground_spawn
from campaign.model import (CampaignState, Flight, FlightState, GroundOrder, GroundRole, GroundUnit, Pos, Side,
                            SpawnState, Squadron, StartMode, Waypoint)
from campaign.persist import Snapshot
from campaign.protocol import PREFIX_FLIGHT, Envelope, Hello, Src, as_list, parse_dcs_name
from tools import demo_save

M_PER_NM = 1852.0
M_PER_FT = 0.3048
FIGHTER_SPOTS = (68, 104, 72)  # DCS parking Term_Type: shelter, open, large; not runway (16) or helo (40)

RED_CAP_LOADOUT = "R-73*4,R-27R*2,Fuel-1500"
BLUE_CAP_LOADOUT = "AIM-9X*2, AIM-120C-5*6, FUEL*3"
BLUE_CAS_LOADOUT = "AIM-9X*2, AIM-120C-5*1, AGM-65D*4, ATFLIR, FUEL"
TARGET_NM_NORTH = 12


def nm(n: float) -> float:
    return n * M_PER_NM


def ft(n: float) -> float:
    return n * M_PER_FT


class Demo:
    def __init__(self, airbase: str, magvar: float, snap: Snapshot | None = None) -> None:
        self.airbase = airbase
        self.magvar = magvar
        if snap:
            self.state = snap.state
        else:
            self._new_campaign()
        self.conn: Connection | None = None
        self.air = AirSpawner(self.state, self._call, notify=self._notify)
        if snap:
            self.air.restore(snap.extra.get("air") or {})
        self.ab: dict | None = None          # airbase query result
        self.players: list[dict] = []
        used = [int(f.id.lstrip("abcdefghijklmnopqrstuvwxyz") or 0) for f in self.state.flights.values()]
        self.ids = itertools.count(max(used, default=0) + 1)
        self.tasks: set[asyncio.Task[Any]] = set()
        self.lock = asyncio.Lock()
        self.quiet = False

    def _new_campaign(self) -> None:
        self.state = CampaignState(theater_map="", clock=0.0, sides={})
        self.state.squadrons["red"] = Squadron(id="red", side=Side.RED, name="Red MiG-29s", aircraft_type="MiG-29A",
                                               base_id="offmap", roles={"CAP"}, aircraft_ready=8, aircraft_damaged=0,
                                               pilots=8, player_flyable=False)
        self.state.squadrons["blue"] = Squadron(id="blue", side=Side.BLUE, name="Blue Hornets",
                                                aircraft_type="FA-18C_hornet", base_id=self.airbase, roles={"CAP", "CAS"},
                                                aircraft_ready=8, aircraft_damaged=0, pilots=8, player_flyable=False)

    # ---------- plumbing ----------

    async def _call(self, op: str, data: dict[str, Any]) -> Any:
        if self.conn is None or self.conn.closed:
            raise BridgeError("mission not connected")
        return await self.conn.call(op, data)

    def _bg(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def say(self, text: str, duration: int = 12) -> None:
        print(f"MSG     {text}", flush=True)
        if self.conn is not None and not self.conn.closed:
            self._bg(self._say(text, duration))

    async def _say(self, text: str, duration: int) -> None:
        try:
            await self.conn.call("message", {"audience": "all", "text": text, "duration": duration})
        except (BridgeError, asyncio.TimeoutError):
            pass

    def _notify(self, f: Flight, what: str) -> None:
        if self.quiet:
            return
        sq = self.state.squadrons[f.squadron_id]
        self.say(f"{f.callsign} ({f.aircraft}x {sq.aircraft_type}, {f.mission}): {what}")
        if sq.side is Side.BLUE and any(w in what for w in ("spawned", "held", "back to the squadron", "took off")):
            self._bg(self.report_warehouse(f"after {f.callsign} {what.split(':')[0]}"))

    # ---------- bridge callbacks ----------

    async def on_hello(self, conn: Connection, hello: Hello, new_run: bool) -> None:
        print(f"HELLO {conn.src} run={hello.run_id} new_run={new_run} spawned={hello.spawned}", flush=True)
        if conn.src != Src.MISSION:
            return
        self.conn = conn
        self.quiet = True
        try:
            self.air.on_hello(hello, new_run)
        finally:
            self.quiet = False
        self.ab = await conn.call("query", {"what": "airbase", "airbase": self.airbase})
        if not self.ab or not self.ab.get("exists"):
            print(f"airbase {self.airbase!r} not found; pass --airbase", flush=True)
            return
        if new_run:
            await self.spawn_target()
            await conn.call("menu", {"audience": "all", "items": [
                {"label": "Spawn", "items": [
                    {"label": "Red CAP: 2x MiG-29A, air start 40 nm N", "key": "red_cap"},
                    {"label": "Blue CAP: 2x F/A-18C, air start overhead", "key": "blue_cap"},
                    {"label": "Blue CAS: 2x F/A-18C, hot ramp start", "key": "blue_cas"},
                    {"label": "Blue cold start, held on the ramp", "key": "blue_cold"},
                ]},
                {"label": "Control", "items": [
                    {"label": "Start the held flight", "key": "start_held"},
                    {"label": "Take the red CAP out of DCS", "key": "aggregate_red"},
                    {"label": "Put the red CAP back", "key": "respawn_red"},
                ]},
                {"label": "Warehouse", "items": [
                    {"label": "Hornet count at the field", "key": "wh_count"},
                    {"label": "Stock 4 Hornets at the field", "key": "wh_stock"},
                ]},
                {"label": "Status", "key": "status"},
            ]})
            self.say("Air demo connected. F10 > Other > Campaign: Spawn, Control, Warehouse, Status. "
                     f"A red target group (4 BTR) is {TARGET_NM_NORTH} nm north of {self.airbase}.", duration=20)

    def on_disconnect(self, conn: Connection) -> None:
        print(f"DISCONNECT {conn.src}", flush=True)

    def on_message(self, conn: Connection, env: Envelope) -> None:
        if env.type == "telemetry":
            self.players = [p for p in as_list(env.data.get("players")) if isinstance(p, dict)]
            exits = self.air.on_telemetry(env.data)
            self._bg(self.tick(exits))
        elif env.type == "event":
            kind = env.data.get("kind")
            unit = env.data.get("unit") or env.data.get("victim") or {}
            parsed = parse_dcs_name(unit.get("name"))
            if kind in ("takeoff", "land", "engine_shutdown", "unit_killed", "kill", "birth", "ejection") and parsed:
                print(f"event   {kind:15} {unit.get('name')} ({unit.get('type')}) {env.data.get('place') or ''}",
                      flush=True)
            if kind == "unit_killed" and parsed and parsed.prefix != PREFIX_FLIGHT:
                self.say(f"Target group lost a {unit.get('type')}")
            self.air.on_event(env.data)
            if kind == "menu_select":
                self._bg(self.menu(str(env.data.get("key", ""))))

    async def tick(self, exits: list[Flight]) -> None:
        if self.lock.locked():
            return
        async with self.lock:
            await self.air.tick(exits)

    # ---------- scenarios ----------

    def _field(self) -> tuple[Pos, float]:
        c = self.ab["pos"]
        return Pos(c["x"], c["y"]), float(self.ab.get("alt") or 0.0)

    def _flight(self, sq: str, mission: str, callsign: str, loadout: str, waypoints: list[Waypoint],
                start: StartMode) -> Flight:
        fid = f"{sq}{next(self.ids)}"
        f = Flight(id=fid, package_id="demo", squadron_id=sq, callsign=callsign, mission=mission, aircraft=2,
                   loadout=loadout, waypoints=waypoints, takeoff_time=0, start=start)
        self.state.flights[fid] = f
        return f

    async def _takeoff(self, n: int) -> Takeoff:
        field, elev = self._field()
        res = await self._call("query", {"what": "parking", "airbase": self.airbase, "free_only": True})
        spots = [s for s in as_list(res.get("spots")) if s.get("type") in FIGHTER_SPOTS]
        spots.sort(key=lambda s: (FIGHTER_SPOTS.index(s["type"]), s.get("dist_rw") or 0))
        print(f"parking: {len(spots)} free fighter spots; using "
              f"{[(s['term'], s['type']) for s in spots[:n]]}", flush=True)
        if len(spots) < n:
            raise ValueError(f"only {len(spots)} free fighter parking spots at {self.airbase}")
        return Takeoff(airdrome_id=int(self.ab["id"]), pos=field, elev_m=elev,
                       parking=[(int(s["term"]), Pos(s["pos"]["x"], s["pos"]["y"])) for s in spots[:n]])

    def _land(self) -> Waypoint:
        field, elev = self._field()
        return Waypoint(field, elev, action="LAND", params={"airdrome_id": int(self.ab["id"])})

    async def red_cap(self) -> None:
        field, _ = self._field()
        north = lambda d: Pos(field.x + nm(d), field.y)  # noqa: E731
        f = self._flight("red", "CAP", "Red CAP", RED_CAP_LOADOUT, [
            Waypoint(north(40), ft(20_000)),
            Waypoint(north(20), ft(20_000), action="ORBIT", params={"duration_s": 900}),
            Waypoint(north(80), ft(25_000), action="EXIT"),
        ], StartMode.AIR)
        await self.air.spawn(f)

    async def blue_cap(self) -> None:
        field, _ = self._field()
        f = self._flight("blue", "CAP", "Blue CAP", BLUE_CAP_LOADOUT, [
            Waypoint(Pos(field.x + nm(3), field.y), ft(18_000)),
            Waypoint(Pos(field.x + nm(10), field.y), ft(18_000), action="ORBIT", params={"duration_s": 900}),
            self._land(),
        ], StartMode.AIR)
        await self.air.spawn(f)

    async def blue_cas(self) -> None:
        field, _ = self._field()
        target = Pos(field.x + nm(TARGET_NM_NORTH), field.y)
        f = self._flight("blue", "CAS", "Blue CAS", BLUE_CAS_LOADOUT, [
            Waypoint(Pos(field.x + nm(5), field.y), ft(12_000)),
            Waypoint(target, ft(12_000), action="CAS", params={"radius_m": nm(4)}),
            Waypoint(Pos(field.x + nm(5), field.y + nm(3)), ft(10_000)),
            self._land(),
        ], StartMode.RAMP_HOT)
        await self.air.spawn(f, takeoff=await self._takeoff(f.aircraft))

    async def blue_cold(self) -> None:
        field, _ = self._field()
        f = self._flight("blue", "CAP", "Blue Cold", BLUE_CAP_LOADOUT, [
            Waypoint(Pos(field.x + nm(5), field.y - nm(5)), ft(15_000), action="ORBIT", params={"duration_s": 300}),
            self._land(),
        ], StartMode.RAMP_COLD)
        await self.air.spawn(f, takeoff=await self._takeoff(f.aircraft), uncontrolled=True)

    def _latest(self, sq: str, spawned: bool) -> Flight | None:
        for f in reversed(list(self.state.flights.values())):
            if f.squadron_id == sq and (f.spawn is SpawnState.SPAWNED) == spawned \
                    and f.state not in (FlightState.LOST, FlightState.LANDED, FlightState.PLANNED):
                return f
        return None

    async def menu(self, key: str) -> None:
        try:
            if key in ("red_cap", "blue_cap", "blue_cas", "blue_cold"):
                await getattr(self, key)()
            elif key == "start_held":
                f = next((f for f in self.state.flights.values() if f.spawn is SpawnState.SPAWNED
                          and self.air.live[f.id].started_at is None), None)
                if f is None:
                    self.say("No flight is held on the ramp.")
                else:
                    await self.air.start(f)
            elif key == "aggregate_red":
                f = self._latest("red", spawned=True)
                if f is None:
                    self.say("No red CAP is in DCS.")
                else:
                    await self.air.aggregate(f)
                    self.say(f"{f.callsign} is now data only: {self.describe(f)}")
            elif key == "respawn_red":
                f = self._latest("red", spawned=False)
                if f is None:
                    self.say("No aggregated red CAP to put back.")
                else:
                    await self.air.spawn(f)
            elif key == "wh_count":
                await self.report_warehouse("now")
            elif key == "wh_stock":
                await self._call("warehouse_set", {"airbase": self.airbase, "aircraft": {"FA-18C_hornet": 4}})
                await self.report_warehouse("after stocking 4")
            elif key == "status":
                self.say(self.status_text(), duration=30)
        except Exception as e:  # noqa: BLE001 - show the failure in game; that's the point of the test
            self.say(f"{key} failed: {e}", duration=20)

    async def report_warehouse(self, when: str) -> None:
        try:
            res = await self._call("query", {"what": "warehouse", "airbase": self.airbase, "types": ["FA-18C_hornet"]})
            n = (res.get("counts") or {}).get("FA-18C_hornet")
            self.say(f"Warehouse {when}: {n} F/A-18C at {self.airbase}")
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"warehouse query failed: {e}", flush=True)

    async def spawn_target(self) -> None:
        field, _ = self._field()
        target = GroundUnit(id="target", side=Side.RED, name="target", role=GroundRole.MECH,
                            composition={"BTR-80": 4}, pos=Pos(field.x + nm(TARGET_NM_NORTH), field.y),
                            location_id=None, order=GroundOrder.HOLD)
        try:
            await self._call("spawn_group", build_ground_spawn(target).data)
        except Exception as e:  # noqa: BLE001 - "already exists" after a reconnect is fine
            print(f"target spawn: {e}", flush=True)

    # ---------- status ----------

    def describe(self, f: Flight) -> str:
        parts = []
        if f.pos is not None and self.players:
            me = self.players[0].get("pos") or {}
            if "x" in me:
                dx, dy = f.pos.x - me["x"], f.pos.y - me["y"]
                brg = (math.degrees(math.atan2(dy, dx)) - self.magvar) % 360
                parts.append(f"{brg:03.0f}M {math.hypot(dx, dy) / M_PER_NM:.0f} nm")
        if f.alt_m is not None:
            parts.append(f"{f.alt_m / M_PER_FT:,.0f} ft")
        parts.append(f"fuel {f.fuel:.0%}")
        return ", ".join(parts)

    def status_text(self) -> str:
        lines = ["AIR STATUS"]
        for f in self.state.flights.values():
            where = "in DCS" if f.spawn is SpawnState.SPAWNED else "data only"
            lines.append(f"{f.callsign} [{f.id}]: {f.aircraft}x, {f.state.name}, {where} | {self.describe(f)}")
        for sq in self.state.squadrons.values():
            lines.append(f"{sq.name}: {sq.aircraft_ready} ready")
        return "\n".join(lines)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--airbase", default="Khalkhalah")
    ap.add_argument("--magvar", type=float, default=5.0, help="magnetic variation, degrees east")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7341)
    demo_save.add_args(ap, "air")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    saver = demo_save.DemoSave(args.save, fresh=args.new)
    snap = saver.load()
    demo = Demo(args.airbase, args.magvar, snap)
    if snap:
        print(demo.status_text(), flush=True)
    bridge = Bridge(demo.on_message, demo.on_hello, demo.on_disconnect, host=args.host, port=args.port)
    saver.restore_runs(bridge)
    await bridge.start()
    print(f"listening on {args.host}:{bridge.port}; waiting for DCS", flush=True)
    async with saver.running(bridge, lambda: (demo.state, {"air": demo.air.export()})):
        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
