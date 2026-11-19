"""Proof P19, part 1: the no-mod pieces of a briefing room in DCS. Run on the dedicated server
with campaign-slots.miz (dynamic slots at Khalkhalah), and join it from your own DCS.

    python3 -m tools.briefing_probe

Everything is driven by chat commands (type them in the chat window, starting with "!"; the
other players don't see them):

    !help                  this list
    !flights               the flights on offer (each has a parked Hornet at Khalkhalah)
    !join N [cold|hot|air] [cat=airplane] [fuel=0.6]
                           ask the server to spawn you into flight N's jet with its loadout
                           (DCS.create_client_aircraft for you, as the slot screen does)
    !spec                  send yourself back to spectators (net.force_player_slot)
    !draw N                draw N extra shapes on the F10 map (everyone sees them)
    !churn S | off         redraw all the extra shapes every S seconds
    !clear                 remove the extra shapes
    !where                 what the server knows about your position, and pilots on foot
    !near [R]              objects within R m (default 300) of where you last were

What it finds out:
1. What a spectator sees on F10: the probe draws a few shapes for everyone and a few for blue
   only.
2. Whether chat commands reach the server from spectators and from a cockpit.
3. Whether the server can spawn a player into a jet with a loadout it chose, on the ramp and in
   the air.
4. What the F10 map can carry: server frame times (FRAME lines every 10 s) while you add and
   churn shapes; watch your own frame rate too.
5. Walking: climb out of a parked jet (LCtrl+E three times) and walk. Does the server see you
   (WALKER lines)? Walk up to a flight's parked Hornet: within 15 m the probe tries to put you
   in that flight.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass, field

from campaign.bridge import Bridge, BridgeError, Connection
from campaign.payloads import dcs_payload
from campaign.protocol import Envelope, Hello, Src

AIRBASE = "Khalkhalah"
AIRDROME_ID = 18
HORNET = "FA-18C_hornet"
HORNET_FUEL_KG = 4900
NM = 1852.0
REACH_M = 15.0
EXTRA_BASE_ID = 900_000  # engine shape ids for !draw
FIXED_IDS = list(range(800_000, 800_020))
BLUE_ID = 2


@dataclass
class Flight:
    n: int
    callsign: str
    mission: str
    loadout: str
    pos: dict | None = None    # parked static's position
    static: str | None = None


FLIGHTS = [
    Flight(1, "Colt 1", "CAS south of Damascus", "AIM-9M*2, AIM-7M*2, FUEL*2"),
    Flight(2, "Uzi 2", "BARCAP over Khalkhalah", "AIM-9M*2, AIM-7M*4, FUEL*3"),
    Flight(3, "Enfield 3", "SEAD near Marj Ruhayyil", "AIM-9M*2, AIM-7M*2, FUEL*1"),
]


@dataclass
class Probe:
    args: argparse.Namespace
    mission: Connection | None = None
    hooks: Connection | None = None
    extra: int = 0                    # extra shapes drawn
    churn_s: float = 0.0
    churn_task: asyncio.Task | None = None
    last_pos: dict[str, dict] = field(default_factory=dict)   # player name -> last unit position
    walkers: dict[str, dict] = field(default_factory=dict)    # walker key -> last report
    joined_by_walk: set[str] = field(default_factory=set)

    # ---------- bridge callbacks ----------

    def on_hello(self, conn: Connection, hello: Hello, new_run: bool) -> None:
        print(f"HELLO   {conn.src} new_run={new_run}", flush=True)
        if conn.src == Src.MISSION:
            self.mission = conn
            if new_run:
                asyncio.create_task(self.setup())
        elif conn.src == Src.HOOKS:
            self.hooks = conn

    def on_disconnect(self, conn: Connection) -> None:
        print(f"DISCONNECT {conn.src}", flush=True)
        if conn is self.mission:
            self.mission = None
        if conn is self.hooks:
            self.hooks = None

    def on_message(self, conn: Connection, env: Envelope) -> None:
        d = env.data or {}
        if env.type == "telemetry":
            self.telemetry(d)
            return
        if env.type not in ("event", "player"):
            return
        kind = d.get("kind")
        if kind == "chat_command":
            print(f"CHAT    {d.get('name')} (id {d.get('player_id')}, side {d.get('side')}, slot {d.get('slot')!r}): "
                  f"!{d.get('text')}", flush=True)
            asyncio.create_task(self.command(d))
        elif kind == "frame_stats":
            print(f"FRAME   server avg {d['avg_ms']:.1f} ms, max {d['max_ms']:.0f} ms, {d['over_50ms']} frames over "
                  f"50 ms in {d['seconds']:.0f} s; {self.extra} extra shapes"
                  f"{f', churn every {self.churn_s:g} s' if self.churn_s else ''}", flush=True)
        elif kind in ("player_slot", "slot_try", "player_connect", "player_disconnect"):
            e = d.get("entry") or {}
            print(f"HOOKS   {kind} {d.get('name')} id {d.get('player_id')} side {d.get('side')} slot "
                  f"{d.get('slot')!r} {d.get('unit_type') or ''} {e.get('groupName') or ''}", flush=True)
        elif kind in ("birth", "ejection", "landing_after_ejection", "player_enter_unit", "player_leave_unit",
                      "unit_removed", "pilot_dead", "takeoff", "land"):
            u = d.get("unit") or {}
            extra = f" pilot={d['pilot']}" if d.get("pilot") else ""
            print(f"EVENT   {kind} {u.get('player') or ''} {u.get('type') or ''} {u.get('name') or ''} "
                  f"cat={u.get('category')} place={d.get('place')}{extra}", flush=True)

    def telemetry(self, d: dict) -> None:
        for p in d.get("players") or []:
            if p.get("player") and p.get("pos"):
                self.last_pos[p["player"]] = p["pos"]
        seen = set()
        for w in d.get("walkers") or []:
            key = w.get("key")
            seen.add(key)
            prev = self.walkers.get(key)
            moved = prev is None or prev.get("exists") != w.get("exists") or (
                w.get("pos") and prev.get("pos") and math.dist((w["pos"]["x"], w["pos"]["y"]),
                                                               (prev["pos"]["x"], prev["pos"]["y"])) > 3)
            if moved:
                frm = w.get("from") or {}
                print(f"WALKER  {key} exists={w.get('exists')} {w.get('type') or ''} cat={w.get('category')} "
                      f"pos={_fmt(w.get('pos'))} from {frm.get('player') or frm.get('name') or '?'}", flush=True)
                self.walkers[key] = w
                if w.get("exists") and w.get("pos"):
                    asyncio.create_task(self.check_reach(w))
        for key in set(self.walkers) - seen:
            print(f"WALKER  {key} no longer reported", flush=True)
            del self.walkers[key]

    # ---------- setup ----------

    async def call(self, conn: Connection | None, op: str, data: dict, timeout: float = 30) -> object:
        if conn is None:
            raise BridgeError(f"{op}: not connected")
        return await conn.call(op, data, timeout=timeout)

    async def setup(self) -> None:
        try:
            spots = await self.call(self.mission, "query", {"what": "parking", "airbase": AIRBASE, "free_only": True})
            free = [s for s in spots.get("spots", []) if not s.get("occupied")]
            ab = await self.call(self.mission, "query", {"what": "airbase", "airbase": AIRBASE})
            center = ab["pos"]
            free.sort(key=lambda s: math.dist((s["pos"]["x"], s["pos"]["y"]), (center["x"], center["y"])))
            for f, spot in zip(FLIGHTS, free[5:]):
                f.pos = spot["pos"]
                f.static = f"p19 {f.callsign}"
                await self.call(self.mission, "spawn_static", {"country": 80, "static": {
                    "name": f.static, "type": HORNET, "category": "Planes", "x": f.pos["x"], "y": f.pos["y"],
                    "heading": 0, "livery_id": ""}})
                await self.call(self.mission, "draw", {"id": FIXED_IDS[f.n], "audience": "all", "shape": "text",
                                                       "pos": {"x": f.pos["x"] + 20, "y": f.pos["y"]},
                                                       "text": f"{f.callsign}: {f.mission}", "font_size": 12,
                                                       "color": [1, 1, 0, 1], "fill": [0, 0, 0, 0.5]})
                print(f"FLIGHT  {f.n} {f.callsign}: parked Hornet at term {spot.get('term')} {_fmt(f.pos)}", flush=True)
            # what spectators see: one label for everyone, one for blue only
            x, y = center["x"] + 3 * NM, center["y"]
            await self.call(self.mission, "draw", {"id": FIXED_IDS[10], "audience": "all", "shape": "text",
                                                   "pos": {"x": x, "y": y}, "text": "P19: drawn for ALL",
                                                   "font_size": 16, "color": [1, 1, 1, 1], "fill": [0, 0, 0, 0.6]})
            await self.call(self.mission, "draw", {"id": FIXED_IDS[11], "audience": "blue", "shape": "text",
                                                   "pos": {"x": x - 1 * NM, "y": y},
                                                   "text": "P19: drawn for BLUE only", "font_size": 16,
                                                   "color": [0.4, 0.6, 1, 1], "fill": [0, 0, 0, 0.6]})
            await self.call(self.mission, "draw", {"id": FIXED_IDS[12], "audience": "all", "shape": "poly",
                                                   "points": [{"x": x + 4 * NM, "y": y - 10 * NM},
                                                              {"x": x + 6 * NM, "y": y},
                                                              {"x": x + 4 * NM, "y": y + 10 * NM}],
                                                   "color": [1, 0, 0, 1], "fill": [1, 0, 0, 0.15]})
            await self.call(self.mission, "message", {"audience": "all", "duration": 30, "text":
                            "P19 briefing room probe. Type !help in chat (it won't show to others)."})
            print("SETUP   done: 3 flights parked, labels drawn (ALL and BLUE-only near Khalkhalah)", flush=True)
        except (BridgeError, asyncio.TimeoutError, KeyError) as e:
            print(f"SETUP   failed: {e}", flush=True)

    # ---------- chat commands ----------

    async def reply(self, player_id: int, text: str) -> None:
        print(f"REPLY   to {player_id}: {text}", flush=True)
        for line in text.split("\n"):
            try:
                await self.call(self.hooks, "chat_to", {"player_id": player_id, "text": line})
            except (BridgeError, asyncio.TimeoutError) as e:
                print(f"REPLY   failed: {e}", flush=True)

    async def command(self, d: dict) -> None:
        pid = d.get("player_id")
        words = (d.get("text") or "").split()
        if not words:
            return
        cmd, rest = words[0].lower(), words[1:]
        try:
            if cmd == "help":
                await self.reply(pid, "!flights | !join N [cold|hot|air] | !spec | !draw N | !churn S|off | "
                                      "!clear | !where | !near [R]")
            elif cmd == "flights":
                await self.reply(pid, "\n".join(f"{f.n}: {f.callsign}, {f.mission} ({f.loadout})" for f in FLIGHTS))
            elif cmd == "join":
                await self.join(pid, d.get("name"), rest)
            elif cmd == "spec":
                r = await self.call(self.hooks, "force_slot", {"player_id": pid, "side": 0, "slot": ""})
                await self.reply(pid, f"force_slot to spectators: {r}")
            elif cmd == "draw":
                await self.draw_extra(int(rest[0]) if rest else 100)
                await self.reply(pid, f"{self.extra} extra shapes on the map")
            elif cmd == "churn":
                await self.churn(rest[0] if rest else "off")
                await self.reply(pid, f"churn: {'every %g s' % self.churn_s if self.churn_s else 'off'}")
            elif cmd == "clear":
                await self.clear_extra()
                await self.reply(pid, "extra shapes removed")
            elif cmd == "where":
                name = d.get("name")
                walkers = ", ".join(f"{k}: exists={w.get('exists')} {_fmt(w.get('pos'))}"
                                    for k, w in self.walkers.items()) or "none"
                await self.reply(pid, f"last seen in a unit: {_fmt(self.last_pos.get(name))}; pilots on foot: {walkers}")
            elif cmd == "near":
                await self.near(pid, d.get("name"), float(rest[0]) if rest else 300.0)
            else:
                await self.reply(pid, f"unknown command !{cmd}; try !help")
        except (BridgeError, asyncio.TimeoutError, ValueError, IndexError) as e:
            await self.reply(pid, f"!{cmd} failed: {e}")

    def spawn_data(self, pid: int, flight: Flight, mode: str, opts: dict[str, str]) -> dict:
        payload = dcs_payload(HORNET, flight.loadout)
        fuel = float(opts.get("fuel", "0.6"))
        pylons = [{"num": int(n), "CLSID": p["CLSID"]} for n, p in sorted(payload["pylons"].items(), key=lambda i: int(i[0]))]
        return {
            "userId": pid, "nickName": str(pid),
            "airdromeId": AIRDROME_ID, "isAirdrome": True,
            "coalition": "blue", "coalitionId": BLUE_ID,
            "typeLA": HORNET, "category": opts.get("cat", "airplane"),
            "boardNumber": "", "numFuel": fuel * HORNET_FUEL_KG,
            "numFlare": payload["flare"], "numChaff": payload["chaff"], "gun": 100, "ammoType": 1,
            "liveryEntry": "", "isHot": mode in ("hot", "air"), "isInAir": mode == "air",
            "localizedAirbaseName": AIRBASE, "localizedUnitTypeName": "F/A-18C",
            "pylon_list": pylons,
        }

    async def join(self, pid: int, name: str | None, rest: list[str]) -> None:
        if not rest:
            await self.reply(pid, "usage: !join N [cold|hot|air] [cat=airplane] [fuel=0.6]")
            return
        flight = next((f for f in FLIGHTS if f.n == int(rest[0])), None)
        if flight is None:
            await self.reply(pid, f"no flight {rest[0]}; try !flights")
            return
        mode = next((w for w in rest[1:] if w in ("cold", "hot", "air")), "cold")
        opts = dict(w.split("=", 1) for w in rest[1:] if "=" in w)
        spawn = self.spawn_data(pid, flight, mode, opts)
        print(f"JOIN    {name} (id {pid}) -> {flight.callsign} {mode}: {len(spawn['pylon_list'])} pylons, "
              f"fuel {spawn['numFuel']:.0f} kg, category {spawn['category']}", flush=True)
        t0 = time.monotonic()
        r = await self.call(self.hooks, "spawn_for", {"player_id": pid, "spawn": spawn})
        print(f"JOIN    create_client_aircraft returned {r} in {1000 * (time.monotonic() - t0):.0f} ms; "
              "watch for HOOKS player_slot and EVENT birth", flush=True)
        await self.reply(pid, f"{flight.callsign} ({mode}): server asked DCS to spawn you; DCS said {r.get('result')!r}")

    async def near(self, pid: int, name: str | None, radius: float) -> None:
        center = self.last_pos.get(name) or next((w.get("pos") for w in self.walkers.values() if w.get("pos")), None)
        if center is None:
            await self.reply(pid, "I don't know where you are yet")
            return
        r = await self.call(self.mission, "query", {"what": "objects", "center": center, "radius": radius, "limit": 30})
        found = r.get("found") or []
        for o in found:
            print(f"NEAR    {o.get('category')} {o.get('type')} {o.get('name')} id={o.get('id')} {_fmt(o.get('pos'))}",
                  flush=True)
        kinds = sorted({f"{o.get('category')}:{o.get('type')}" for o in found})
        await self.reply(pid, f"{len(found)} objects within {radius:.0f} m: {', '.join(kinds)[:300]}")

    async def check_reach(self, w: dict) -> None:
        pos = w["pos"]
        for f in FLIGHTS:
            if not f.pos or math.dist((pos["x"], pos["y"]), (f.pos["x"], f.pos["y"])) > REACH_M:
                continue
            key = f"{w['key']}:{f.n}"
            if key in self.joined_by_walk:
                return
            self.joined_by_walk.add(key)
            frm = w.get("from") or {}
            name = frm.get("player")
            print(f"REACH   pilot on foot {w['key']} ({name or 'unknown player'}) reached {f.callsign}'s jet", flush=True)
            players = await self.call(self.hooks, "players", {})
            pid = next((p["id"] for p in players if name and p.get("name") == name), None)
            if pid is None:
                print("REACH   can't tell which player this is; not joining", flush=True)
                return
            await self.reply(pid, f"You walked up to {f.callsign}'s jet. Strapping you in...")
            r = await self.call(self.hooks, "spawn_for", {"player_id": pid, "spawn": self.spawn_data(pid, f, "cold", {})})
            print(f"REACH   create_client_aircraft for {name} returned {r}", flush=True)
            return

    # ---------- F10 capacity ----------

    def extra_shape(self, i: int, center: dict) -> dict:
        rng = random.Random(i * 7919 + int(time.monotonic() * 10) % 1000)
        x = center["x"] + rng.uniform(-30, 30) * 1000
        y = center["y"] + rng.uniform(-30, 30) * 1000
        sid = EXTRA_BASE_ID + i
        kind = i % 3
        if kind == 0:
            return {"id": sid, "audience": "all", "shape": "circle", "center": {"x": x, "y": y},
                    "radius": rng.uniform(2, 10) * 1000, "color": [1, 0.3, 0, 0.8], "fill": [1, 0.3, 0, 0.05]}
        if kind == 1:
            return {"id": sid, "audience": "all", "shape": "line", "from": {"x": x, "y": y},
                    "to": {"x": x + rng.uniform(-5, 5) * 1000, "y": y + rng.uniform(-5, 5) * 1000},
                    "color": [0.2, 0.4, 1, 1]}
        return {"id": sid, "audience": "all", "shape": "text", "pos": {"x": x, "y": y},
                "text": f"BN {i}", "font_size": 10, "color": [1, 1, 1, 1], "fill": [0, 0, 0, 0.4]}

    async def center(self) -> dict:
        ab = await self.call(self.mission, "query", {"what": "airbase", "airbase": AIRBASE})
        return ab["pos"]

    async def draw_range(self, start: int, stop: int) -> tuple[int, float]:
        c = await self.center()
        drawn, ms = 0, 0.0
        for a in range(start, stop, 200):
            items = [self.extra_shape(i, c) for i in range(a, min(stop, a + 200))]
            r = await self.call(self.mission, "draw_batch", {"items": items}, timeout=60)
            drawn += r.get("drawn", 0)
            ms += r.get("ms", 0.0)
        return drawn, ms

    async def draw_extra(self, n: int) -> None:
        n = max(0, min(n, 5000))
        if n > self.extra:
            drawn, ms = await self.draw_range(self.extra, n)
            print(f"DRAW    +{drawn} shapes in {ms:.0f} ms of DCS time; {n} extra on the map", flush=True)
        elif n < self.extra:
            await self.call(self.mission, "undraw", {"ids": [EXTRA_BASE_ID + i for i in range(n, self.extra)]})
            print(f"DRAW    removed {self.extra - n}; {n} extra on the map", flush=True)
        self.extra = n

    async def clear_extra(self) -> None:
        await self.churn("off")
        await self.draw_extra(0)

    async def churn(self, arg: str) -> None:
        if self.churn_task:
            self.churn_task.cancel()
            self.churn_task = None
        self.churn_s = 0.0 if arg == "off" else max(1.0, float(arg))
        if self.churn_s:
            self.churn_task = asyncio.create_task(self.churn_loop())

    async def churn_loop(self) -> None:
        while True:
            await asyncio.sleep(self.churn_s)
            try:
                drawn, ms = await self.draw_range(0, self.extra)
                print(f"CHURN   redrew {drawn} shapes in {ms:.0f} ms of DCS time", flush=True)
            except (BridgeError, asyncio.TimeoutError) as e:
                print(f"CHURN   failed: {e}", flush=True)


def _fmt(pos: dict | None) -> str:
    return "?" if not pos else f"({pos['x']:.0f}, {pos['y']:.0f})"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7341)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    probe = Probe(args)
    bridge = Bridge(probe.on_message, probe.on_hello, probe.on_disconnect, port=args.port)
    await bridge.start()
    print(f"listening on 127.0.0.1:{bridge.port}; load campaign-slots.miz on the server and join it", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
