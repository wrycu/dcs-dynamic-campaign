"""In-game test of the bubble: aggregated ground formations spawn and despawn around players.

    python3 -m tools.bubble_demo [--airbase Khalkhalah] [--config config/campaign.toml] [--spawn 8] [--despawn 10] [--lookahead 0]

Then start build/campaign-smoke.miz (it loads the mission bridge; this replaces smoke_server).
On the first connection the demo places five formations around the airbase and draws each on
the F10 map: a filled dot and label for the formation, and a thin circle for its spawn radius.
Grey = aggregated (only data), red/blue = spawned in DCS. Fly in and out of the circles.
Every spawn, despawn and loss is also shown as an in-game message, and F10 > Other >
Campaign > Status lists every formation with its distance from you.

The campaign is saved to build/saves/bubble.json (every 5 s while something changes, and on
exit) and loaded at startup, so losses survive restarting the mission, this script, or both.
--new starts over; the previous save is kept as bubble.json.1.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
from pathlib import Path
from typing import Any

from campaign.bridge import Bridge, BridgeError, Connection
from campaign.bubble import BubbleConfig, despawn_radius, spawn_radius, strength
from campaign.dtc import DtcWaypoint, hornet_dtc, write_dtc
from campaign.model import CampaignState, GroundOrder, GroundRole, GroundUnit, Pos, Side, SpawnState
from campaign.persist import Snapshot
from campaign.protocol import PREFIX_GROUND, Envelope, Hello, Src, as_list, parse_dcs_name
from campaign.settings import SettingsError, bubble_config, load_bubble_config
from campaign.spawner import GroundSpawner
from tools import demo_save
from tools.build_test_missions import AIRBASE_NAME, AIRBASE_XY

# (id, side, role, composition, nm north of the airbase, nm east)
FORMATIONS = [
    ("blue1", Side.BLUE, GroundRole.ARMOR, {"M-1 Abrams": 4}, -2, 0),
    ("near1", Side.RED, GroundRole.MECH, {"BTR-80": 2, "Ural-375": 2}, 3, 0),
    ("armor1", Side.RED, GroundRole.ARMOR, {"T-72B": 4, "BMP-2": 4}, 13, 0),
    ("mech1", Side.RED, GroundRole.MECH, {"BTR-80": 6, "Ural-375": 2}, 24, 0),
    ("sa6", Side.RED, GroundRole.SAM, {"Kub 1S91 str": 1, "Kub 2P25 ln": 3}, 0, 32),
]

GREY = [0.6, 0.6, 0.6, 1]
SMOKE_COLOR = {Side.RED: "red", Side.BLUE: "blue"}
NAV_INTERVAL_S = 10
NAV_ARRIVED_M = 2_000
M_PER_NM = 1852.0
M_PER_FT = 0.3048
SMOKE_REFRESH_S = 270  # DCS smoke lasts about 5 minutes

SIDE_COLOR = {Side.RED: [1, 0.15, 0.15, 1], Side.BLUE: [0.2, 0.45, 1, 1]}


class Demo:
    def __init__(self, airbase: str, cfg: BubbleConfig, magvar: float = 5.0,
                 dtc_dir: Path | None = None, snap: Snapshot | None = None) -> None:
        self.airbase = airbase
        self.dtc_dir = dtc_dir
        self.magvar = magvar  # degrees east; magnetic = true - magvar
        self.players: list[dict] = []
        self.nav_target: str | None = None
        self.nav_task: asyncio.Task[Any] | None = None
        self.cfg = cfg
        self.state = snap.state if snap else CampaignState(theater_map="", clock=0.0, sides={})
        self.conn: Connection | None = None
        self.spawner = GroundSpawner(self.state, self._call, cfg, on_change=self._on_change)
        if snap:
            self.spawner.restore(snap.extra.get("ground") or {})
        self.draw_ids = {f[0]: 1000 + 10 * i for i, f in enumerate(FORMATIONS)}
        self.tasks: set[asyncio.Task[Any]] = set()
        self.last_state: dict[str, SpawnState] = {u.id: u.spawn for u in self.state.ground_units.values()}
        self.smoke_tasks: dict[str, asyncio.Task[Any]] = {}
        self.quiet = False

    async def _call(self, op: str, data: dict[str, Any]) -> Any:
        if self.conn is None or self.conn.closed:
            raise BridgeError("mission not connected")
        return await self.conn.call(op, data)

    def _bg(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # ---------- bridge callbacks ----------

    async def on_hello(self, conn: Connection, hello: Hello, new_run: bool) -> None:
        print(f"HELLO {conn.src} run={hello.run_id} new_run={new_run} spawned={hello.spawned}", flush=True)
        if conn.src != Src.MISSION:
            return
        self.conn = conn
        self.quiet = True  # a restart wipes DCS; don't announce that as despawns
        try:
            self.spawner.on_hello(hello, new_run)
        finally:
            self.quiet = False
        if not self.state.ground_units:
            ab = await conn.call("query", {"what": "airbase", "airbase": self.airbase})
            if not ab or not ab.get("exists"):
                print(f"airbase {self.airbase!r} not found; pass --airbase", flush=True)
                return
            c = ab["pos"]
            if self.airbase == AIRBASE_NAME:
                # Same anchor as the cartridge written at startup, so its waypoints match the formations.
                drift = math.hypot(c["x"] - AIRBASE_XY[0], c["y"] - AIRBASE_XY[1])
                if drift > 100:
                    print(f"WARNING: DCS puts {self.airbase} {drift / M_PER_NM:.1f} nm from AIRBASE_XY; "
                          f"update it to ({c['x']:.1f}, {c['y']:.1f})", flush=True)
                c = {"x": AIRBASE_XY[0], "y": AIRBASE_XY[1]}
            for fid, side, role, comp, north, east in FORMATIONS:
                self.state.ground_units[fid] = GroundUnit(
                    id=fid, side=side, name=fid, role=role, composition=dict(comp),
                    pos=Pos(c["x"] + north * M_PER_NM, c["y"] + east * M_PER_NM), location_id=None, order=GroundOrder.HOLD)
            await self.write_cartridge(conn, hello.map or "", Pos(c["x"], c["y"]))
        if new_run:
            for u in self.state.ground_units.values():
                self._bg(self.draw(u))
            nav = [{"label": f"{fid}", "key": f"nav:{fid}"} for fid, *_ in FORMATIONS]
            nav.append({"label": "Stop navigating", "key": "nav:"})
            await conn.call("menu", {"audience": "all", "items": [
                {"label": "Navigate to", "items": nav},
                {"label": "Status (bearing/range)", "key": "status"},
                {"label": "Smoke all formations", "key": "smoke"},
            ]})
            await conn.call("message", {"audience": "all", "duration": 20, "text":
                                        "Bubble demo connected. F10 map: grey = aggregated, "
                                        "coloured = spawned. Dashed circles are spawn radii.\n"
                                        "F10 > Other > Campaign > Navigate to <formation> gives a steer "
                                        "every 10 s. Spawned formations are marked with smoke."})

    def on_disconnect(self, conn: Connection) -> None:
        print(f"DISCONNECT {conn.src}", flush=True)

    def on_message(self, conn: Connection, env: Envelope) -> None:
        if env.type == "telemetry":
            self.players = [p for p in as_list(env.data.get("players")) if isinstance(p, dict)]
            self.spawner.on_telemetry(env.data)
            self._bg(self.tick())
        elif env.type == "event":
            kind = env.data.get("kind")
            if kind in ("unit_killed", "kill", "unit_removed", "birth"):
                unit = env.data.get("unit") or env.data.get("victim") or {}
                print(f"event   {kind:12} {unit.get('name')} ({unit.get('type')})", flush=True)
            self.spawner.on_event(env.data)
            if kind == "unit_killed":
                parsed = parse_dcs_name((env.data.get("unit") or {}).get("name"))
                u = parsed and parsed.prefix == PREFIX_GROUND and self.state.ground_units.get(parsed.model_id)
                if u:
                    left = strength(u)
                    self.say(f"{u.id} lost a {env.data['unit'].get('type')}: "
                             + (f"{left} left" if left else "DESTROYED"))
            elif kind == "menu_select" and env.data.get("key") == "status":
                self.say(self.status_text(), duration=30)
            elif kind == "menu_select" and str(env.data.get("key", "")).startswith("nav:"):
                self.navigate(env.data["key"][4:] or None)
            elif kind == "menu_select" and env.data.get("key") == "smoke":
                for u in self.state.ground_units.values():
                    self._bg(self.smoke(u, "white" if u.spawn is SpawnState.AGGREGATED else None))
                self.say("Smoke: red/blue = spawned formation, white = aggregated (nothing there yet)")

    # ---------- bubble ----------

    async def tick(self) -> None:
        plan = await self.spawner.tick()
        if plan.spawn or plan.despawn:
            self.print_status()

    def _on_change(self, u: GroundUnit) -> None:
        self._bg(self.draw(u))
        prev = self.last_state.get(u.id, SpawnState.AGGREGATED)
        self.last_state[u.id] = u.spawn
        if u.spawn is not prev and strength(u) > 0 and not self.quiet:
            verb = "SPAWNED" if u.spawn is SpawnState.SPAWNED else "despawned"
            self.say(f"{u.id} {verb} ({strength(u)} vehicles, {self.bra(u)} from you)")
        if u.spawn is SpawnState.SPAWNED and u.id not in self.smoke_tasks:
            self.smoke_tasks[u.id] = asyncio.create_task(self.keep_smoking(u))
        elif u.spawn is not SpawnState.SPAWNED and u.id in self.smoke_tasks:
            self.smoke_tasks.pop(u.id).cancel()

    async def keep_smoking(self, u: GroundUnit) -> None:
        """Smoke a spawned formation now and every few minutes until it despawns."""
        while True:
            await self.smoke(u)
            await asyncio.sleep(SMOKE_REFRESH_S)

    async def smoke(self, u: GroundUnit, color: str | None = None) -> None:
        if self.conn is None or self.conn.closed:
            return
        try:
            await self.conn.call("smoke", {"pos": {"x": u.pos.x, "y": u.pos.y},
                                           "color": color or SMOKE_COLOR.get(u.side, "orange")})
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"smoke {u.id} failed: {e}", flush=True)

    # ---------- data cartridge ----------

    def cartridge_points(self, airbase: Pos) -> tuple[list[Pos], list[str]]:
        """Waypoints 1-5 = formations in FORMATIONS order (start positions), 6 = the airbase."""
        points = [Pos(airbase.x + north * M_PER_NM, airbase.y + east * M_PER_NM)
                  for _, _, _, _, north, east in FORMATIONS]
        labels = [fid for fid, *_ in FORMATIONS]
        return points + [airbase], labels + [self.airbase]

    def save_cartridge(self, theatre: str, airbase: Pos, heights: list[float] | None = None) -> None:
        if self.dtc_dir is None:
            return
        points, labels = self.cartridge_points(airbase)
        heights = heights or [0.0] * len(points)
        wps = [DtcWaypoint(p, float(h), note) for p, h, note in zip(points, heights, labels)]
        path = write_dtc(self.dtc_dir, hornet_dtc("campaign_bubble", theatre, wps))
        seq = write_dtc(self.dtc_dir, hornet_dtc("campaign_bubble_seq", theatre, wps, route=1))
        print(f"DTC written: {path} (no route sequence)\n             {seq} (route sequence 1)", flush=True)
        for i, (note, h) in enumerate(zip(labels, heights), start=1):
            print(f"    WP{i}: {note} (elev {float(h) / M_PER_FT:.0f} ft)", flush=True)

    async def write_cartridge(self, conn: Connection, theatre: str, airbase: Pos) -> None:
        """Rewrite the cartridge with terrain elevations from DCS."""
        if self.dtc_dir is None:
            return
        points, _ = self.cartridge_points(airbase)
        try:
            res = await conn.call("query", {"what": "heights",
                                            "points": [{"x": p.x, "y": p.y} for p in points]})
            heights = [float(h) for h in as_list(res.get("heights"))]
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"heights query failed ({e}); keeping elevations at 0", flush=True)
            heights = None
        self.save_cartridge(theatre, airbase, heights)

    # ---------- navigator ----------

    def navigate(self, target: str | None) -> None:
        if self.nav_task:
            self.nav_task.cancel()
            self.nav_task = None
        self.nav_target = target if target in self.state.ground_units else None
        if self.nav_target:
            self.nav_task = asyncio.create_task(self._nav_loop())
        else:
            self.say("Navigation off")

    async def _nav_loop(self) -> None:
        u = self.state.ground_units[self.nav_target]
        while True:
            if not self.players:
                self.say(f"NAV {u.id}: waiting for your position", duration=NAV_INTERVAL_S)
            else:
                p = self.players[0]
                me = Pos(float(p["pos"]["x"]), float(p["pos"]["y"]))
                dx, dy = u.pos.x - me.x, u.pos.y - me.y
                dist = math.hypot(dx, dy)
                if dist < NAV_ARRIVED_M:
                    self.say(f"NAV {u.id}: OVERHEAD. Navigation off.", duration=15)
                    self.nav_task = None
                    return
                brg = math.degrees(math.atan2(dy, dx)) % 360
                spd = float(p.get("spd") or 0)
                eta = f", {dist / spd / 60:.1f} min at {spd * 1.944:.0f} kt" if spd > 20 else ""
                state = "spawned" if u.spawn is SpawnState.SPAWNED else "aggregated"
                self.say(f"NAV {u.id} ({state}): steer {(brg - self.magvar) % 360:03.0f}M "
                         f"({brg:03.0f}T), {dist / M_PER_NM:.1f} nm{eta}",
                         duration=NAV_INTERVAL_S)
            await asyncio.sleep(NAV_INTERVAL_S)

    def bra(self, u: GroundUnit) -> str:
        """Magnetic bearing and range (nm) from the (first) player to a formation."""
        if not self.spawner.observers:
            return "? "
        me = self.spawner.observers[0].pos
        dx, dy = u.pos.x - me.x, u.pos.y - me.y  # x north, y east
        brg = math.degrees(math.atan2(dy, dx)) % 360
        nm = math.hypot(dx, dy) / M_PER_NM
        return f"{(brg - self.magvar) % 360:03.0f}M {nm:.1f} nm"

    def say(self, text: str, duration: int = 10) -> None:
        print(f"MSG     {text}", flush=True)
        if self.conn is not None and not self.conn.closed:
            self._bg(self._say(text, duration))

    async def _say(self, text: str, duration: int) -> None:
        try:
            await self.conn.call("message", {"audience": "all", "text": text, "duration": duration})
        except (BridgeError, asyncio.TimeoutError):
            pass

    def status_text(self) -> str:
        lines = ["BUBBLE STATUS: magnetic bearing and range from you | spawn radius | despawn beyond"]
        for u in self.state.ground_units.values():
            r = spawn_radius(u, self.cfg)
            state = "SPAWNED" if u.spawn is SpawnState.SPAWNED else "aggregated"
            lines.append(f"{u.id}: {state}, {strength(u)} veh | {self.bra(u)} | {r / M_PER_NM:.1f} nm | "
                         f"{despawn_radius(u, self.cfg) / M_PER_NM:.1f} nm")
        return "\n".join(lines)

    def print_status(self) -> None:
        live = sum(strength(u) for u in self.state.ground_units.values() if u.spawn is SpawnState.SPAWNED)
        print(f"--- {len(self.spawner.observers)} player(s), {live}/{self.cfg.max_units} vehicles live", flush=True)
        for u in self.state.ground_units.values():
            comp = ", ".join(f"{n} {t}" for t, n in sorted(u.composition.items())) or "destroyed"
            print(f"    {u.id:7} {u.spawn.name:10} r={spawn_radius(u, self.cfg) / M_PER_NM:.1f}nm  {comp}", flush=True)

    async def draw(self, u: GroundUnit) -> None:
        if self.conn is None:
            return
        base = self.draw_ids[u.id]
        alive = strength(u) > 0
        color = SIDE_COLOR[u.side] if u.spawn is SpawnState.SPAWNED else GREY
        fill = color[:3] + [0.6 if alive else 0.1]
        state = "SPAWNED" if u.spawn is SpawnState.SPAWNED else "aggregated"
        label = f"{u.id}: {strength(u)} veh, {state}" if alive else f"{u.id}: destroyed"
        pos = {"x": u.pos.x, "y": u.pos.y}
        try:
            await self.conn.call("draw", {"id": base, "audience": "all", "shape": "circle", "center": pos,
                                          "radius": 700, "color": color, "fill": fill})
            await self.conn.call("draw", {"id": base + 1, "audience": "all", "shape": "text",
                                          "pos": {"x": u.pos.x - 1200, "y": u.pos.y + 900}, "text": label,
                                          "color": color, "fill": [0, 0, 0, 0], "font_size": 14})
            await self.conn.call("draw", {"id": base + 2, "audience": "all", "shape": "circle", "center": pos,
                                          "radius": spawn_radius(u, self.cfg), "color": color[:3] + [0.5],
                                          "fill": [0, 0, 0, 0], "line_type": 2})
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"draw {u.id} failed: {e}", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--airbase", default="Khalkhalah")
    ap.add_argument("--config", type=Path,
                    help="campaign TOML (e.g. config/campaign.toml); without it the demo uses its short "
                         "test distances. The flags below override either.")
    ap.add_argument("--spawn", type=float, help="ground spawn distance, nm (demo default 8)")
    ap.add_argument("--despawn", type=float, help="ground despawn distance, nm (demo default 10)")
    ap.add_argument("--sam-margin", type=float, help="nm added to SAM range to spawn (demo default 5)")
    ap.add_argument("--sam-despawn-margin", type=float,
                    help="nm added to SAM range to despawn (demo default 9)")
    ap.add_argument("--dwell", type=float, help="minimum seconds a spawn stays (demo default 30)")
    ap.add_argument("--lookahead", type=float,
                    help="seconds to project players ahead (demo default 0: circles on the map are exact)")
    ap.add_argument("--magvar", type=float, default=5.0,
                    help="magnetic variation, degrees east (Syria is about 5)")
    ap.add_argument("--dtc-dir", type=Path, default=Path("build/dtc"),
                    help="where to write campaign_bubble.dtc (point it at Saved Games/DCS/DTC to skip copying)")
    ap.add_argument("--max-units", type=int)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7341)
    demo_save.add_args(ap, "bubble")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    demo_defaults = {"ground_spawn_nm": 8, "ground_despawn_nm": 10, "sam_spawn_margin_nm": 5,
                     "sam_despawn_margin_nm": 9, "min_dwell_s": 30, "lookahead_s": 0}
    flags = {"ground_spawn_nm": args.spawn, "ground_despawn_nm": args.despawn,
             "sam_spawn_margin_nm": args.sam_margin, "sam_despawn_margin_nm": args.sam_despawn_margin,
             "min_dwell_s": args.dwell, "lookahead_s": args.lookahead, "max_units": args.max_units}
    try:
        cfg = load_bubble_config(args.config) if args.config else bubble_config(demo_defaults)
        cfg = bubble_config({k: v for k, v in flags.items() if v is not None}, cfg)
    except (SettingsError, OSError) as e:
        raise SystemExit(f"bad settings: {e}")
    print(f"bubble: ground spawn {cfg.ground_spawn / M_PER_NM:.1f} nm / despawn {cfg.ground_despawn / M_PER_NM:.1f} nm, "
          f"SAM range + {cfg.threat_spawn_margin / M_PER_NM:.1f} / + {cfg.threat_despawn_margin / M_PER_NM:.1f} nm", flush=True)
    saver = demo_save.DemoSave(args.save, fresh=args.new)
    snap = saver.load()
    demo = Demo(args.airbase, cfg, args.magvar, args.dtc_dir, snap)
    if snap:
        print(demo.status_text(), flush=True)
    if args.airbase == AIRBASE_NAME:
        # Write the cartridge now so it can be loaded from the briefing, before the mission connects.
        demo.save_cartridge("Syria", Pos(*AIRBASE_XY))
    bridge = Bridge(demo.on_message, demo.on_hello, demo.on_disconnect, host=args.host, port=args.port)
    saver.restore_runs(bridge)
    await bridge.start()
    print(f"listening on {args.host}:{bridge.port}; waiting for DCS", flush=True)
    async with saver.running(bridge, lambda: (demo.state, {"ground": demo.spawner.export()})):
        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
