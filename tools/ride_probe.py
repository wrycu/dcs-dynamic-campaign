"""Proof P22: ride to the jets. Blue spectators ride in a Humvee the server drives from a briefing
room spot across the base, along its taxiways, to their parked jets; each is dropped at their own
jet and put in it, and the Humvee drives off. Host from your DCS (MULTIPLAYER > New server), with
client script version 24.

    python3 -m tools.ride_probe

Host any mission that loads the campaign bridge (campaign-dev/campaign-p20.miz from Run 25). The
probe reads Khalkhalah's free parking, picks two open spots side by side that a Hornet fits,
builds campaign-dev/campaign-p22a.miz (or -p22b, whichever isn't loaded) with a two-ship of Client Hornets there ("P22 Ride-1" and
"-2", hot unless --cold) and loads it. Then (Run 30):

  * The jets are visible before anyone is in them: a Client jet isn't in the world until its
    player takes the slot, so a static Hornet stands on each empty spot, removed just before the
    player is put in, and put back when they leave.
  * The ride starts beside the base's tower (or the building nearest the jets), found among the
    scenery near the jets (the SCENERY line lists what's there).
  * The Humvee follows the taxiways: the base's taxiway network from DCS's own files
    (campaign/taxinet.py), keeping off the runway.
  * It stops beside each jet in turn, the nearest next. Riders get out at their own jet; at a jet
    nobody rides to, it stops for a moment all the same (the pilot of an AI jet, in the campaign).
  * It stops beside each jet's nose, side on, coming in along the row of jets and driving straight
    down the row between jets close together (Run 30: driving into each stand to face the nose,
    then looping out, looked wrong).
  * After the last jet it drives on the way it was going if a taxiway goes on ahead (else back the
    way it came, the only way out), and is removed 250 m later, out of sight.

In the mission, pick blue, stay in spectators, and type in chat:

    -ride [N] [seat|chase] [place] [fixed] [KMH]
                               ride to jet N (1 or 2; default the last, so the Humvee stops at the
                               other first). The Humvee waits GATHER_S seconds for others: each
                               -ride in that time boards too (front passenger, then the back
                               seats), each to their own jet. Views as in Run 29: the seat (DCS's
                               camera mounted on the Humvee, F7 and F4 pressed for you) or chase.
    -eye FWD RIGHT UP          the front passenger's seat camera, metres (default 0.2 0.45 1.55); the
    -nudge FWD RIGHT UP        back seats follow it; during a ride it moves at once
    -car TYPE | -cars          the vehicle for the next ride (default Hummer)
    -in [N]                    straight into jet N, no ride
    -stop (or -ride stop)      end the ride, remove the Humvee
    -statics                   put the stand-in Hornets back on every empty spot
    -rebuild                   build and load the mission again (new spots)
    -help                      this list

Printed: TAXI (the network), SCENERY and START (where the ride starts and why), JETS, DRIVE
(each leg, and where the Humvee is), STOP (each jet), ARRIVE (into a jet), STATIC, and RIDE
lines from the client scripts.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
from pathlib import Path
from typing import Any

from campaign import ride as ridemod
from campaign import model_boxes, taxinet
from campaign.ride_starts import RideStarts
from campaign.rides import find_start
from campaign.bridge import Bridge, BridgeError, Connection
from campaign.catalog import COUNTRY_FOR_SIDE
from campaign.model import Pos, Side
from campaign.parking import size_from, stands_from, usable_spots
from campaign.protocol import Envelope, Hello, Src
from tools.build_test_missions import AIRBASE_NAME, AIRDROME_ID, DEFAULT_TEMPLATE, SAVED_GAMES, build
from tools.extract_theater import DCS_DIR

HORNET = "FA-18C_hornet"
HUMVEE = "Hummer"
# Light vehicles to try for the ride (Run 29: the Humvee's passenger figure is where the camera sits).
CARS = ["Hummer", "M1043 HMMWV Armament", "M1045 HMMWV TOW", "P20_drivable", "TugHarlan_drivable", "B600_drivable",
        "r11_volvo_drivable", "Willys_MB", "UAZ-469", "Tigr_233036", "VAZ Car", "tacr2a"]
GROUP = "P22 Ride"
JETS = 2
CAR = "P22 Humvee"
STATIC = "P22 Static"
# Alternating, like the campaign's session missions: DCS won't reload the file that's loaded once it
# has been rewritten under it ("Attempt to restart server with an invalid mission", Run 30).
MISSION_FILES = ("campaign-p22a.miz", "campaign-p22b.miz")
BLUE = 2
OPEN_SPOTS = (104, 72, 68)       # open spots first, shelters last: the drive should end in the open
PAIR_M = (18.0, 80.0)            # the two jets' spots this far apart
GATHER_S = 8.0                   # the Humvee waits this long for riders before it sets off
DROP_S = 6.0                     # stopped at each jet this long
ARRIVE_M = 4.0                   # this close to the end of a leg: there
POS_S = 0.25                     # positions to the client scripts this often
DRIVE_LOG_S = 2.0
STUCK_S = 20.0                   # not moving this long on a leg: stuck
LEG_GIVE_UP_S = 240.0
BACK_SEAT_M = 1.1                # the back seats this far behind the front passenger's
FT, KMH = 0.3048, 1 / 3.6
# The client script's ride progress (client_web, stage "ride") worth telling the player.
RIDE_CHAT = [
    ("native: the view came", "Your view goes to the Humvee: you can look around."),
    ("native: in the seat", "You're in the Humvee: look around as you like."),
    ("native: not at the seat with the world", "DCS's camera stays outside the Humvee (it wouldn't go to the seat)."),
    ("native: the view is elsewhere", "Your view left the Humvee: -ride to start again."),
    ("focus: native", "Getting your view onto the Humvee (F7, then F4, pressed for you)..."),
    ("native: the vehicle isn't", "Can't see the Humvee from your PC: placing the camera instead (F4 pressed for you)."),
    ("focus: mounted at the seat", "You're in the Humvee: look around as you like."),
    ("focus: DCS doesn't keep", "You're in the Humvee (the script holds the camera there)."),
    ("focus: chase view", "Behind the Humvee: look around as you like."),
    ("focus: F7 didn't reach", "Couldn't get the view onto the Humvee: placing the camera in it instead (F4 pressed for you)."),
    ("focus: the view is elsewhere", "Your view left the Humvee: -ride to start again."),
    ("holding:", "You're in the Humvee."),
    ("let go:", "Your view changed, so the ride let go of the camera. -ride to start again."),
]


def dist(a: Pos, b: Pos) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def pos_of(d: dict[str, Any]) -> Pos:
    return Pos(float(d["x"]), float(d["y"]))


class Jet:
    def __init__(self, n: int, name: str, slot: str, pos: Pos, heading: float) -> None:
        self.n, self.name, self.slot, self.pos, self.heading = n, name, slot, pos, heading
        self.static = f"{STATIC} {n}"
        self.static_up = False


class Rider:
    def __init__(self, pid: int, jet: Jet, seat: int, view: str, look: bool, native: bool) -> None:
        self.pid, self.jet, self.seat, self.view, self.look, self.native = pid, jet, seat, view, look, native
        self.dropped = False


class Probe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.mission: Connection | None = None
        self.hooks: Connection | None = None
        self.hooks_up = asyncio.Event()
        self.client_scripts: dict[int, Any] = {}    # player id -> client script version
        self.jets: list[Jet] = []
        self.net: taxinet.TaxiNet | None = None
        self.start: Pos | None = None
        self.eye = [0.2, 0.45, 1.55]
        self.car_type = HUMVEE
        self.riders: dict[int, Rider] = {}           # on the ride now (boarding or driving)
        self.boarding = False                        # the Humvee is waiting for riders
        self.ride_task: asyncio.Task | None = None
        self.rebuild = args.rebuild
        self.slot_of: dict[int, str] = {}            # player id -> slot, from the Hooks
        self.loaded_file = ""                        # the mission DCS has loaded (Hooks mission_load)

    # ---------- bridge callbacks ----------

    def on_hello(self, conn: Connection, hello: Hello, new_run: bool) -> None:
        print(f"HELLO   {conn.src} new_run={new_run}", flush=True)
        if conn.src == Src.MISSION:
            self.mission = conn
            self.jets = []
            asyncio.create_task(self.mission_up())
        elif conn.src == Src.HOOKS:
            self.hooks = conn
            self.hooks_up.set()

    def on_disconnect(self, conn: Connection) -> None:
        print(f"DISCONNECT {conn.src}", flush=True)
        if conn is self.mission:
            self.mission = None
            if self.ride_task and not self.ride_task.done():
                self.ride_task.cancel()
        if conn is self.hooks:
            self.hooks = None
            self.hooks_up.clear()

    def on_message(self, conn: Connection, env: Envelope) -> None:
        d = env.data or {}
        if env.type not in ("event", "player"):
            return
        kind = d.get("kind")
        if kind == "chat_command":
            print(f"CHAT    {d.get('name')} (id {d.get('player_id')}, side {d.get('side')}, slot {d.get('slot')!r}): "
                  f"-{d.get('text')}", flush=True)
            asyncio.create_task(self.command(d))
        elif kind == "client_hello":
            self.client_scripts[d.get("player_id")] = d.get("version")
            print(f"CLIENT  player id {d.get('player_id')} has the client script (version {d.get('version')})", flush=True)
        elif kind == "client_web" and d.get("stage") == "ride":
            detail = str(d.get("detail") or "")
            print(f"RIDE    player id {d.get('player_id')}: {detail}", flush=True)
            text = next((t for prefix, t in RIDE_CHAT if detail.startswith(prefix)), None)
            if text and d.get("player_id") is not None:
                asyncio.create_task(self.reply(int(d["player_id"]), text))
        elif kind == "mission_load":
            if d.get("file"):
                self.loaded_file = str(d["file"])
            print(f"HOOKS   mission_load {d.get('stage')} {d.get('file') or ''}", flush=True)
        elif kind in ("player_slot", "slot_try"):
            print(f"HOOKS   {kind} {d.get('name')} id {d.get('player_id')} side {d.get('side')} slot {d.get('slot')!r}",
                  flush=True)
            if kind == "player_slot" and d.get("player_id") is not None:
                old = self.slot_of.get(d["player_id"])
                self.slot_of[d["player_id"]] = str(d.get("slot") or "")
                left = next((j for j in self.jets if j.slot == old), None)
                if left is not None:   # out of a jet: its stand-in comes back once the jet's gone
                    asyncio.create_task(self.restore_static(left, delay=3.0))
        elif kind in ("birth", "player_enter_unit", "engine_startup"):
            u = d.get("unit") or {}
            if str(u.get("name") or "").startswith("P22"):
                print(f"EVENT   {kind} {u.get('player') or ''} {u.get('type') or ''} {u.get('name')}", flush=True)
                jet = next((j for j in self.jets if j.name == u.get("name")), None)
                if kind == "birth" and jet is not None:
                    asyncio.create_task(self.check_heading(jet))

    async def call(self, conn: Connection | None, op: str, data: dict, timeout: float = 30) -> Any:
        if conn is None:
            raise BridgeError(f"{op}: not connected")
        return await conn.call(op, data, timeout=timeout)

    async def say(self, text: str, duration: int = 20) -> None:
        print(f"SAY     {text}", flush=True)
        try:
            await self.call(self.mission, "message", {"audience": "all", "text": text, "duration": duration})
        except (BridgeError, asyncio.TimeoutError):
            pass

    async def reply(self, pid: int, text: str) -> None:
        print(f"REPLY   to {pid}: {text}", flush=True)
        try:   # "> ": a reply never starts with the command prefix (Run 14)
            await self.call(self.hooks, "chat_to", {"player_id": pid, "text": "> " + text})
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"REPLY   failed: {e}", flush=True)

    async def tell_riders(self, text: str) -> None:
        for r in list(self.riders.values()):
            if not r.dropped:
                await self.reply(r.pid, text)

    async def rpc(self, pid: int, method: str, *args: Any) -> None:
        await self.call(self.hooks, "client_rpc", {"player_id": pid, "method": method, "args": list(args)})

    # ---------- the mission ----------

    async def theatre(self) -> str:
        try:
            info = await self.call(self.mission, "query", {"what": "mission"})
            return str(info.get("theatre") or "Syria")
        except (BridgeError, asyncio.TimeoutError):
            return "Syria"

    async def load_net(self) -> None:
        theatre = await self.theatre()
        try:
            self.net = taxinet.load(DCS_DIR, theatre, AIRBASE_NAME)
        except (OSError, ValueError) as e:
            self.net = None
            print(f"TAXI    can't read {taxinet.taxiways_file(DCS_DIR, theatre, AIRBASE_NAME)}: {e}", flush=True)
            return
        if self.net is None:
            print(f"TAXI    no taxiway file {taxinet.taxiways_file(DCS_DIR, theatre, AIRBASE_NAME)}: driving straight",
                  flush=True)
        else:
            print(f"TAXI    {AIRBASE_NAME}: {len(self.net.edges)} taxiway and runway segments, {len(self.net.nodes)} "
                  f"junctions, {sum(e.length for e in self.net.edges) / 1000:.1f} km", flush=True)

    async def mission_up(self) -> None:
        try:
            res = await self.call(self.mission, "query", {"what": "client_groups"})
            group = next((g for g in res.get("groups") or [] if g.get("name") == GROUP), None)
            units = sorted((group or {}).get("units") or [], key=lambda u: str(u.get("name")))
            if len(units) < JETS or self.rebuild or any(u.get("x") is None for u in units):
                self.rebuild = False
                await self.build_and_load()
                return
            await self.load_net()
            self.jets = [Jet(i, str(u["name"]), str(u["id"]), pos_of(u), float(u.get("heading") or 0))
                         for i, u in enumerate(units, 1)]
            for j in self.jets:
                h = ridemod.parked_heading(self.net, j.pos)
                print(f"JETS    {j.n}: {j.name}, slot {j.slot}, x {j.pos.x:.0f} y {j.pos.y:.0f}; heading in the .miz "
                      f"{math.degrees(j.heading):.0f}, towards its taxiway "
                      f"{'?' if h is None else f'{math.degrees(h):.0f}'}", flush=True)
                if h is not None:
                    j.heading = h
            await self.find_start()
            for j in self.jets:
                await self.restore_static(j)
            await self.say("P22 ride probe. Pick blue, stay in spectators, and type -ride in chat (-ride 1 or -ride 2 "
                           "for your jet). -help for the rest.", 30)
        except (BridgeError, asyncio.TimeoutError, KeyError, TypeError, ValueError) as e:
            print(f"MISSION setup failed: {e}", flush=True)

    async def find_start(self) -> None:
        """Beside the tower, or the building nearest the jets, clear of everything (campaign/rides.py)."""
        async def call(op: str, data: dict) -> Any:
            return await self.call(self.mission, op, data)
        theatre = await self.theatre()
        mapped = RideStarts().at(theatre, AIRBASE_NAME)
        boxes = model_boxes.load(model_boxes.path_for(theatre))
        self.start, _, note = await find_start(call, self.net, [j.pos for j in self.jets], mapped, boxes)
        print(f"START   {note}", flush=True)

    async def build_and_load(self) -> None:
        try:
            await asyncio.wait_for(self.hooks_up.wait(), 30)
        except asyncio.TimeoutError:
            print("BUILD   no Hooks connection in 30 s: are campaign_hooks.lua installed and DCS restarted?", flush=True)
            return
        ab = await self.call(self.mission, "query", {"what": "airbase", "airbase": AIRBASE_NAME})
        if not ab.get("exists"):
            print(f"BUILD   this mission has no {AIRBASE_NAME} (Syria): host a Syria mission with the bridge", flush=True)
            return
        await self.load_net()
        spots = (await self.call(self.mission, "query", {"what": "parking", "airbase": AIRBASE_NAME, "free_only": True}))
        stands = stands_from(await self.call(self.hooks, "stands", {"airdrome_id": int(ab["id"])}))
        size = size_from(await self.call(self.mission, "query", {"what": "type_size", "type": HORNET}))
        all_spots = spots.get("spots") or []
        fit = [s for s in usable_spots(all_spots, stands, size) if s["type"] in OPEN_SPOTS[:2]] or \
            usable_spots(all_spots, stands, size)
        fit.sort(key=lambda s: (OPEN_SPOTS.index(s["type"]) if s["type"] in OPEN_SPOTS else 9, s.get("dist_rw") or 0))
        pair = None
        for a in fit:
            others = [b for b in fit if b is not a and PAIR_M[0] <= dist(pos_of(a["pos"]), pos_of(b["pos"])) <= PAIR_M[1]]
            if others:
                pair = [a, min(others, key=lambda b: dist(pos_of(a["pos"]), pos_of(b["pos"])))]
                break
        if pair is None:
            print(f"BUILD   no two free spots side by side at {AIRBASE_NAME} that a Hornet fits", flush=True)
            return
        units = []
        for i, s in enumerate(pair, 1):
            p = pos_of(s["pos"])
            h = ridemod.parked_heading(self.net, p) or 0.0
            units.append({"name": f"{GROUP}-{i}", "skill": "Client", "type": HORNET, "x": p.x, "y": p.y,
                          "heading": h, "parking": int(s["term"])})
            print(f"BUILD   jet {i} at term {s['term']} (type {s['type']}), x {p.x:.0f} y {p.y:.0f}, heading "
                  f"{math.degrees(h):.0f}", flush=True)
        group = {"name": GROUP, "airdrome_id": int(ab["id"]), "x": ab["pos"]["x"], "y": ab["pos"]["y"],
                 "start": "cold" if self.args.cold else "hot",
                 "waypoints": [{"x": ab["pos"]["x"] + 20_000, "y": ab["pos"]["y"], "alt": 15_000 * FT}], "units": units}
        out = self.args.missions_dir
        loaded = self.loaded_file.replace("\\", "/").rsplit("/", 1)[-1].lower()
        name = next(f for f in MISSION_FILES if f != loaded)
        path = await asyncio.to_thread(build, self.args.template, out, name, "lua/mission/campaign_bridge.lua",
                                       "", "Load campaign bridge", None, True, None,
                                       {"client_groups": [group], "drop_players": True}, None, AIRDROME_ID, False)
        try:
            rel = "\\".join(path.resolve().relative_to((SAVED_GAMES / "Missions").resolve()).parts)
        except ValueError:
            print(f"BUILD   {path} isn't under {SAVED_GAMES / 'Missions'}: load it yourself", flush=True)
            return
        await self.say(f"P22: built {name}. Loading it in 10 s.", 12)
        await self.call(self.hooks, "load_mission", {"file": rel, "delay": 10})
        print(f"BUILD   {path}; loading {rel}", flush=True)

    # ---------- the stand-in jets ----------

    def occupied(self, jet: Jet) -> bool:
        return jet.slot in self.slot_of.values()

    async def restore_static(self, jet: Jet, delay: float = 0.0) -> None:
        """A static Hornet on the jet's spot while nobody's in it."""
        if delay:
            await asyncio.sleep(delay)
        if self.occupied(jet) or self.mission is None:
            return
        try:
            await self.call(self.mission, "remove_static", {"name": jet.static})
            await self.call(self.mission, "spawn_static", {"country": COUNTRY_FOR_SIDE[Side.BLUE], "static": {
                "name": jet.static, "type": HORNET, "category": "Planes", "x": jet.pos.x, "y": jet.pos.y,
                "heading": jet.heading, "livery_id": ""}})
            jet.static_up = True
            print(f"STATIC  stand-in Hornet on jet {jet.n}'s spot, heading {math.degrees(jet.heading):.0f}", flush=True)
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"STATIC  jet {jet.n}: {e}", flush=True)

    async def remove_static(self, jet: Jet) -> None:
        try:
            r = await self.call(self.mission, "remove_static", {"name": jet.static})
            jet.static_up = False
            print(f"STATIC  jet {jet.n}'s stand-in removed: {r}", flush=True)
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"STATIC  removing jet {jet.n}'s: {e}", flush=True)

    async def check_heading(self, jet: Jet) -> None:
        """The real jet's heading, against the stand-in's (a stand-in turned wrong would show as the
        jet swinging round when its player gets in)."""
        await asyncio.sleep(1.0)
        try:
            g = await self.call(self.mission, "query", {"what": "group", "group": GROUP})
            u = next((u for u in g.get("units") or [] if u.get("name") == jet.name), None)
            if u is None:
                return
            real = float(u.get("hdg") or 0.0)
            off = (real - math.degrees(jet.heading) + 180) % 360 - 180
            moved = dist(pos_of(u["pos"]), jet.pos)
            print(f"STATIC  jet {jet.n} spawned heading {real:.0f} (stand-in {math.degrees(jet.heading):.0f}, "
                  f"{off:+.0f} deg), {moved:.1f} m from its spot", flush=True)
            jet.heading = math.radians(real)   # later stand-ins match it
        except (BridgeError, asyncio.TimeoutError, KeyError, TypeError, ValueError) as e:
            print(f"STATIC  jet {jet.n} heading check: {e}", flush=True)

    # ---------- chat ----------

    async def command(self, d: dict) -> None:
        pid = d.get("player_id")
        words = (d.get("text") or "").split()
        if not words or pid is None:
            return
        cmd, rest = words[0].lower(), words[1:]
        try:
            if cmd == "help":
                await self.reply(pid, "-ride [N] [seat|chase] [place] [fixed] [KMH] | -eye/-nudge FWD RIGHT UP | "
                                      "-car TYPE | -cars | -in [N] | -stop | -statics | -rebuild")
            elif cmd == "ride" and any(w.lstrip("-") == "stop" for w in rest):
                await self.command({**d, "text": "stop"})
            elif cmd == "ride":
                await self.board(pid, d, rest)
            elif cmd in ("eye", "nudge"):
                vals = [float(w) for w in rest[:3]]
                if cmd == "eye":
                    self.eye = vals + self.eye[len(vals):]
                else:
                    self.eye = [round(e + (vals[i] if i < len(vals) else 0.0), 3) for i, e in enumerate(self.eye)]
                live = [r for r in self.riders.values() if not r.dropped]
                for r in live:
                    await self.rpc(r.pid, "campaignRideEye", self.eye_for(r.seat))
                await self.reply(pid, f"front seat camera: {self.eye[0]:g} m forward, {self.eye[1]:g} m right, "
                                      f"{self.eye[2]:g} m up" + (" (moved now)" if live else " (next ride)"))
            elif cmd == "car":
                if not rest:
                    await self.reply(pid, f"the ride's vehicle is {self.car_type}; -cars lists some")
                else:
                    self.car_type = " ".join(rest)
                    await self.reply(pid, f"next ride in a {self.car_type}"
                                          + ("" if self.car_type in CARS else " (not one I know: DCS may refuse it)"))
            elif cmd == "cars":
                await self.reply(pid, "-car " + " | ".join(CARS))
            elif cmd == "stop":
                if self.ride_task and not self.ride_task.done():
                    self.ride_task.cancel()
                for r in list(self.riders.values()):
                    await self.rpc(r.pid, "campaignRideStop", "-stop")
                self.riders.clear()
                self.boarding = False
                await self.call(self.mission, "despawn_group", {"group": CAR})
                await self.reply(pid, "ride stopped")
            elif cmd == "in":
                jet = self.jet_arg(rest)
                if jet is None:
                    await self.reply(pid, f"no jet {rest[0] if rest else ''} (1 to {len(self.jets)})")
                else:
                    await self.into_jet(pid, jet)
            elif cmd == "statics":
                for j in self.jets:
                    await self.restore_static(j)
                await self.reply(pid, "stand-ins back on the empty spots")
            elif cmd == "rebuild":
                self.rebuild = True
                await self.build_and_load()
        except (BridgeError, asyncio.TimeoutError, ValueError) as e:
            await self.reply(pid, f"-{cmd} failed: {e}")

    def jet_arg(self, rest: list[str]) -> Jet | None:
        n = next((int(w) for w in rest if w.isdigit()), None)
        if n is None:
            return self.jets[-1] if self.jets else None
        return self.jets[n - 1] if 1 <= n <= len(self.jets) else None

    def eye_for(self, seat: int) -> list[float]:
        """Seat 0 is the front passenger's; 1 and 2 the back seats behind them and behind the driver."""
        f, r, u = self.eye
        if seat == 0:
            return [f, r, u]
        return [f - BACK_SEAT_M, r if seat == 1 else -r, u]

    # ---------- the ride ----------

    async def board(self, pid: int, d: dict, rest: list[str]) -> None:
        if not self.jets or self.start is None:
            await self.reply(pid, "the jets aren't ready yet (wait for the probe to load its mission)")
            return
        if str(d.get("slot") or "") not in ("", "0"):   # Run 29: -ride from the jet drove into it
            await self.reply(pid, "You're in a jet: go back to blue spectators first, then -ride")
            return
        if int(d.get("side") or 0) != BLUE:
            await self.reply(pid, "pick blue first (spectators are fine): DCS only moves blue spectators into a jet")
        version = self.client_scripts.get(pid)
        if version is None or int(version) < 24:
            await self.reply(pid, f"your client script is version {version or 'none'}: the ride needs 24")
        jet = self.jet_arg([w for w in rest if w.isdigit() and len(w) == 1])
        if jet is None:
            await self.reply(pid, f"no such jet (1 to {len(self.jets)})")
            return
        if pid in self.riders:
            await self.reply(pid, "you're on the ride already")
            return
        if any(r.jet is jet for r in self.riders.values()):
            await self.reply(pid, f"someone's riding to jet {jet.n} already: -ride with the other number")
            return
        driving = self.ride_task is not None and not self.ride_task.done()
        if driving and not self.boarding:
            await self.reply(pid, "the Humvee has left: wait for it to finish (or -stop), then -ride")
            return
        if len(self.riders) >= 3:
            await self.reply(pid, "the Humvee is full")
            return
        view = next((w for w in rest if w in ("seat", "chase")), "seat")
        r = Rider(pid, jet, len(self.riders), view, "fixed" not in rest, "place" not in rest)
        self.riders[pid] = r
        kmh = next((float(w) for w in rest if w.replace(".", "", 1).isdigit() and len(w) > 1), self.args.kmh)
        if not driving:
            self.boarding = True
            self.ride_task = asyncio.create_task(self.ride(kmh))
            await asyncio.sleep(1.0)   # the Humvee has to exist before the camera goes to it
        await self.rpc(pid, "campaignRide", {"unit": f"{CAR}-1", "view": r.view, "eye": self.eye_for(r.seat),
                                             "chase": [12, 5], "look": r.look, "native": r.native})
        seat = ["front passenger seat", "back seat, right", "back seat, left"][r.seat]
        await self.reply(pid, f"Boarding: {seat}, to jet {jet.n}. The Humvee leaves {GATHER_S:.0f} s after the first "
                              "rider boards; others can -ride with the other jet's number.")

    async def ride(self, kmh: float) -> None:
        try:
            await self._ride(kmh)
        except asyncio.CancelledError:
            print("DRIVE   ride cancelled", flush=True)
            raise
        except (BridgeError, asyncio.TimeoutError, KeyError, TypeError, ValueError) as e:
            print(f"DRIVE   ride failed: {type(e).__name__} {e}", flush=True)
            await self.tell_riders(f"ride failed: {type(e).__name__} {e}")
            try:   # don't leave it parked in front of a jet (Run 30)
                await self.call(self.mission, "despawn_group", {"group": CAR})
            except (BridgeError, asyncio.TimeoutError):
                pass
        finally:
            self.boarding = False
            self.riders.clear()

    async def _ride(self, kmh: float) -> None:
        assert self.start is not None
        legs = ridemod.plan(self.net, self.start, [j.pos for j in self.jets], [j.heading for j in self.jets])
        speed = kmh * KMH
        first = legs[0].points
        hdg = math.atan2(first[1].y - first[0].y, first[1].x - first[0].x) if len(first) > 1 else 0.0
        await self.call(self.mission, "despawn_group", {"group": CAR})
        await self.call(self.mission, "spawn_group", {"category": "ground", "country": COUNTRY_FOR_SIDE[Side.BLUE], "group": {
            "name": CAR, "task": "Ground Nothing",
            "units": [{"name": f"{CAR}-1", "type": self.car_type, "x": self.start.x, "y": self.start.y, "heading": hdg,
                       "skill": "Average", "playerCanDrive": False}],
            "route": {"points": ridemod.waypoints([self.start], speed)}}})
        total = sum(leg.length for leg in legs)
        print(f"DRIVE   {self.car_type} at the start; {len(legs)} legs, {total:.0f} m in all at {kmh:g} km/h: "
              + ", ".join(f"{'jet ' + str(self.jets[leg.jet].n) if leg.jet is not None else 'off'} "
                          f"{leg.length:.0f} m ({len(leg.points)} points)" for leg in legs), flush=True)
        # Boarding: others may join while it waits; positions keep coming for server-side cameras.
        loop = asyncio.get_running_loop()
        until = loop.time() + GATHER_S
        while loop.time() < until:
            await self.send_pos()
            await asyncio.sleep(POS_S)
        self.boarding = False
        if not self.riders:
            print("DRIVE   nobody boarded", flush=True)
        await self.tell_riders(f"Off we go: {total * 3.281:.0f} ft, about {total / speed / 60:.0f} min, "
                               f"stopping at {len(legs) - 1} jets.")
        for i, leg in enumerate(legs, 1):
            what = f"jet {self.jets[leg.jet].n}" if leg.jet is not None else "away"
            await self.retry("set_route", {"group": CAR, "points": ridemod.waypoints(leg.points, speed)})
            print(f"DRIVE   leg {i}/{len(legs)} to {what}: {leg.length:.0f} m", flush=True)
            if not await self.drive_leg(leg, what):
                break
            if leg.jet is not None:
                await self.at_jet(self.jets[leg.jet])
        await self.call(self.mission, "despawn_group", {"group": CAR})
        print("DRIVE   the Humvee is back and gone", flush=True)

    async def retry(self, op: str, data: dict, tries: int = 4) -> Any:
        """A mission command that must get through: DCS can be slow to answer while it spawns a
        player's jet (Run 30: a 5 s timeout there ended the ride, leaving the Humvee parked)."""
        for n in range(tries):
            try:
                return await self.call(self.mission, op, data, timeout=10)
            except asyncio.TimeoutError:
                print(f"DRIVE   {op}: no answer in 10 s (try {n + 1} of {tries})", flush=True)
        raise BridgeError(f"{op}: no answer after {tries} tries")

    async def send_pos(self) -> dict[str, Any] | None:
        """The Humvee's state, also sent to the riders; {} if DCS didn't answer in time, None if it's gone."""
        try:
            g = await self.call(self.mission, "query", {"what": "group", "group": CAR}, timeout=5)
        except asyncio.TimeoutError:
            print("DRIVE   no position in 5 s (DCS busy)", flush=True)
            return {}
        if not g.get("exists") or not g.get("units"):
            return None
        u = g["units"][0]
        for r in list(self.riders.values()):
            if not r.dropped:
                await self.rpc(r.pid, "campaignRidePos", {"x": u["pos"]["x"], "y": u["pos"]["y"], "alt": u.get("alt"),
                                                          "hdg": u.get("hdg"), "spd": u.get("spd")})
        return u

    async def drive_leg(self, leg: ridemod.Leg, what: str) -> bool:
        """Until the Humvee is at the end of the leg. False if it's gone."""
        loop = asyncio.get_running_loop()
        began = last_log = last_move = loop.time()
        last: Pos | None = None
        while True:
            u = await self.send_pos()
            if u == {}:
                await asyncio.sleep(POS_S)
                continue
            if u is None:
                await self.tell_riders("the Humvee is gone")
                for r in self.riders.values():
                    await self.rpc(r.pid, "campaignRideStop", "the Humvee is gone")
                return False
            here = pos_of(u["pos"])
            now = loop.time()
            if last is None or dist(here, last) > 0.5:
                last, last_move = here, now
            to_go = dist(here, leg.to)
            if now - last_log >= DRIVE_LOG_S:
                last_log = now
                print(f"DRIVE   to {what}: {to_go:.0f} m straight-line, {float(u.get('spd') or 0) / KMH:.0f} km/h, "
                      f"hdg {float(u.get('hdg') or 0):.0f}", flush=True)
            if to_go <= ARRIVE_M:
                print(f"STOP    at {what} after {now - began:.0f} s", flush=True)
                return True
            if now - last_move >= STUCK_S:
                print(f"STUCK   on the way to {what}: no movement for {STUCK_S:.0f} s, {to_go:.0f} m short at "
                      f"x {here.x:.0f} y {here.y:.0f}", flush=True)
                await self.tell_riders(f"The Humvee is stuck {to_go * 3.281:.0f} ft short of {what}. -in to get in "
                                       "anyway, -stop to end.")
                last_move = now
            if now - began >= LEG_GIVE_UP_S:
                print(f"STUCK   gave up on {what} after {LEG_GIVE_UP_S:.0f} s", flush=True)
                return True
            await asyncio.sleep(POS_S)

    async def at_jet(self, jet: Jet) -> None:
        """Stopped beside a jet: its rider gets out (into the jet); otherwise a moment's stop all the same."""
        riders = [r for r in self.riders.values() if r.jet is jet and not r.dropped]
        loop = asyncio.get_running_loop()
        until = loop.time() + DROP_S
        if not riders:
            print(f"STOP    jet {jet.n}: nobody's for this one (in the campaign: an AI jet's pilot)", flush=True)
            await self.tell_riders(f"Stopping at jet {jet.n} (not yours).")
        await asyncio.sleep(1.5)   # parked a moment before the cut
        for r in riders:
            r.dropped = True
            await self.into_jet(r.pid, jet)
        while loop.time() < until:
            await self.send_pos()
            await asyncio.sleep(POS_S)

    async def into_jet(self, pid: int, jet: Jet) -> None:
        if self.occupied(jet):
            await self.reply(pid, f"someone's in jet {jet.n}")
            return
        await self.remove_static(jet)
        r = await self.call(self.hooks, "force_slot", {"player_id": pid, "side": BLUE, "slot": jet.slot})
        print(f"ARRIVE  force_slot player {pid} -> jet {jet.n} (slot {jet.slot}): {r}", flush=True)
        if not (isinstance(r, dict) and r.get("ok")):
            await self.rpc(pid, "campaignRideStop", "force_slot failed")
            await self.reply(pid, "DCS didn't move you into the jet: pick blue first (spectators are fine)")
            asyncio.create_task(self.restore_static(jet, delay=1.0))
            return
        await asyncio.sleep(0.5)
        await self.rpc(pid, "campaignEnter")
        print(f"ARRIVE  asked player {pid}'s client script to enter jet {jet.n}", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    ap.add_argument("--missions-dir", type=Path, default=SAVED_GAMES / "Missions" / "campaign-dev")
    ap.add_argument("--port", type=int, default=7341)
    ap.add_argument("--cold", action="store_true", help="park the jets cold (default: hot, engines running)")
    ap.add_argument("--kmh", type=float, default=20.0, help="the Humvee's speed (default 20 km/h)")
    ap.add_argument("--rebuild", action="store_true", help="build the mission again even if it's loaded")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    probe = Probe(args)
    bridge = Bridge(probe.on_message, probe.on_hello, probe.on_disconnect, port=args.port)
    await bridge.start()
    print(f"listening on 127.0.0.1:{bridge.port}; host any mission with the campaign bridge "
          f"(campaign-dev/campaign-p20.miz), MULTIPLAYER > New server", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
