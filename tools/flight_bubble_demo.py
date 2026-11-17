"""In-game test of the flight bubble: AI flights exist in DCS only near players, and fly their
routes in the engine everywhere else.

    python3 -m tools.flight_bubble_demo [--air-spawn 30] [--air-despawn 40] [--new] [--ghost ...]

``--ghost`` adds a ghost player (see tools/ghosts.py); ``--ghost far`` puts one on Blue Ferry's base.

Then start build/campaign-smoke.miz. The scenario starts on the first connection; times are on
the campaign clock, which runs with DCS model time (time acceleration works):

- Red Ferry: 2 unarmed MiG-29A from off the map, 60 nm north, orbit 35 nm north for 15 min,
  then leave the map to the north. Weapons hold.
- Blue Ferry: 2 clean F/A-18C from an airbase about 100 nm away, taking off at +1 min, landing at
  Khalkhalah. Nobody is near their base, so they take off without DCS.
- Blue CAS: 2 F/A-18C, hot ramp start at Khalkhalah at +5 min, attack the BTRs 12 nm north,
  land again. Starts in DCS only if a player is within the spawn distance of the field.

The F10 map shows a marker for every flight where the engine has it: "[DCS]" when it is in DCS,
"[engine]" when only the engine is flying it. F10 > Other > Campaign > Status lists them with
magnetic bearing and range from you. Saved to build/saves/flight_bubble.json.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
from pathlib import Path
from typing import Any

from campaign.air import AirSpawner
from campaign.air_bubble import AirBubble
from campaign.bridge import Bridge, BridgeError, Connection
from campaign.bubble import NM, BubbleConfig
from campaign.dtc import DtcWaypoint, hornet_dtc, write_dtc
from campaign.flight_spawn import Takeoff
from campaign.ground_spawn import build_ground_spawn
from campaign.model import (CampaignState, Flight, FlightState, GroundOrder, GroundRole, GroundUnit, Pos, Side,
                            SpawnState, Squadron, StartMode, Waypoint)
from campaign.persist import Snapshot
from campaign.protocol import PREFIX_GROUND, Envelope, Hello, Src, as_list, parse_dcs_name
from campaign.settings import SettingsError, bubble_config, load_bubble_config
from tools import demo_save, ghosts
from tools.build_test_missions import AIRBASE_XY

M_PER_FT = 0.3048
HOME = "Khalkhalah"
# Candidates for the far base; the one closest to FAR_NM from HOME is used.
FAR_CANDIDATES = ["Damascus", "Mezzeh", "Marj Ruhayyil", "Rayak", "Al Qusayr", "Shayrat", "Tiyas", "Palmyra",
                  "King Hussein Air College", "Mafraq", "Prince Hassan", "H4", "Ramat David", "Haifa",
                  "Beirut-Rafic Hariri", "Kiryat Shmona", "Rosh Pina"]
FAR_NM = 100
TARGET_NM_NORTH = 12
RED_ORBIT_NM_NORTH = 35
# Cartridge test points: (label, nm north of HOME). Red Ferry's orbit is WP2, so the HSI shows
# the range that decides when it spawns (< spawn distance) and leaves DCS (> despawn distance).
TEST_POINTS = [("8 nm N: watch point", 8), ("Red Ferry orbit", RED_ORBIT_NM_NORTH), (HOME, 0), ("10 nm S: hold", -10)]
CAS_LOADOUT = "AIM-9X*2, AIM-120C-5*1, AGM-65D*4, ATFLIR, FUEL"
FIGHTER_SPOTS = (68, 104, 72)
SIDE_COLOR = {Side.RED: [1, 0.2, 0.2, 1], Side.BLUE: [0.3, 0.55, 1, 1]}
MARK_EVERY_S = 10.0


def nm(n: float) -> float:
    return n * NM


def ft(n: float) -> float:
    return n * M_PER_FT


class Demo:
    def __init__(self, cfg: BubbleConfig, magvar: float, snap: Snapshot | None,
                 ghost_specs: list[str] = ()) -> None:
        self.cfg = cfg
        self.ghosts = ghosts.Ghosts(list(ghost_specs), magvar)
        self.magvar = magvar
        self.state = snap.state if snap else CampaignState(theater_map="Syria", clock=0.0, sides={})
        self.conn: Connection | None = None
        self.air = AirSpawner(self.state, self._call, notify=self._notify)
        self.bubble = AirBubble(self.state, self.air, cfg, base_pos=self.base_pos, takeoff_for=self.takeoff_for)
        if snap:
            self.air.restore(snap.extra.get("air") or {})
            self.bubble.restore(snap.extra.get("bubble") or {})
        self.bases: dict[str, dict] = {}   # airbase name -> query result
        self.players: list[dict] = []
        self.tasks: set[asyncio.Task[Any]] = set()
        self.lock = asyncio.Lock()
        self.quiet = False
        self.marks: dict[str, int] = {}
        self.drawn: set[str] = set()
        self.last_marks = -math.inf

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
        print(f"MSG     [{self.clock_text()}] {text}", flush=True)
        if self.conn is not None and not self.conn.closed:
            self._bg(self._quiet_call("message", {"audience": "all", "text": text, "duration": duration}))

    async def _quiet_call(self, op: str, data: dict[str, Any]) -> None:
        try:
            await self._call(op, data)
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"{op} failed: {e}", flush=True)

    def _notify(self, f: Flight, what: str) -> None:
        if not self.quiet:
            self.say(f"{f.callsign}: {what}")

    def clock_text(self) -> str:
        s = int(self.state.clock)
        return f"T+{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"

    # ---------- bases ----------

    def base_pos(self, sq: Squadron) -> Pos | None:
        ab = self.bases.get(sq.base_id)
        return Pos(ab["pos"]["x"], ab["pos"]["y"]) if ab else None

    async def takeoff_for(self, f: Flight) -> Takeoff:
        name = self.state.squadrons[f.squadron_id].base_id
        ab = self.bases[name]
        res = await self._call("query", {"what": "parking", "airbase": name, "free_only": True})
        spots = [s for s in as_list(res.get("spots")) if s.get("type") in FIGHTER_SPOTS]
        spots.sort(key=lambda s: (FIGHTER_SPOTS.index(s["type"]), s.get("dist_rw") or 0))
        if len(spots) < f.aircraft:
            raise ValueError(f"only {len(spots)} free fighter parking spots at {name}")
        return Takeoff(airdrome_id=int(ab["id"]), pos=Pos(ab["pos"]["x"], ab["pos"]["y"]),
                       elev_m=float(ab.get("alt") or 0.0),
                       parking=[(int(s["term"]), Pos(s["pos"]["x"], s["pos"]["y"])) for s in spots[:f.aircraft]])

    async def find_bases(self) -> bool:
        for name in [HOME] + FAR_CANDIDATES:
            if name in self.bases:
                continue
            ab = await self._call("query", {"what": "airbase", "airbase": name})
            if ab and ab.get("exists"):
                self.bases[name] = ab
        return HOME in self.bases

    def far_base(self) -> str | None:
        home = self.bases[HOME]["pos"]
        far = [(abs(math.dist((home["x"], home["y"]), (ab["pos"]["x"], ab["pos"]["y"])) / NM - FAR_NM), name)
               for name, ab in self.bases.items() if name != HOME]
        return min(far)[1] if far else None

    # ---------- scenario ----------

    def create_scenario(self) -> None:
        home = self.base_pos_name(HOME)
        hid = int(self.bases[HOME]["id"])
        land_home = Waypoint(home, float(self.bases[HOME].get("alt") or 0), action="LAND", params={"airdrome_id": hid})
        t0 = self.state.clock
        far = self.far_base()
        sqs = [Squadron(id="red", side=Side.RED, name="Red MiG-29s (off map)", aircraft_type="MiG-29A",
                        base_id="offmap", roles={"FERRY"}, aircraft_ready=8, aircraft_damaged=0, pilots=8,
                        player_flyable=False),
               Squadron(id="blue_home", side=Side.BLUE, name=f"Blue Hornets ({HOME})", aircraft_type="FA-18C_hornet",
                        base_id=HOME, roles={"CAS"}, aircraft_ready=8, aircraft_damaged=0, pilots=8,
                        player_flyable=False)]
        if far:
            sqs.append(Squadron(id="blue_far", side=Side.BLUE, name=f"Blue Hornets ({far})",
                                aircraft_type="FA-18C_hornet", base_id=far, roles={"FERRY"}, aircraft_ready=8,
                                aircraft_damaged=0, pilots=8, player_flyable=False))
        for sq in sqs:
            self.state.squadrons[sq.id] = sq
        north = lambda d: Pos(home.x + nm(d), home.y)  # noqa: E731
        flights = [
            Flight(id="redferry", package_id="demo", squadron_id="red", callsign="Red Ferry", mission="FERRY",
                   aircraft=2, loadout="", takeoff_time=t0, start=StartMode.AIR, waypoints=[
                       Waypoint(north(60), ft(20_000)),
                       Waypoint(north(RED_ORBIT_NM_NORTH), ft(20_000), action="ORBIT", params={"duration_s": 900}),
                       Waypoint(north(90), ft(25_000), action="EXIT")]),
            Flight(id="blucas", package_id="demo", squadron_id="blue_home", callsign="Blue CAS", mission="CAS",
                   aircraft=2, loadout=CAS_LOADOUT, takeoff_time=t0 + 300, start=StartMode.RAMP_HOT, waypoints=[
                       Waypoint(north(5), ft(12_000)),
                       Waypoint(north(TARGET_NM_NORTH), ft(12_000), action="CAS", params={"radius_m": nm(4)}),
                       Waypoint(Pos(home.x + nm(5), home.y + nm(3)), ft(10_000)),
                       land_home]),
        ]
        if far:
            fp = self.base_pos_name(far)
            d = math.dist((fp.x, fp.y), (home.x, home.y))
            toward = lambda k: Pos(fp.x + (home.x - fp.x) * k, fp.y + (home.y - fp.y) * k)  # noqa: E731
            flights.append(Flight(
                id="bluferry", package_id="demo", squadron_id="blue_far", callsign="Blue Ferry", mission="FERRY",
                aircraft=2, loadout="", takeoff_time=t0 + 60, start=StartMode.RAMP_HOT, waypoints=[
                    Waypoint(toward(nm(5) / d), ft(20_000)),
                    Waypoint(toward(1 - nm(15) / d), ft(12_000)),
                    land_home]))
        for f in flights:
            self.state.flights[f.id] = f
        print(f"scenario: far base {far} ({self.dist_nm(far):.0f} nm from {HOME})" if far
              else "scenario: no far base found; Blue Ferry skipped", flush=True)

    def base_pos_name(self, name: str) -> Pos:
        p = self.bases[name]["pos"]
        return Pos(p["x"], p["y"])

    def dist_nm(self, name: str) -> float:
        a, b = self.base_pos_name(HOME), self.base_pos_name(name)
        return math.dist((a.x, a.y), (b.x, b.y)) / NM

    async def spawn_target(self) -> None:
        home = self.base_pos_name(HOME)
        target = GroundUnit(id="target", side=Side.RED, name="target", role=GroundRole.MECH,
                            composition={"BTR-80": 4}, pos=Pos(home.x + nm(TARGET_NM_NORTH), home.y),
                            location_id=None, order=GroundOrder.HOLD)
        try:
            await self._call("spawn_group", build_ground_spawn(target).data)
        except Exception as e:  # noqa: BLE001 - "already exists" after a reconnect is fine
            print(f"target spawn: {e}", flush=True)

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
        if new_run:
            self.bubble.on_new_run()
            self.drawn.clear()  # the old drawings went with the old mission
        if not await self.find_bases():
            print(f"airbase {HOME} not found", flush=True)
            return
        if self.ghosts:
            for g in self.ghosts.ghosts:  # "far": Blue Ferry's base, chosen at runtime
                if g.pos is None and g.spec.lower() == "far":
                    g.spec = self.far_base() or g.spec
            try:
                for line in await self.ghosts.setup(conn.call):
                    print(line, flush=True)
            except BridgeError as e:
                print(f"ghosts: {e}", flush=True)
        if not self.state.flights:
            self.create_scenario()
        if new_run:
            await self.spawn_target()
            await self._quiet_call("warehouse_set", {"airbase": HOME, "aircraft": {"FA-18C_hornet": 4}})
            await self._call("menu", {"audience": "all", "items": [{"label": "Status", "key": "status"}]})
            self.say("Flight bubble demo connected. F10 map markers show every flight: [DCS] = in DCS, "
                     "[engine] = flown by the engine only. F10 > Other > Campaign > Status for details.",
                     duration=20)
        print(self.status_text(), flush=True)

    def on_disconnect(self, conn: Connection) -> None:
        print(f"DISCONNECT {conn.src}", flush=True)

    def on_message(self, conn: Connection, env: Envelope) -> None:
        if env.type == "telemetry":
            self.players = [p for p in as_list(env.data.get("players")) if isinstance(p, dict)]
            exits = self.air.on_telemetry(env.data)
            self.bubble.on_telemetry(env.t, self.ghosts.inject(env.data))
            self._bg(self.tick(exits))
        elif env.type == "event":
            kind = env.data.get("kind")
            unit = env.data.get("unit") or env.data.get("victim") or {}
            parsed = parse_dcs_name(unit.get("name"))
            if kind in ("takeoff", "land", "engine_shutdown", "unit_killed", "birth") and parsed:
                print(f"event   [{self.clock_text()}] {kind:15} {unit.get('name')} ({unit.get('type')}) "
                      f"{env.data.get('place') or ''}", flush=True)
            if kind == "unit_killed" and parsed and parsed.prefix == PREFIX_GROUND:
                self.say(f"Target group lost a {unit.get('type')}")
            self.air.on_event(env.data)
            self.bubble.on_event(env.data)
            if kind == "menu_select" and env.data.get("key") == "status":
                self.say(self.status_text(), duration=30)

    async def tick(self, exits: list[Flight]) -> None:
        if self.lock.locked():
            return
        async with self.lock:
            await self.air.tick(exits)
            plan = await self.bubble.tick()
            if plan.spawn or plan.aggregate or plan.launch_dcs or plan.launch_virtual:
                print(self.status_text(), flush=True)
            if self.state.clock - self.last_marks >= MARK_EVERY_S:
                self.last_marks = self.state.clock
                await self.draw_marks()

    # ---------- display ----------

    def bra(self, pos: Pos | None) -> str:
        if pos is None or not self.players or "x" not in (self.players[0].get("pos") or {}):
            return "?"
        me = self.players[0]["pos"]
        dx, dy = pos.x - me["x"], pos.y - me["y"]
        return f"{(math.degrees(math.atan2(dy, dx)) - self.magvar) % 360:03.0f}M {math.hypot(dx, dy) / NM:.0f} nm"

    def where(self, f: Flight) -> str:
        if f.state is FlightState.PLANNED:
            return f"takes off at T+{int(f.takeoff_time) // 60} min"
        if f.state in (FlightState.LANDED, FlightState.LOST):
            return f.state.name.lower()
        alt = f" {f.alt_m / M_PER_FT:,.0f} ft" if f.alt_m is not None else ""
        orbit = (f", orbiting {int(f.hold_until - self.state.clock) // 60} min more"
                 if f.hold_until is not None else "")
        return f"{'[DCS]' if f.spawn is SpawnState.SPAWNED else '[engine]'} {self.bra(f.pos)}{alt}{orbit}"

    def status_text(self) -> str:
        lines = [f"FLIGHTS at {self.clock_text()}: spawn within {self.cfg.air_spawn / NM:.0f} nm, "
                 f"leave DCS beyond {self.cfg.air_despawn / NM:.0f} nm"]
        if HOME in self.bases:
            lines.append(f"{HOME}: {self.bra(self.base_pos_name(HOME))}")
        for f in self.state.flights.values():
            lines.append(f"{f.callsign}: {f.aircraft}x {f.state.name} {self.where(f)}")
        for sq in self.state.squadrons.values():
            lines.append(f"{sq.name}: {sq.aircraft_ready} ready")
        return "\n".join(lines)

    async def draw_marks(self) -> None:
        for i, f in enumerate(self.state.flights.values()):
            did = self.marks.setdefault(f.id, 3000 + i)
            sq = self.state.squadrons[f.squadron_id]
            if f.pos is None or f.state in (FlightState.PLANNED, FlightState.LANDED, FlightState.LOST):
                if f.id in self.drawn:
                    self.drawn.discard(f.id)
                    await self._quiet_call("undraw", {"id": did})
                continue
            self.drawn.add(f.id)
            tag = "DCS" if f.spawn is SpawnState.SPAWNED else "engine"
            alt = f" {f.alt_m / M_PER_FT / 1000:.0f}k ft" if f.alt_m is not None else ""
            await self._quiet_call("draw", {"id": did, "audience": "all", "shape": "text",
                                            "pos": {"x": f.pos.x, "y": f.pos.y}, "font_size": 12,
                                            "text": f"{f.callsign} [{tag}]{alt}", "color": SIDE_COLOR[sq.side],
                                            "read_only": True})


def write_cartridge(dtc_dir: Path) -> None:
    home = Pos(*AIRBASE_XY)
    wps = [DtcWaypoint(Pos(home.x + nm(n), home.y), 0.0, label) for label, n in TEST_POINTS]
    path = write_dtc(dtc_dir, hornet_dtc("campaign_flights", "Syria", wps))
    print(f"DTC written: {path}", flush=True)
    for i, (label, _) in enumerate(TEST_POINTS, start=1):
        print(f"    WP{i}: {label}", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path,
                    help="campaign TOML; without it the demo uses its short test distances")
    ap.add_argument("--air-spawn", type=float, help="AI flights spawn within this many nm (demo default 30)")
    ap.add_argument("--air-despawn", type=float, help="and leave DCS beyond this many nm (demo default 40)")
    ap.add_argument("--magvar", type=float, default=5.0, help="magnetic variation, degrees east")
    ap.add_argument("--dtc-dir", type=Path, default=Path("build/dtc"),
                    help="where to write campaign_flights.dtc (point it at Saved Games/DCS/DTC to skip copying)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7341)
    demo_save.add_args(ap, "flight_bubble")
    ghosts.add_args(ap)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_bubble_config(args.config) if args.config else bubble_config(
            {"air_spawn_nm": 30, "air_despawn_nm": 40, "air_min_dwell_s": 60, "lookahead_s": 0})
        flags = {"air_spawn_nm": args.air_spawn, "air_despawn_nm": args.air_despawn}
        cfg = bubble_config({k: v for k, v in flags.items() if v is not None}, cfg)
    except (SettingsError, OSError) as e:
        raise SystemExit(f"bad settings: {e}")
    write_cartridge(args.dtc_dir)
    saver = demo_save.DemoSave(args.save, fresh=args.new)
    snap = saver.load()
    demo = Demo(cfg, args.magvar, snap, args.ghost)
    bridge = Bridge(demo.on_message, demo.on_hello, demo.on_disconnect, host=args.host, port=args.port)
    saver.restore_runs(bridge)
    await bridge.start()
    print(f"listening on {args.host}:{bridge.port}; waiting for DCS. AI flights spawn within "
          f"{cfg.air_spawn / NM:.0f} nm and leave DCS beyond {cfg.air_despawn / NM:.0f} nm", flush=True)
    async with saver.running(bridge, lambda: (demo.state, {"air": demo.air.export(),
                                                           "bubble": demo.bubble.export()})):
        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
