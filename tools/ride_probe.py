"""Proof P22: ride to the jet. A blue spectator's camera rides in a Humvee the server drives across
the base to their parked jet, then the server puts them in it. Host from your DCS (MULTIPLAYER >
New server), with client script version 24.

    python3 -m tools.ride_probe

Host any mission that loads the campaign bridge (campaign-dev/campaign-p20.miz from Run 25). The
probe reads Khalkhalah's free parking, picks an open spot a Hornet fits, builds
campaign-dev/campaign-p22.miz with one Client Hornet there ("P22 Ride", hot unless --cold) and
loads it. In that mission, pick blue, stay in spectators, and type in chat:

    -ride [seat|chase] [place] [fixed] [KMH]
                               a Humvee starts at the "briefing room" (another parking spot, 300 to
                               600 m from the jet) and drives to the jet at KMH (default 20). Your
                               view goes into its passenger seat (seat, the default) or behind it
                               (chase) by itself. First DCS's own object camera on the Humvee,
                               from outside for a few seconds, then in its passenger seat, if this
                               DCS lets the script ask for that camera (client version 20; Run 29:
                               LoCreateCameraRequest gives nil, version 21 builds the request). If not,
                               the script presses F7 (ground unit view) until the view watches the
                               Humvee, then F4 (camera mounted on it), and puts the camera at the
                               seat (version 22). If none of that works, it places the camera every
                               frame on whatever F4 shows, as in Run 29 (don't press F5: it crashed
                               DCS mid-ride). "place" skips DCS's camera request; "fixed" keeps the
                               view straight ahead. It stops beside the jet and you're put in it.
    -eye FWD RIGHT UP          where the seat camera sits in the vehicle, metres (default 0.2 0.45 1.55);
                               during a ride it moves at once (client version 24)
    -nudge FWD RIGHT UP        move it by that much (e.g. -nudge -0.1 0 0.05: 10 cm back, 5 cm up)
    -car TYPE                  the vehicle for the next ride (default Hummer); -cars lists some
    -stop (or -ride stop)      stop the ride, remove the Humvee
    -in                        straight into the jet, no ride
    -rebuild                   build and load campaign-p22.miz again (new spots)
    -help                      this list

Printed: RIDE lines from your client script (whether it found the Humvee in DCS's own objects or
follows the server's positions, and how often the camera stayed where it put it), DRIVE lines
(where the Humvee is), ARRIVE and the slot change.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
from pathlib import Path
from typing import Any

from campaign.bridge import Bridge, BridgeError, Connection
from campaign.catalog import COUNTRY_FOR_SIDE
from campaign.model import Side
from campaign.parking import size_from, stands_from, usable_spots
from campaign.protocol import Envelope, Hello, Src
from tools.build_test_missions import AIRBASE_NAME, AIRDROME_ID, DEFAULT_TEMPLATE, SAVED_GAMES, build

HORNET = "FA-18C_hornet"
HUMVEE = "Hummer"
# Light vehicles to try for the ride (Run 29: the Humvee's passenger figure is where the camera sits).
CARS = ["Hummer", "M1043 HMMWV Armament", "M1045 HMMWV TOW", "P20_drivable", "TugHarlan_drivable", "B600_drivable",
        "r11_volvo_drivable", "Willys_MB", "UAZ-469", "Tigr_233036", "VAZ Car", "tacr2a"]
GROUP = "P22 Ride"
SEAT = f"{GROUP}-1"
CAR = "P22 Humvee"
MISSION_FILE = "campaign-p22.miz"
BLUE = 2
OPEN_SPOTS = (104, 72, 68)       # open spots first, shelters last: the drive should end in the open
START_M = (300.0, 600.0)         # the "briefing room": a parking spot this far from the jet
STOP_SHORT_M = 15.0              # the Humvee stops this far from the jet, on the side it came from
ARRIVE_M = 4.0                   # this close to its stop point: arrived
POS_S = 0.25                     # positions to the client script this often
DRIVE_LOG_S = 2.0
STUCK_S = 20.0                   # not moving this long before arriving: stuck
GIVE_UP_S = 300.0
FT, KMH = 0.3048, 1 / 3.6
# The client script's ride progress (client_web, stage "ride") worth telling the player.
RIDE_CHAT = [
    ("native: the view came", "Your view goes to the Humvee: you can look around."),
    ("native: in the seat", "You're in the Humvee's passenger seat: look around as you like."),
    ("native: not at the seat with the world", "DCS's camera stays outside the Humvee (it wouldn't go to the seat)."),
    ("native: the view is elsewhere", "Your view left the Humvee: -ride to start again."),
    ("focus: native", "Getting your view onto the Humvee (F7, then F4, pressed for you)..."),
    ("native: the vehicle isn't", "Can't see the Humvee from your PC: placing the camera instead (F4 pressed for you)."),
    ("focus: mounted at the seat", "You're in the Humvee's passenger seat: look around as you like."),
    ("focus: DCS doesn't keep", "You're in the Humvee's passenger seat (the script holds the camera there)."),
    ("focus: chase view", "Behind the Humvee: look around as you like."),
    ("focus: F7 didn't reach", "Couldn't get the view onto the Humvee: placing the camera in it instead (F4 pressed for you)."),
    ("focus: the view is elsewhere", "Your view left the Humvee: -ride to start again."),
    ("holding:", "You're in the Humvee."),
    ("let go:", "Your view changed, so the ride let go of the camera. -ride to start again."),
]


def dist(a: dict[str, float], b: dict[str, float]) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


class Probe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.mission: Connection | None = None
        self.hooks: Connection | None = None
        self.hooks_up = asyncio.Event()
        self.client_scripts: dict[int, Any] = {}    # player id -> client script version
        self.jet: dict[str, Any] | None = None      # {x, y, heading, slot} of P22 Ride-1, once loaded
        self.eye = [0.2, 0.45, 1.55]
        self.car_type = HUMVEE
        self.riding: int | None = None    # the player id on a ride now
        self.ride_task: asyncio.Task | None = None
        self.rebuild = args.rebuild

    # ---------- bridge callbacks ----------

    def on_hello(self, conn: Connection, hello: Hello, new_run: bool) -> None:
        print(f"HELLO   {conn.src} new_run={new_run}", flush=True)
        if conn.src == Src.MISSION:
            self.mission = conn
            self.jet = None
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
        elif kind in ("player_slot", "slot_try"):
            print(f"HOOKS   {kind} {d.get('name')} id {d.get('player_id')} side {d.get('side')} slot {d.get('slot')!r}",
                  flush=True)
        elif kind in ("birth", "player_enter_unit", "engine_startup"):
            u = d.get("unit") or {}
            if str(u.get("name") or "").startswith("P22"):
                print(f"EVENT   {kind} {u.get('player') or ''} {u.get('type') or ''} {u.get('name')}", flush=True)

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

    async def rpc(self, pid: int, method: str, *args: Any) -> None:
        await self.call(self.hooks, "client_rpc", {"player_id": pid, "method": method, "args": list(args)})

    # ---------- the mission ----------

    async def mission_up(self) -> None:
        try:
            res = await self.call(self.mission, "query", {"what": "client_groups"})
            group = next((g for g in res.get("groups") or [] if g.get("name") == GROUP), None)
            unit = next((u for u in (group or {}).get("units") or [] if u.get("name") == SEAT), None)
            if unit is None or self.rebuild or unit.get("x") is None:
                if unit is not None and unit.get("x") is None:
                    print("MISSION the mission bridge doesn't say where the jet is (an older bridge?): rebuilding",
                          flush=True)
                self.rebuild = False
                await self.build_and_load()
                return
            self.jet = {"x": float(unit["x"]), "y": float(unit["y"]), "heading": float(unit.get("heading") or 0),
                        "slot": str(unit["id"])}
            print(f"MISSION {GROUP} parked at x {self.jet['x']:.0f} y {self.jet['y']:.0f}, slot {self.jet['slot']}",
                  flush=True)
            await self.say("P22 ride probe. Pick blue, stay in spectators, and type -ride (or -ride chase) in chat. "
                           "-help for the rest.", 30)
        except (BridgeError, asyncio.TimeoutError, KeyError, TypeError, ValueError) as e:
            print(f"MISSION setup failed: {e}", flush=True)

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
        spots = (await self.call(self.mission, "query", {"what": "parking", "airbase": AIRBASE_NAME, "free_only": True}))
        stands = stands_from(await self.call(self.hooks, "stands", {"airdrome_id": int(ab["id"])}))
        size = size_from(await self.call(self.mission, "query", {"what": "type_size", "type": HORNET}))
        all_spots = spots.get("spots") or []
        fit = usable_spots(all_spots, stands, size)
        # The jet: an open spot with another free spot START_M away for the briefing room.
        fit.sort(key=lambda s: OPEN_SPOTS.index(s["type"]) if s["type"] in OPEN_SPOTS else len(OPEN_SPOTS))
        jet = next((s for s in fit if self.start_for(s["pos"], all_spots)), fit[0] if fit else None)
        if jet is None:
            print(f"BUILD   no free spot at {AIRBASE_NAME} that a Hornet fits", flush=True)
            return
        print(f"BUILD   jet at term {jet['term']} (type {jet['type']}), x {jet['pos']['x']:.0f} y {jet['pos']['y']:.0f}; "
              f"{len(fit)} of {len(all_spots)} free spots fit a Hornet", flush=True)
        group = {"name": GROUP, "airdrome_id": int(ab["id"]), "x": ab["pos"]["x"], "y": ab["pos"]["y"],
                 "start": "cold" if self.args.cold else "hot",
                 "waypoints": [{"x": ab["pos"]["x"] + 20_000, "y": ab["pos"]["y"], "alt": 15_000 * FT}],
                 "units": [{"name": SEAT, "skill": "Client", "type": HORNET, "x": jet["pos"]["x"],
                            "y": jet["pos"]["y"], "heading": 0, "parking": int(jet["term"])}]}
        out = self.args.missions_dir
        path = await asyncio.to_thread(build, self.args.template, out, MISSION_FILE, "lua/mission/campaign_bridge.lua",
                                       "", "Load campaign bridge", None, True, None,
                                       {"client_groups": [group], "drop_players": True}, None, AIRDROME_ID, False)
        try:
            rel = "\\".join(path.resolve().relative_to((SAVED_GAMES / "Missions").resolve()).parts)
        except ValueError:
            print(f"BUILD   {path} isn't under {SAVED_GAMES / 'Missions'}: load it yourself", flush=True)
            return
        await self.say(f"P22: built {MISSION_FILE}. Loading it in 10 s.", 12)
        await self.call(self.hooks, "load_mission", {"file": rel, "delay": 10})
        print(f"BUILD   {path}; loading {rel}", flush=True)

    def start_for(self, jet: dict[str, float], spots: list[dict[str, Any]]) -> dict[str, float] | None:
        """The briefing room for a jet at ``jet``: the free spot nearest the middle of START_M."""
        mid = sum(START_M) / 2
        near = [s for s in spots if START_M[0] <= dist(s["pos"], jet) <= START_M[1]]
        return min(near, key=lambda s: abs(dist(s["pos"], jet) - mid))["pos"] if near else None

    # ---------- chat ----------

    async def command(self, d: dict) -> None:
        pid = d.get("player_id")
        words = (d.get("text") or "").split()
        if not words or pid is None:
            return
        cmd, rest = words[0].lower(), words[1:]
        try:
            if cmd == "help":
                await self.reply(pid, "-ride [seat|chase] [place] [fixed] [KMH] | -eye FWD RIGHT UP | -nudge FWD RIGHT UP | "
                                      "-car TYPE | -cars | -stop | -in | -rebuild")
            elif cmd == "ride" and any(w.lstrip("-") == "stop" for w in rest):
                await self.command({**d, "text": "stop"})
            elif cmd == "ride":
                view = next((w for w in rest if w in ("seat", "chase")), "seat")
                kmh = next((float(w) for w in rest if w.replace(".", "", 1).isdigit()), self.args.kmh)
                if str(d.get("slot") or "") not in ("", "0"):   # Run 29: -ride from the jet drove into it
                    await self.reply(pid, "You're in a jet: go back to blue spectators first, then -ride")
                    return
                if self.ride_task and not self.ride_task.done():
                    self.ride_task.cancel()
                self.ride_task = asyncio.create_task(self.ride(pid, d.get("side"), view, kmh, "fixed" not in rest,
                                                           "place" not in rest))
            elif cmd in ("eye", "nudge"):
                vals = [float(w) for w in rest[:3]]
                if cmd == "eye":
                    self.eye = vals + self.eye[len(vals):]
                else:
                    self.eye = [round(e + (vals[i] if i < len(vals) else 0.0), 3) for i, e in enumerate(self.eye)]
                live = self.riding == pid and self.ride_task is not None and not self.ride_task.done()
                if live:
                    await self.rpc(pid, "campaignRideEye", self.eye)
                await self.reply(pid, f"seat camera: {self.eye[0]:g} m forward, {self.eye[1]:g} m right, {self.eye[2]:g} m up"
                                      + (" (moved now)" if live else " (next ride)"))
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
                await self.rpc(pid, "campaignRideStop", "-stop")
                await self.call(self.mission, "despawn_group", {"group": CAR})
                await self.reply(pid, "ride stopped")
            elif cmd == "in":
                await self.into_jet(pid)
            elif cmd == "rebuild":
                self.rebuild = True
                await self.build_and_load()
        except (BridgeError, asyncio.TimeoutError, ValueError) as e:
            await self.reply(pid, f"-{cmd} failed: {e}")

    # ---------- the ride ----------

    async def ride(self, pid: int, side: Any, view: str, kmh: float, look: bool = True, native: bool = True) -> None:
        try:
            await self._ride(pid, side, view, kmh, look, native)
        except asyncio.CancelledError:
            print("DRIVE   ride cancelled", flush=True)
            raise
        except (BridgeError, asyncio.TimeoutError, KeyError, TypeError, ValueError) as e:
            await self.reply(pid, f"ride failed: {e}")

    async def _ride(self, pid: int, side: Any, view: str, kmh: float, look: bool = True, native: bool = True) -> None:
        if self.jet is None:
            await self.reply(pid, f"{GROUP} isn't in this mission yet (wait for the probe to load {MISSION_FILE})")
            return
        if int(side or 0) != BLUE:
            await self.reply(pid, "pick blue first (spectators are fine): DCS only moves blue spectators into a jet")
        version = self.client_scripts.get(pid)
        if version is None or int(version) < 23:
            await self.reply(pid, f"your client script is version {version or 'none'}: the ride needs 23")
        jet = {"x": self.jet["x"], "y": self.jet["y"]}
        spots = (await self.call(self.mission, "query", {"what": "parking", "airbase": AIRBASE_NAME,
                                                         "free_only": True})).get("spots") or []
        start = self.start_for(jet, [s for s in spots if dist(s["pos"], jet) > 1])
        if start is None:
            await self.reply(pid, f"no free parking spot {START_M[0]:.0f} to {START_M[1]:.0f} m from the jet to start from")
            return
        d = dist(start, jet)
        k = (d - STOP_SHORT_M) / d
        stop = {"x": start["x"] + (jet["x"] - start["x"]) * k, "y": start["y"] + (jet["y"] - start["y"]) * k}
        hdg = math.atan2(jet["y"] - start["y"], jet["x"] - start["x"])
        speed = kmh * KMH
        await self.call(self.mission, "despawn_group", {"group": CAR})
        await self.call(self.mission, "spawn_group", {"category": "ground", "country": COUNTRY_FOR_SIDE[Side.BLUE], "group": {
            "name": CAR, "task": "Ground Nothing",
            "units": [{"name": f"{CAR}-1", "type": self.car_type, "x": start["x"], "y": start["y"], "heading": hdg,
                       "skill": "Average", "playerCanDrive": False}],
            "route": {"points": [{"x": p["x"], "y": p["y"], "action": "Off Road", "type": "Turning Point",
                                  "speed": speed, "speed_locked": True, "ETA": 0, "ETA_locked": False,
                                  "formation_template": ""} for p in (start, stop)]}}})
        self.riding = pid
        print(f"DRIVE   {self.car_type} from x {start['x']:.0f} y {start['y']:.0f} to {STOP_SHORT_M:.0f} m short of the jet: "
              f"{d - STOP_SHORT_M:.0f} m at {kmh:g} km/h (about {(d - STOP_SHORT_M) / speed:.0f} s), {view} view"
              f"{'' if native else ', placing the camera'}{'' if look else ', fixed'}", flush=True)
        await self.reply(pid, f"Your ride is here: {d * 3.281:.0f} ft to the jet, about {(d - STOP_SHORT_M) / speed:.0f} s")
        await self.rpc(pid, "campaignRide", {"unit": f"{CAR}-1", "view": view, "eye": self.eye, "chase": [12, 5],
                                             "look": look, "native": native})
        loop = asyncio.get_running_loop()
        began = last_log = last_move = loop.time()
        last_pos: dict[str, float] | None = None
        while True:
            g = await self.call(self.mission, "query", {"what": "group", "group": CAR}, timeout=5)
            if not g.get("exists") or not g.get("units"):
                await self.reply(pid, "the Humvee is gone")
                await self.rpc(pid, "campaignRideStop", "the Humvee is gone")
                return
            u = g["units"][0]
            pos = u["pos"]
            await self.rpc(pid, "campaignRidePos", {"x": pos["x"], "y": pos["y"], "alt": u.get("alt"),
                                                    "hdg": u.get("hdg"), "spd": u.get("spd")})
            now = loop.time()
            if last_pos is None or dist(pos, last_pos) > 0.5:
                last_pos, last_move = pos, now
            to_stop = dist(pos, stop)
            if now - last_log >= DRIVE_LOG_S:
                last_log = now
                print(f"DRIVE   {to_stop:.0f} m to go, {float(u.get('spd') or 0) / KMH:.0f} km/h, hdg {float(u.get('hdg') or 0):.0f}",
                      flush=True)
            if to_stop <= ARRIVE_M:
                break
            if now - last_move >= STUCK_S:
                print(f"STUCK   no movement for {STUCK_S:.0f} s, {to_stop:.0f} m short at x {pos['x']:.0f} y {pos['y']:.0f}",
                      flush=True)
                await self.reply(pid, f"The Humvee is stuck {to_stop * 3.281:.0f} ft short. -in to get in anyway, -stop to end.")
                last_move = now
            if now - began >= GIVE_UP_S:
                await self.reply(pid, "The ride took too long; -in to get in.")
                await self.rpc(pid, "campaignRideStop", "too long")
                return
            await asyncio.sleep(POS_S)
        print(f"ARRIVE  {loop.time() - began:.0f} s after setting off, {dist(last_pos or stop, jet):.0f} m from the jet",
              flush=True)
        await asyncio.sleep(1.5)   # a moment parked beside it before the cut
        await self.into_jet(pid)
        await asyncio.sleep(5)
        await self.call(self.mission, "despawn_group", {"group": CAR})

    async def into_jet(self, pid: int) -> None:
        if self.jet is None:
            await self.reply(pid, f"{GROUP} isn't in this mission yet")
            return
        r = await self.call(self.hooks, "force_slot", {"player_id": pid, "side": BLUE, "slot": self.jet["slot"]})
        print(f"ARRIVE  force_slot player {pid} -> slot {self.jet['slot']}: {r}", flush=True)
        if not (isinstance(r, dict) and r.get("ok")):
            await self.rpc(pid, "campaignRideStop", "force_slot failed")
            await self.reply(pid, "DCS didn't move you into the jet: pick blue first (spectators are fine)")
            return
        await asyncio.sleep(0.5)
        await self.rpc(pid, "campaignEnter")
        print(f"ARRIVE  asked player {pid}'s client script to enter", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    ap.add_argument("--missions-dir", type=Path, default=SAVED_GAMES / "Missions" / "campaign-dev")
    ap.add_argument("--port", type=int, default=7341)
    ap.add_argument("--cold", action="store_true", help="park the jet cold (default: hot, engines running)")
    ap.add_argument("--kmh", type=float, default=20.0, help="the Humvee's speed (default 20 km/h)")
    ap.add_argument("--rebuild", action="store_true", help=f"build {MISSION_FILE} again even if it's loaded")
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
