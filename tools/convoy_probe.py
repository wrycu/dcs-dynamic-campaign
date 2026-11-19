"""Proof P3: ground movement at scale. Pair with build/campaign-smoke.miz (single player is fine).

    python3 -m tools.convoy_probe [--groups 24] [--vehicles 8] [--radius-nm 30] [--cycle-min 4]

Around Khalkhalah, the probe picks start and end points on roads, asks DCS for the road route
between them (land.findPathOnRoads), and spawns a red convoy at each start, in a column on the
road, told to drive to its end "On Road". It then checks:

- Routes: how long DCS takes to answer a road route, and whether convoys drive the route it
  gave (the distance of each lead vehicle from that route, "off route").
- Despawn and respawn mid-route: every --cycle-min in-game minutes, half the convoys are taken out of
  DCS and put back 5 s later where they were, in a column on the road, with the rest of the
  route. How long until they move again, and do they stay on the road?
- Scale: how fast the sim runs (model time against wall time; 1.00 = real time), and how long
  the bridge takes to answer, with all convoys moving. Watch the frame rate yourself.

Convoys that arrive turn around (a respawn with the route reversed), so the load stays the same.
A summary prints every 30 s of game time. All times are in-game, so pausing doesn't skew them;
single-player DCS pauses when it loses focus, so keep it focused (or turn that off) while it runs. Destinations are labelled on the F10 map ("C07 end").
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any

from campaign.bridge import Bridge, BridgeError, Connection
from campaign.catalog import COUNTRY_FOR_SIDE
from campaign.model import Pos, Side
from campaign.protocol import PREFIX_GROUND, Envelope, Hello, Src, as_list, group_name, parse_dcs_name, unit_name
from campaign.roads import Path, simplify
from tools.build_test_missions import AIRBASE_NAME, AIRBASE_XY

NM = 1852.0
MIX = ["BTR-80", "Ural-375", "BMP-2", "Ural-375", "T-72B", "Ural-375", "BTR-80", "T-72B"]
SPACING_M = 25.0
RESPAWN_GAP_S = 5.0
MOVED_M = 30.0        # a respawned convoy has "resumed" once its lead is this far along
ARRIVED_M = 200.0     # within this of the end counts as arrived
SUMMARY_S = 30.0
ROUTE_TIMEOUT_S = 120.0
POINTS_TOLERANCE_M = 5.0
DRAW_BASE = 4000


@dataclass
class Convoy:
    idx: int
    path: Path
    query_ms: float
    spawned: bool = False
    busy: bool = False
    along: float = 0.0
    offset: float = 0.0
    max_offset: float = 0.0
    last: tuple[float, float] | None = None     # (model time, along) of the previous report
    speed_ms: float | None = None
    respawned_at: float | None = None           # wall time of the last respawn, until it moves
    respawn_along: float = 0.0
    started_at: float | None = None             # model time the current leg began
    legs: int = 0
    samples: list[float] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"convoy{self.idx:02d}"

    @property
    def label(self) -> str:
        return f"C{self.idx:02d}"


def column(path: Path, lead_s: float, n: int) -> list[tuple[Pos, float]]:
    """Positions and headings for n vehicles in a column with the lead at lead_s."""
    return [(path.point_at(lead_s - k * SPACING_M), path.heading_at(lead_s - k * SPACING_M)) for k in range(n)]


def group_data(c: Convoy, lead_s: float, n: int, speed_ms: float, orders: str = "road") -> dict[str, Any]:
    units = []
    for k, (p, hdg) in enumerate(column(c.path, lead_s, n), start=1):
        units.append({"name": unit_name(PREFIX_GROUND, c.id, k), "type": MIX[(k - 1) % len(MIX)],
                      "x": round(p.x, 1), "y": round(p.y, 1), "heading": round(hdg, 4),
                      "skill": "Average", "playerCanDrive": False})
    route = c.path.after(lead_s)
    if len(route) < 2:
        route = route + [c.path.points[-1]]
    if orders == "road":
        # Start and end only, "On Road": DCS's AI finds and follows the roads itself.
        pts, action = [route[0], route[-1]], "On Road"
    else:
        # The saved route itself, thinned to its corners, driven point to point ("Off Road"):
        # DCS has no road-following to do.
        pts, action = simplify(route, POINTS_TOLERANCE_M), "Off Road"
    return {"category": "ground", "country": COUNTRY_FOR_SIDE[Side.RED], "group": {
        "name": group_name(PREFIX_GROUND, c.id), "task": "Ground Nothing", "units": units,
        "route": {"points": [{"x": round(p.x, 1), "y": round(p.y, 1), "action": action, "type": "Turning Point",
                              "speed": speed_ms, "speed_locked": True, "ETA": 0, "ETA_locked": False,
                              "formation_template": ""} for p in pts]}}}


class Probe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rng = random.Random(args.seed)
        self.conn: Connection | None = None
        self.convoys: dict[str, Convoy] = {}
        self.speed = args.speed_kph / 3.6
        self.model_t: float | None = None
        self.model_t0: float | None = None   # model time when the convoys were spawned
        self.wall0: tuple[float, float] | None = None     # (wall, model) at the start of a ratio window
        self.ratio: float | None = None
        self.rtt_ms: list[float] = []
        self.resumes: list[float] = []
        self.respawns = 0
        self.failures = 0
        self.cycle = 0
        self.slow_routes = 0

    # ---------- bridge callbacks ----------

    def on_hello(self, conn: Connection, hello: Hello, new_run: bool) -> None:
        print(f"HELLO {conn.src} run={hello.run_id} new_run={new_run}", flush=True)
        if conn.src != Src.MISSION:
            return
        self.conn = conn
        if new_run:
            self.convoys.clear()
            asyncio.create_task(self.run(conn))

    def on_disconnect(self, conn: Connection) -> None:
        print(f"DISCONNECT {conn.src}", flush=True)
        if conn is self.conn:
            self.conn = None

    def on_message(self, conn: Connection, env: Envelope) -> None:
        if env.type == "telemetry" and env.t is not None:
            self.on_telemetry(env.t, env.data)
        elif env.type == "event" and env.data.get("kind") == "unit_killed":
            unit = env.data.get("unit") or {}
            print(f"EVENT   unit_killed {unit.get('name')} ({unit.get('type')})", flush=True)

    def on_telemetry(self, t: float, data: dict[str, Any]) -> None:
        now = time.monotonic()
        self.model_t = t
        if self.wall0 is None:
            self.wall0 = (now, t)
        elif now - self.wall0[0] >= 10:
            self.ratio = (t - self.wall0[1]) / (now - self.wall0[0])
            self.wall0 = (now, t)
        for g in as_list(data.get("groups")):
            parsed = parse_dcs_name(g.get("id")) if isinstance(g, dict) else None
            c = self.convoys.get(parsed.model_id) if parsed and parsed.prefix == PREFIX_GROUND else None
            pos = g.get("pos") if c else None
            if not c or not c.spawned or c.busy or not isinstance(pos, dict):
                continue
            proj = c.path.project(Pos(float(pos["x"]), float(pos["y"])))
            c.along, c.offset = proj.along, proj.offset
            c.max_offset = max(c.max_offset, proj.offset)
            if c.last and t > c.last[0]:
                c.speed_ms = (proj.along - c.last[1]) / (t - c.last[0])
            c.last = (t, proj.along)
            if c.respawned_at is not None and proj.along - c.respawn_along >= MOVED_M:
                took = now - c.respawned_at
                self.resumes.append(took)
                c.respawned_at = None
                print(f"RESUMED {c.label} moving again {took:.0f} s after respawn, {proj.offset:.0f} m off route",
                      flush=True)
            if proj.along >= c.path.length - ARRIVED_M:
                asyncio.create_task(self.turn_around(c, t))

    # ---------- scenario ----------

    async def call(self, op: str, data: dict[str, Any], timeout: float = 10.0) -> Any:
        if self.conn is None:
            raise BridgeError("mission not connected")
        return await self.conn.call(op, data, timeout)

    async def road_point(self, p: Pos) -> Pos | None:
        res = await self.call("query", {"what": "road_point", "pos": {"x": p.x, "y": p.y}})
        return Pos(res["pos"]["x"], res["pos"]["y"]) if res and res.get("found") else None

    def random_point(self, around: Pos, lo_nm: float, hi_nm: float) -> Pos:
        a = self.rng.uniform(0, 2 * math.pi)
        r = self.rng.uniform(lo_nm, hi_nm) * NM
        return Pos(around.x + r * math.cos(a), around.y + r * math.sin(a))

    async def plan(self, idx: int) -> Convoy | None:
        home = Pos(*AIRBASE_XY)
        for _ in range(6):
            start = await self.road_point(self.random_point(home, 0, self.args.radius_nm))
            end = await self.road_point(self.random_point(start, 8, 20)) if start else None
            if not start or not end:
                continue
            t = time.monotonic()
            try:
                # The mission script (and so the sim) waits while DCS searches; allow a long time to measure it.
                res = await self.call("query", {"what": "road_path", "from": {"x": start.x, "y": start.y},
                                                "to": {"x": end.x, "y": end.y}}, timeout=ROUTE_TIMEOUT_S)
            except asyncio.TimeoutError:
                print(f"ROUTE   C{idx:02d}: no answer from DCS in {ROUTE_TIMEOUT_S:.0f} s "
                      f"({math.dist((start.x, start.y), (end.x, end.y)) / NM:.1f} nm straight); skipped", flush=True)
                self.slow_routes += 1
                continue
            ms = (time.monotonic() - t) * 1000
            if ms > 2_000:
                self.slow_routes += 1
                print(f"SLOW    C{idx:02d}: DCS took {ms / 1000:.1f} s for this route (the sim waits meanwhile)",
                      flush=True)
            pts = [Pos(p["x"], p["y"]) for p in (res or {}).get("points", [])]
            if len(pts) >= 2 and Path(pts).length >= 5_000:
                path = Path(pts)
                straight = math.dist((start.x, start.y), (end.x, end.y))
                print(f"ROUTE   C{idx:02d}: {len(pts)} points ({len(simplify(pts, POINTS_TOLERANCE_M))} corners), "
                      f"{path.length / NM:.1f} nm by road "
                      f"({straight / NM:.1f} nm straight), answered in {ms:.0f} ms", flush=True)
                return Convoy(idx, path, ms)
        print(f"ROUTE   C{idx:02d}: no usable road route found", flush=True)
        return None

    async def spawn(self, c: Convoy, lead_s: float) -> bool:
        try:
            await self.call("spawn_group", group_data(c, lead_s, self.args.vehicles, self.speed, self.args.orders))
            c.spawned, c.last, c.speed_ms = True, None, None
            return True
        except (BridgeError, asyncio.TimeoutError) as e:
            self.failures += 1
            print(f"SPAWN   {c.label} failed: {e}", flush=True)
            return False

    async def run(self, conn: Connection) -> None:
        try:
            await self.call("message", {"audience": "all", "duration": 20, "text":
                                        f"P3 convoy probe: planning {self.args.groups} convoys of "
                                        f"{self.args.vehicles} around {AIRBASE_NAME}."})
            for i in range(1, self.args.groups + 1):
                try:
                    c = await self.plan(i)
                except (BridgeError, asyncio.TimeoutError) as e:
                    print(f"ROUTE   C{i:02d}: failed ({type(e).__name__}: {e}); skipped", flush=True)
                    continue
                if not c:
                    continue
                self.convoys[c.id] = c
                lead_s = (self.args.vehicles - 1) * SPACING_M
                if await self.spawn(c, lead_s):
                    c.started_at = self.model_t
                    c.legs = 1
                end = c.path.points[-1]
                await self.call("draw", {"id": DRAW_BASE + i, "audience": "all", "shape": "text",
                                         "pos": {"x": end.x, "y": end.y}, "font_size": 12,
                                         "text": f"{c.label} end", "color": [1, 0.3, 0.3, 1], "read_only": True})
                await asyncio.sleep(0.5)
            qms = sorted(c.query_ms for c in self.convoys.values())
            if qms:
                print(f"ROUTES  {len(qms)} routes; DCS answered in {qms[0]:.0f}-{qms[-1]:.0f} ms "
                      f"(median {qms[len(qms) // 2]:.0f}); {self.slow_routes} slow (> 2 s) or unanswered",
                      flush=True)
            await self.call("message", {"audience": "all", "duration": 20,
                                        "text": f"P3: {len(self.convoys)} convoys moving. Ends are labelled on F10."})
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"SETUP FAILED: {type(e).__name__}: {e}", flush=True)
            return
        self.model_t0 = self.model_t
        asyncio.create_task(self.summaries(conn))
        if self.args.cycle_min > 0:
            asyncio.create_task(self.cycles(conn))

    async def respawn(self, c: Convoy, why: str, reverse: bool = False) -> None:
        if c.busy or not c.spawned:
            return
        c.busy = True
        try:
            res = await self.call("despawn_group", {"group": group_name(PREFIX_GROUND, c.id)})
            c.spawned = False
            pos = (res or {}).get("pos")
            if not isinstance(pos, dict):
                print(f"RESPAWN {c.label}: gone from DCS ({why})", flush=True)
                return
            proj = c.path.project(Pos(float(pos["x"]), float(pos["y"])))
            await asyncio.sleep(RESPAWN_GAP_S)
            if reverse:
                c.path = c.path.reversed()
                lead_s = max(c.path.length - proj.along, (self.args.vehicles - 1) * SPACING_M)
            else:
                lead_s = proj.along
            if await self.spawn(c, lead_s):
                self.respawns += 1
                c.respawned_at, c.respawn_along = time.monotonic(), lead_s
                print(f"RESPAWN {c.label} ({why}) at {lead_s / NM:.1f} of {c.path.length / NM:.1f} nm, "
                      f"was {proj.offset:.0f} m off route", flush=True)
        except (BridgeError, asyncio.TimeoutError) as e:
            self.failures += 1
            print(f"RESPAWN {c.label} failed: {e}", flush=True)
        finally:
            c.busy = False

    async def turn_around(self, c: Convoy, t: float) -> None:
        if c.busy:
            return
        if c.started_at is not None and t > c.started_at:
            avg = (c.path.length - (self.args.vehicles - 1) * SPACING_M) / (t - c.started_at)
            c.samples.append(avg)
            print(f"ARRIVED {c.label} after {(t - c.started_at) / 60:.1f} min, {c.path.length / NM:.1f} nm, "
                  f"average {avg * 3.6:.0f} km/h; turning around", flush=True)
        await self.respawn(c, "turn around", reverse=True)
        c.started_at, c.legs = t, c.legs + 1

    async def model_wait(self, conn: Connection, seconds: float) -> bool:
        """Wait for this much model (in-game) time; False if the mission went away."""
        while self.model_t is None and self.conn is conn:
            await asyncio.sleep(1)
        start = self.model_t
        while self.conn is conn:
            if self.model_t is not None and start is not None and self.model_t - start >= seconds:
                return True
            await asyncio.sleep(1)
        return False

    async def cycles(self, conn: Connection) -> None:
        while await self.model_wait(conn, self.args.cycle_min * 60):
            self.cycle += 1
            half = [c for c in self.convoys.values() if c.idx % 2 == self.cycle % 2]
            print(f"CYCLE   {self.cycle}: despawning and respawning {len(half)} convoys", flush=True)
            await asyncio.gather(*(self.respawn(c, f"cycle {self.cycle}") for c in half))

    async def summaries(self, conn: Connection) -> None:
        while await self.model_wait(conn, SUMMARY_S):
            t = time.monotonic()
            try:
                await self.call("query", {"what": "time"})
                self.rtt_ms.append((time.monotonic() - t) * 1000)
            except (BridgeError, asyncio.TimeoutError):
                pass
            live = [c for c in self.convoys.values() if c.spawned]
            moving = [c for c in live if c.speed_ms is not None and c.speed_ms > 1]
            speeds = [c.speed_ms * 3.6 for c in moving if c.speed_ms is not None]
            worst = max(live, key=lambda c: c.offset, default=None)
            game_min = (self.model_t - self.model_t0) / 60 if self.model_t is not None and self.model_t0 is not None else 0
            parts = [f"SUMMARY {game_min:5.1f} game min",
                     f"{len(live)}/{len(self.convoys)} in DCS, {len(moving)} moving"
                     + (f", avg {sum(speeds) / len(speeds):.0f} km/h" if speeds else "")]
            if worst:
                parts.append(f"off route: now worst {worst.label} {worst.offset:.0f} m, "
                             f"ever {max(c.max_offset for c in self.convoys.values()):.0f} m")
            if self.ratio:
                parts.append(f"sim speed {self.ratio:.2f}x")
            if self.rtt_ms:
                parts.append(f"bridge {self.rtt_ms[-1]:.0f} ms")
            res = (f"{len(self.resumes)} resumed, avg {sum(self.resumes) / len(self.resumes):.0f} s"
                   if self.resumes else "none resumed yet")
            parts.append(f"respawns {self.respawns} ({res}), failures {self.failures}")
            print(" | ".join(parts), flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", type=int, default=24, help="number of convoys (default 24)")
    ap.add_argument("--vehicles", type=int, default=8, help="vehicles per convoy (default 8)")
    ap.add_argument("--radius-nm", type=float, default=30.0, help=f"convoys start within this of {AIRBASE_NAME}")
    ap.add_argument("--speed-kph", type=float, default=50.0, help="ordered road speed (default 50 km/h)")
    ap.add_argument("--cycle-min", type=float, default=4.0, help="despawn/respawn half the convoys this often; 0 = never")
    ap.add_argument("--orders", choices=["road", "points"], default="road",
                    help="road: start and end, 'On Road' (DCS follows roads); points: the saved route as "
                         "waypoints, 'Off Road' (no road-following in DCS)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--port", type=int, default=7341)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    probe = Probe(args)
    bridge = Bridge(probe.on_message, probe.on_hello, probe.on_disconnect, port=args.port)
    await bridge.start()
    print(f"listening on 127.0.0.1:{bridge.port}; start build/campaign-smoke.miz in DCS", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
