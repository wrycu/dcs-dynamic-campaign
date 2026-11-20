"""Proof P19, part 1: the no-mod pieces of a briefing room in DCS. Run on the dedicated server
with campaign-slots.miz (dynamic slots at Khalkhalah), and join it from your own DCS.

    python3 -m tools.briefing_probe

Everything is driven by chat commands (type them in the chat window, starting with "-"; the
other players don't see them):

    -help                  this list
    -flights               the flights on offer (each has a parked Hornet at Khalkhalah)
    -join N [cold|hot|air] [cat=airplane] [fuel=0.6]
                           ask the server to spawn you into flight N's jet with its loadout
                           (DCS.create_client_aircraft for you, as the slot screen does)
    -spec                  send yourself back to spectators (net.force_player_slot)
    -draw N                draw N extra shapes on the F10 map (everyone sees them)
    -churn S | off         redraw all the extra shapes every S seconds
    -clear                 remove the extra shapes
    -where                 what the server knows about your position, and pilots on foot
    -near [R]              objects within R m (default 300) of where you last were

What it finds out:
1. What a spectator sees on F10: the probe draws a few shapes for everyone and a few for blue
   only.
2. Whether chat commands reach the server from spectators and from a cockpit.
3. Whether the server can spawn a player into a jet with a loadout it chose, on the ramp and in
   the air.
4. What the F10 map can carry: server frame times (FRAME lines every 10 s) while you add and
   churn shapes; watch your own frame rate too.
5. Walking: climb out of a parked jet (LCtrl+E three times) and walk. Does the server see you
   (WALKER lines)? Walk up to any parked Hornet: within 15 m the probe tries to put you
   in that flight.
6. Combined Arms: take a blue Tactical Commander or JTAC/Operator slot, jump into the "Walker"
   Humvee and drive it to a parked Hornet (SOLDIER and REACH lines); the probe moves you into
   the Ramp slot.
7. Windows (needs the client script, version 2):
     -window       a campaign window (dxgui) listing the flights; its "Ramp" button puts you in
                   the Ramp jet. Ctrl+Shift+F9 shows or hides it.
     -web [URL]    a web page inside DCS (DCS's own Chromium widget): the probe's test page, or URL.
   With --web-port N the probe also serves the test page over HTTP (try -web http://SERVER:N/).
   CLICK lines are buttons pressed; WEB lines say how the window and page got on.
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
EXTRA_BASE_ID = 900_000  # engine shape ids for -draw
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


SERVER_PLAYER_ID = 1   # a dedicated server is always player 1
# Walking (part 4): a pilot on foot who comes within REACH_M of any parked Hornet is moved into this
# mission Client slot (the "Ramp" Hornet from build_test_missions, unit id 3) and, with the client
# script, spawned into it. Run 14d: a mission slot is the join that works; dynamic slots don't.
WALK_SLOT = "3"
EJECTION_MATCH_M = 3000.0   # a walker is the player who ejected within this distance
OWN_JET_M = 30.0            # jets this close to where the player ejected are their own; ignored
# Combined Arms (part 4, Run 14g): the blue unit "Walker" from build_test_missions (an armed Humvee:
# Combined Arms can't occupy infantry). Unlike an ejected pilot, a unit a player controls is a
# mission unit, so the server should know where it is.
SOLDIER_GROUP = "Walker"
SOLDIER_POLL_S = 2.0
CA_SLOT_HINTS = ("artillery_commander", "forward_observer")   # Combined Arms slot ids contain these
INFANTRY_PREFIX = "Inf-"    # the row of soldier types next to Walker (build_test_missions.INFANTRY_TYPES)
INFANTRY_RADIUS_M = 150.0

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
    slot_waiters: dict[int, asyncio.Future] = field(default_factory=dict)   # player id -> next slot
    client_scripts: set[int] = field(default_factory=set)   # player ids that said client_hello
    ejections: list[tuple[str, dict, float]] = field(default_factory=list)   # (player, pilot pos, when)
    walker_owner: dict[str, str] = field(default_factory=dict)   # walker key -> player name
    soldier_task: asyncio.Task | None = None

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
            if d.get("player_id") == SERVER_PLAYER_ID:   # the server's own chat (our replies), never a command
                return
            print(f"CHAT    {d.get('name')} (id {d.get('player_id')}, side {d.get('side')}, slot {d.get('slot')!r}): "
                  f"-{d.get('text')}", flush=True)
            asyncio.create_task(self.command(d))
        elif kind == "client_hello":
            self.client_scripts.add(d.get("player_id"))
            print(f"CLIENT  player id {d.get('player_id')} has the client script (version {d.get('version')})",
                  flush=True)
        elif kind == "client_click":
            print(f"CLICK   {d.get('name')} (id {d.get('player_id')}, slot {d.get('slot')!r}): {d.get('key')}", flush=True)
            asyncio.create_task(self.click(d))
        elif kind == "client_web":
            print(f"WEB     player id {d.get('player_id')}: {d.get('stage')} {d.get('detail') or ''}", flush=True)
        elif kind == "frame_stats":
            print(f"FRAME   server avg {d['avg_ms']:.1f} ms, max {d['max_ms']:.0f} ms, {d['over_50ms']} frames over "
                  f"50 ms in {d['seconds']:.0f} s; {self.extra} extra shapes"
                  f"{f', churn every {self.churn_s:g} s' if self.churn_s else ''}", flush=True)
        elif kind in ("player_slot", "slot_try", "player_connect", "player_disconnect"):
            e = d.get("entry") or {}
            print(f"HOOKS   {kind} {d.get('name')} id {d.get('player_id')} side {d.get('side')} slot "
                  f"{d.get('slot')!r} {d.get('unit_type') or ''} {e.get('groupName') or ''}", flush=True)
            waiter = self.slot_waiters.get(d.get("player_id"))
            if kind == "player_slot" and d.get("slot") and waiter and not waiter.done():
                waiter.set_result((d.get("side"), str(d["slot"])))
        elif kind in ("birth", "ejection", "landing_after_ejection", "player_enter_unit", "player_leave_unit",
                      "unit_removed", "pilot_dead", "takeoff", "land"):
            u = d.get("unit") or {}
            extra = f" pilot={d['pilot']}" if d.get("pilot") else ""
            if kind == "ejection" and u.get("player") and (d.get("pilot") or {}).get("pos"):
                self.ejections.append((u["player"], d["pilot"]["pos"], time.monotonic()))
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
                owner = self.owner_of(w)
                print(f"WALKER  {key} exists={w.get('exists')} {w.get('type') or ''} cat={w.get('category')} "
                      f"pos={_fmt(w.get('pos'))} player {owner or '?'}", flush=True)
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
            # Dynamic slots need stock; the mission ships with none (Run 14). Also lets -join's
            # create_client_aircraft take a Hornet from the base, as the slot screen does.
            await self.call(self.mission, "warehouse_set", {"airbase": AIRBASE, "aircraft": {HORNET: 4}})
            print(f"STOCK   {AIRBASE}: {HORNET} x4", flush=True)
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
            await self.call(self.mission, "draw", {"id": FIXED_IDS[11], "audience": "coalition:blue", "shape": "text",
                                                   "pos": {"x": x - 1 * NM, "y": y},
                                                   "text": "P19: drawn for BLUE only", "font_size": 16,
                                                   "color": [0.4, 0.6, 1, 1], "fill": [0, 0, 0, 0.6]})
            await self.call(self.mission, "draw", {"id": FIXED_IDS[12], "audience": "all", "shape": "poly",
                                                   "points": [{"x": x + 4 * NM, "y": y - 10 * NM},
                                                              {"x": x + 6 * NM, "y": y},
                                                              {"x": x + 4 * NM, "y": y + 10 * NM}],
                                                   "color": [1, 0, 0, 1], "fill": [1, 0, 0, 0.15]})
            await self.call(self.mission, "message", {"audience": "all", "duration": 30, "text":
                            "P19 briefing room probe. Type -help in chat (it won't show to others)."})
            print("SETUP   done: 3 flights parked, labels drawn (ALL and BLUE-only near Khalkhalah)", flush=True)
            if self.soldier_task is None or self.soldier_task.done():
                self.soldier_task = asyncio.create_task(self.follow_soldier())
        except (BridgeError, asyncio.TimeoutError, KeyError) as e:
            print(f"SETUP   failed: {e}", flush=True)

    # ---------- chat commands ----------

    async def reply(self, player_id: int, text: str) -> None:
        print(f"REPLY   to {player_id}: {text}", flush=True)
        for line in text.split("\n"):
            try:
                # "> " so a reply never starts with the command prefix: the server's own chat goes
                # through the same hook, and Run 14 looped on "-draw failed: ...".
                await self.call(self.hooks, "chat_to", {"player_id": player_id, "text": "> " + line})
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
                await self.reply(pid, "-flights | -join N [cold|hot|air] [nopush|client] | -enter | -spec [side] | -draw N | -churn S|off | "
                                      "-clear | -where | -near [R] | -slot ID [side] | -window | -web [URL]")
            elif cmd == "flights":
                await self.reply(pid, "\n".join(f"{f.n}: {f.callsign}, {f.mission} ({f.loadout})" for f in FLIGHTS))
            elif cmd == "join":
                await self.join(pid, d.get("name"), rest)
            elif cmd == "slot":
                side = int(rest[1]) if len(rest) > 1 else BLUE_ID
                r = await self.call(self.hooks, "force_slot", {"player_id": pid, "side": side, "slot": rest[0]})
                await self.reply(pid, f"force_slot to side {side} slot {rest[0]}: {r}")
            elif cmd == "enter":
                # Part 3: the client script calls net.spawn_player(), to enter a slot the server gave us.
                await self.call(self.hooks, "client_rpc", {"player_id": pid, "method": "campaignEnter", "args": []})
                await self.reply(pid, "asked your client script to enter your slot"
                                      + ("" if pid in self.client_scripts else " (no client_hello from you yet)"))
            elif cmd == "window":
                await self.call(self.hooks, "client_rpc", {"player_id": pid, "method": "campaignWindow",
                                                           "args": [window_spec()]})
                await self.reply(pid, "sent a campaign window (Ctrl+Shift+F9 shows or hides it)"
                                      + ("" if pid in self.client_scripts else " (no client_hello from you yet)"))
            elif cmd == "web":
                arg = {"url": rest[0]} if rest else {"html": test_page("sent over RPC")}
                await self.call(self.hooks, "client_rpc", {"player_id": pid, "method": "campaignWeb", "args": [arg]})
                await self.reply(pid, f"sent a web page ({rest[0] if rest else 'the test page'})"
                                      + ("" if pid in self.client_scripts else " (no client_hello from you yet)"))
            elif cmd == "spec":
                # Run 14: a push into a slot only took when the player was a BLUE spectator, so stay blue
                # unless asked (-spec 0 = neutral).
                side = int(rest[0]) if rest else BLUE_ID
                r = await self.call(self.hooks, "force_slot", {"player_id": pid, "side": side, "slot": ""})
                await self.reply(pid, f"force_slot to side {side} spectators: {r}")
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
                await self.reply(pid, f"unknown command -{cmd}; try -help")
        except (BridgeError, asyncio.TimeoutError, ValueError, IndexError) as e:
            await self.reply(pid, f"-{cmd} failed: {e}")

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
            await self.reply(pid, "usage: -join N [cold|hot|air] [nopush|client] [cat=airplane] [fuel=0.6]")
            return
        flight = next((f for f in FLIGHTS if f.n == int(rest[0])), None)
        if flight is None:
            await self.reply(pid, f"no flight {rest[0]}; try -flights")
            return
        mode = next((w for w in rest[1:] if w in ("cold", "hot", "air")), "cold")
        opts = dict(w.split("=", 1) for w in rest[1:] if "=" in w)
        spawn = self.spawn_data(pid, flight, mode, opts)
        print(f"JOIN    {name} (id {pid}) -> {flight.callsign} {mode}: {len(spawn['pylon_list'])} pylons, "
              f"fuel {spawn['numFuel']:.0f} kg, category {spawn['category']}", flush=True)
        if "client" in rest:
            # Part 3: the player's own DCS spawns and enters, as its slot screen does.
            await self.call(self.hooks, "client_rpc", {"player_id": pid, "method": "campaignJoin", "args": [spawn]})
            print(f"JOIN    asked {name}'s client script to spawn {flight.callsign}", flush=True)
            await self.reply(pid, f"{flight.callsign} ({mode}): asked your client script to spawn you"
                                  + ("" if pid in self.client_scripts else " (no client_hello from you yet)"))
            return
        waiter = self.slot_waiters[pid] = asyncio.get_running_loop().create_future()
        t0 = time.monotonic()
        r = await self.call(self.hooks, "spawn_for", {"player_id": pid, "spawn": spawn})
        print(f"JOIN    create_client_aircraft returned {r} in {1000 * (time.monotonic() - t0):.0f} ms; "
              "watch for HOOKS player_slot and EVENT birth", flush=True)
        await self.reply(pid, f"{flight.callsign} ({mode}): server asked DCS to spawn you; DCS said {r.get('result')!r}")
        # Run 14: DCS said the player occupied the new aircraft, but the client never moved into it.
        # So, unless told not to, push the client into the slot DCS reports, as the server.
        try:
            side, slot = await asyncio.wait_for(waiter, 5)
        except asyncio.TimeoutError:
            await self.reply(pid, "no player_slot from DCS within 5 s; nothing to push")
            return
        finally:
            self.slot_waiters.pop(pid, None)
        if "nopush" in rest:
            await self.reply(pid, f"DCS put you in slot {slot} (side {side}); not pushing (-slot {slot} to try)")
            return
        await asyncio.sleep(0.5)
        r = await self.call(self.hooks, "force_slot", {"player_id": pid, "side": BLUE_ID, "slot": slot})
        print(f"JOIN    force_slot {pid} -> side {BLUE_ID} slot {slot}: {r}", flush=True)
        await self.reply(pid, f"pushed you into slot {slot}: {r}")

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

    def owner_of(self, w: dict) -> str | None:
        """Which player a pilot on foot is. The bridge links the parachuting pilot to the jet, but the
        pilot after landing is a new object; take the player who ejected nearest to it."""
        key = w.get("key")
        if key in self.walker_owner:
            return self.walker_owner[key]
        name = (w.get("from") or {}).get("player")
        if not name and w.get("pos"):
            pos = w["pos"]
            near = [(math.dist((pos["x"], pos["y"]), (p["x"], p["y"])), n) for n, p, _ in self.ejections]
            near = [c for c in near if c[0] <= EJECTION_MATCH_M]
            name = min(near)[1] if near else None
        if name:
            self.walker_owner[key] = name
        return name

    async def check_reach(self, w: dict) -> None:
        """Part 4: a pilot on foot within REACH_M of any parked Hornet (a flight's static, or any other)
        is moved into WALK_SLOT and, with the client script, spawned."""
        if w["key"] in self.joined_by_walk or self.mission is None:
            return
        self.joined_by_walk.add(w["key"])   # one check at a time per walker; dropped again below if no jet
        pos = w["pos"]
        try:
            r = await self.call(self.mission, "query", {"what": "objects", "center": pos, "radius": REACH_M, "limit": 10})
        except BridgeError:
            r = {}
        # Ignore the jet the pilot ejected from: they land next to it (Run 14e: sent straight back in).
        name = self.owner_of(w)
        ej = [p for n, p, _ in self.ejections if n == name]
        jets = [o for o in (r.get("found") or []) if o.get("type") == HORNET and o.get("pos")
                and not (ej and math.dist((o["pos"]["x"], o["pos"]["y"]), (ej[-1]["x"], ej[-1]["y"])) <= OWN_JET_M)]
        if not jets:
            self.joined_by_walk.discard(w["key"])
            return
        jet = jets[0]
        label = next((f.callsign for f in FLIGHTS if f.static and f.static == jet.get("name")), None) or jet.get("name")
        print(f"REACH   pilot on foot {w['key']} ({name or 'unknown player'}) reached Hornet {label} "
              f"({jet.get('category')}) at {_fmt(jet.get('pos'))}", flush=True)
        players = await self.call(self.hooks, "players", {})
        pid = next((p["id"] for p in players if name and p.get("name") == name), None)
        if pid is None:
            print("REACH   can't tell which player this is; not joining", flush=True)
            self.joined_by_walk.discard(w["key"])
            return
        await self.reply(pid, f"You walked up to Hornet {label}. Strapping you in (slot {WALK_SLOT})...")
        # A pilot who ejected from WALK_SLOT is still in it, and force_slot into your own slot does
        # nothing (Run 14), so go through blue spectators first.
        r0 = await self.call(self.hooks, "force_slot", {"player_id": pid, "side": BLUE_ID, "slot": ""})
        print(f"REACH   force_slot {name} -> blue spectators: {r0}", flush=True)
        await asyncio.sleep(1)
        r = await self.call(self.hooks, "force_slot", {"player_id": pid, "side": BLUE_ID, "slot": WALK_SLOT})
        print(f"REACH   force_slot {name} -> slot {WALK_SLOT}: {r}", flush=True)
        if pid in self.client_scripts:
            await asyncio.sleep(1)
            await self.call(self.hooks, "client_rpc", {"player_id": pid, "method": "campaignEnter", "args": []})
            print(f"REACH   asked {name}'s client script to enter", flush=True)
        else:
            await self.reply(pid, "No client script: click Fly to get in.")

    async def follow_soldier(self) -> None:
        """Part 4 with Combined Arms: follow the Walker soldier; within REACH_M of a parked Hornet,
        move whoever is controlling it into WALK_SLOT."""
        last: dict | None = None
        last_player: str | None = "?"
        done = False
        home: dict | None = None
        infantry: dict[str, tuple[dict, str | None]] = {}   # unit name -> (pos, player)
        while True:
            await asyncio.sleep(SOLDIER_POLL_S)
            if self.mission is None:
                continue
            if home is not None:
                await self.watch_infantry(home, infantry)
            try:
                g = await self.call(self.mission, "query", {"what": "group", "group": SOLDIER_GROUP}, timeout=5)
                if not g.get("exists"):
                    if last is not None:
                        print(f"SOLDIER {SOLDIER_GROUP} is gone", flush=True)
                        last = None
                    continue
                pos = g["units"][0]["pos"]
                home = home or pos
                near = await self.call(self.mission, "query", {"what": "objects", "center": pos, "radius": REACH_M,
                                                               "limit": 10}, timeout=5)
            except (BridgeError, asyncio.TimeoutError, KeyError, IndexError):
                continue
            found = near.get("found") or []
            me = next((o for o in found if o.get("name") == f"{SOLDIER_GROUP}-1"), {})
            player = me.get("player")
            if last is None or math.dist((pos["x"], pos["y"]), (last["x"], last["y"])) > 3 or player != last_player:
                print(f"SOLDIER {SOLDIER_GROUP} pos={_fmt(pos)} player={player or 'none (getPlayerName)'}", flush=True)
                last, last_player = pos, player
            jets = [o for o in found if o.get("type") == HORNET]
            if not jets:
                done = False   # walked away: allow another join
                continue
            if done:
                continue
            done = True
            jet = jets[0]
            label = next((f.callsign for f in FLIGHTS if f.static and f.static == jet.get("name")), None) or jet.get("name")
            print(f"REACH   soldier {SOLDIER_GROUP} reached Hornet {label} ({jet.get('category')}) at "
                  f"{_fmt(jet.get('pos'))}", flush=True)
            await self.join_from_soldier(player, label)

    async def watch_infantry(self, home: dict, seen: dict[str, tuple[dict, str | None]]) -> None:
        """Which of the soldier types next to Walker a player has taken over, and whether it moves."""
        try:
            r = await self.call(self.mission, "query", {"what": "objects", "center": home,
                                                        "radius": INFANTRY_RADIUS_M, "limit": 60}, timeout=5)
        except (BridgeError, asyncio.TimeoutError):
            return
        for o in r.get("found") or []:
            name = o.get("name") or ""
            if not name.startswith(INFANTRY_PREFIX) or not o.get("pos"):
                continue
            pos, player = o["pos"], o.get("player")
            prev = seen.get(name)
            if prev is None:
                print(f"INFANTRY {name} ({o.get('type')}) is in the mission at {_fmt(pos)}", flush=True)
            elif prev[1] != player or math.dist((pos["x"], pos["y"]), (prev[0]["x"], prev[0]["y"])) > 3:
                print(f"INFANTRY {name} pos={_fmt(pos)} player={player or 'none'}", flush=True)
            else:
                continue
            seen[name] = (pos, player)

    async def join_from_soldier(self, player: str | None, label: str) -> None:
        players = await self.call(self.hooks, "players", {})
        for p in players:
            print(f"REACH   player id {p.get('id')} {p.get('name')} side {p.get('side')} slot {p.get('slot')!r}", flush=True)
        pid = next((p["id"] for p in players if player and p.get("name") == player), None)
        how = "by name"
        if pid is None:
            ca = [p for p in players if any(h in str(p.get("slot") or "") for h in CA_SLOT_HINTS)]
            pid = ca[0]["id"] if len(ca) == 1 else None
            how = "the only player in a Combined Arms slot"
        if pid is None:
            print("REACH   can't tell which player controls the soldier; not joining", flush=True)
            return
        print(f"REACH   soldier is player id {pid} ({how})", flush=True)
        await self.reply(pid, f"You walked up to Hornet {label}. Strapping you in (slot {WALK_SLOT})...")
        r = await self.call(self.hooks, "force_slot", {"player_id": pid, "side": BLUE_ID, "slot": WALK_SLOT})
        print(f"REACH   force_slot {pid} -> slot {WALK_SLOT} straight from Combined Arms: {r}", flush=True)
        if not (r or {}).get("ok"):
            r0 = await self.call(self.hooks, "force_slot", {"player_id": pid, "side": BLUE_ID, "slot": ""})
            await asyncio.sleep(1)
            r = await self.call(self.hooks, "force_slot", {"player_id": pid, "side": BLUE_ID, "slot": WALK_SLOT})
            print(f"REACH   via blue spectators: {r0}, then slot {WALK_SLOT}: {r}", flush=True)
        if pid in self.client_scripts:
            await asyncio.sleep(1)
            await self.call(self.hooks, "client_rpc", {"player_id": pid, "method": "campaignEnter", "args": []})
            print(f"REACH   asked player {pid}'s client script to enter", flush=True)
        else:
            await self.reply(pid, "No client script: click Fly to get in.")

    async def click(self, d: dict) -> None:
        """A button in the client script's window or web page: "go SLOT" puts the player in that slot."""
        pid, words = d.get("player_id"), str(d.get("key") or "").split()
        try:
            if len(words) == 2 and words[0] == "go":
                r = await self.call(self.hooks, "force_slot", {"player_id": pid, "side": BLUE_ID, "slot": words[1]})
                print(f"CLICK   force_slot {pid} -> slot {words[1]}: {r}", flush=True)
                if not (r or {}).get("ok"):
                    await self.call(self.hooks, "force_slot", {"player_id": pid, "side": BLUE_ID, "slot": ""})
                    await asyncio.sleep(1)
                    r = await self.call(self.hooks, "force_slot", {"player_id": pid, "side": BLUE_ID, "slot": words[1]})
                    print(f"CLICK   via blue spectators: {r}", flush=True)
                await asyncio.sleep(1)
                await self.call(self.hooks, "client_rpc", {"player_id": pid, "method": "campaignEnter", "args": []})
            else:
                await self.reply(pid, f"you clicked {d.get('key')!r}")
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"CLICK   failed: {e}", flush=True)

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


def window_spec() -> dict:
    """What -window shows: the flights, and a button into the Ramp jet."""
    return {"title": "Khalkhalah flights",
            "lines": [f"{f.n}  {f.callsign}: {f.mission}" for f in FLIGHTS]
                     + ["", "Buttons go to the server; it answers in chat."],
            "buttons": [{"key": f"go {WALK_SLOT}", "label": "Ramp: F/A-18C, parked cold"},
                        {"key": "hello", "label": "Say hello to the server"}]}


def test_page(how: str) -> str:
    """The -web test page: does it render, run JavaScript, and reach Lua (window.edQuery)?"""
    rows = "".join(f"<li><b>{f.callsign}</b>: {f.mission} <i>({f.loadout})</i></li>" for f in FLIGHTS)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Campaign</title><style>
body {{ background: #1b2633; color: #e8e8e8; font: 16px sans-serif; margin: 16px; }}
h1 {{ font-size: 22px; margin: 0 0 8px; }} button {{ font-size: 16px; margin: 6px 6px 0 0; padding: 6px 12px; }}
#status {{ margin-top: 12px; color: #9fd39f; }} .bad {{ color: #f08080 !important; }}
</style></head><body>
<h1>Campaign test page</h1>
<p>Page {how}. JavaScript: <span id="js" class="bad">not running</span>.
   Lua bridge (window.edQuery): <span id="eq">?</span>. Time: <span id="clock"></span>.</p>
<ul>{rows}</ul>
<button onclick="send('go {WALK_SLOT}')">Fly the Ramp jet (F/A-18C, parked cold)</button>
<button onclick="send('hello from the web page')">Say hello to the server</button>
<button onclick="call('close', {{}})">Close</button>
<div id="status"></div>
<script>
document.getElementById('js').textContent = 'running'; document.getElementById('js').className = '';
document.getElementById('eq').textContent = ('edQuery' in window) ? 'yes' : 'no';
setInterval(function () {{ document.getElementById('clock').textContent = new Date().toLocaleTimeString(); }}, 1000);
function status(t, bad) {{ var s = document.getElementById('status'); s.textContent = t; s.className = bad ? 'bad' : ''; }}
function call(method, parameters) {{
  if (!('edQuery' in window)) {{ status('no window.edQuery: this page cannot reach Lua', true); return; }}
  window.edQuery({{ request: JSON.stringify({{ method: method, parameters: parameters }}), persistent: false,
    onSuccess: function (r) {{ status('Lua answered: ' + r); }},
    onFailure: function (code, msg) {{ status('Lua failed: ' + code + ' ' + msg, true); }} }});
}}
function send(key) {{ call('click', {{ key: key }}); }}
</script></body></html>"""


async def serve_page(port: int) -> None:
    """--web-port: the test page over plain HTTP, to see whether DCS's widget loads a remote page
    and whether such a page still gets window.edQuery."""
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            while (await asyncio.wait_for(reader.readline(), 10)) not in (b"\r\n", b"\n", b""):
                pass
            body = test_page("served over HTTP").encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                         + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
            print(f"HTTP    {writer.get_extra_info('peername')[0]} {line.decode(errors='replace').strip()}", flush=True)
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    print(f"HTTP    test page on port {port}", flush=True)
    async with server:
        await server.serve_forever()


def _fmt(pos: dict | None) -> str:
    return "?" if not pos else f"({pos['x']:.0f}, {pos['y']:.0f})"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7341)
    ap.add_argument("--web-port", type=int, default=0, help="also serve the -web test page over HTTP on this port")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    probe = Probe(args)
    bridge = Bridge(probe.on_message, probe.on_hello, probe.on_disconnect, port=args.port)
    await bridge.start()
    if args.web_port:
        asyncio.create_task(serve_page(args.web_port))
    print(f"listening on 127.0.0.1:{bridge.port}; load campaign-slots.miz on the server and join it", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
