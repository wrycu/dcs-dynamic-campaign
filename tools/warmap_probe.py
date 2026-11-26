"""Proof P21: a war map inside DCS, in a native window (not the F10 map, not the browser).

The opt-in client script (lua/client/campaign_client.lua, version 3) opens a window with DCS's own
map widget, the one Instant Action's editor and the in-game DTC manager use (GUIMapView), and
draws a made-up war around Khalkhalah on it: a front line between shaded blue and red ground,
formations in contact, SAM rings, an airbase, two flight routes and labels, with a side panel.
Clicking a symbol shows its details. It works for a player who joined a server and for the host
of a server hosted from DCS (MULTIPLAYER > New server).

    python3 -m tools.warmap_probe

Host campaign-dev/campaign-p20.miz (from Run 25, or any mission at all: the probe needs only the
Hooks and the client script). Then type in chat:

    -map          open the war map (or redraw it); Ctrl+Shift+F8 then shows or hides it
    -map move     the war moves on: redraw it without moving your view, and time the redraw
    -map live S   move the flight along its route every S seconds (a live refresh); -map off stops
    -help         this list

MAP lines say how far the window got (each step, then what was drawn and how long it took);
CLICK lines what a click on the map found.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
from typing import Any

from campaign.bridge import Bridge, BridgeError, Connection
from campaign.protocol import Envelope, Hello, Src
from tools.build_test_missions import AIRBASE_NAME, AIRBASE_XY

NM = 1852.0
BLUE = [0.35, 0.6, 1.0, 1.0]
RED = [1.0, 0.3, 0.25, 1.0]
YELLOW = [1.0, 0.85, 0.2, 1.0]
WHITE = [1.0, 1.0, 1.0, 1.0]
FRONT_NM_EAST = 25.0
ROUTE_NM = [(0, 0), (10, 12), (12, 22), (0, 22), (0, 0)]   # (north, east) of the field: Enfield 1's CAP


def at(nm_north: float, nm_east: float) -> dict[str, float]:
    return {"x": AIRBASE_XY[0] + nm_north * NM, "y": AIRBASE_XY[1] + nm_east * NM}


def front(step: int) -> list[dict[str, float]]:
    """North to south, bulging west a little more each step (red is pushing)."""
    return [at(n, FRONT_NM_EAST - step * 2 + 3 * math.sin(n / 9)) for n in range(-40, 41, 5)]


def along(route: list[tuple[float, float]], t: float) -> tuple[dict[str, float], float]:
    """A point a fraction t (0..1) of the way along a route, and the heading there (degrees)."""
    legs = list(zip(route, route[1:]))
    lens = [math.dist(a, b) for a, b in legs]
    d = (t % 1.0) * sum(lens)
    for (a, b), n in zip(legs, lens):
        if d <= n or (a, b) == legs[-1]:
            f = d / n if n else 0.0
            p = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
            return at(*p), math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 360
        d -= n
    return at(*route[0]), 0.0


def war_picture(step: int = 0, flight_t: float = 0.3, keep_view: bool = False) -> dict[str, Any]:
    """The made-up war the probe draws. step moves the ground war on; flight_t is how far Enfield 1
    is along its route."""
    line = front(step)
    west, east = at(0, -60)["y"], at(0, 90)["y"]
    blue_ground = line + [{"x": line[-1]["x"], "y": west}, {"x": line[0]["x"], "y": west}]
    red_ground = line + [{"x": line[-1]["x"], "y": east}, {"x": line[0]["x"], "y": east}]
    shift = -2.0 * step
    objects: list[dict[str, Any]] = [
        {"kind": "area", "points": blue_ground, "color": [0, 0, 0, 0], "fill": [0.3, 0.55, 1.0, 0.20]},
        {"kind": "area", "points": red_ground, "color": [0, 0, 0, 0], "fill": [1.0, 0.3, 0.25, 0.20]},
        {"kind": "line", "points": line, "color": [0.9, 0.9, 0.9, 1], "style": "boundary",
         "info": f"The front.\nRed has pushed {2 * step} nm west since the probe started."},
        {"kind": "icon", "sym": "airbase", "color": BLUE, **at(0, 0), "label": AIRBASE_NAME,
         "info": f"{AIRBASE_NAME} (blue)\n2 Hornet squadrons, 18 aircraft\nRunway 07/25 open"},
    ]
    for i, n in enumerate((-12, -2, 9)):
        p = line[min(range(len(line)), key=lambda j: abs(line[j]["x"] - at(n, 0)["x"]))]
        objects.append({"kind": "icon", "sym": "tank", "color": RED, "x": p["x"], "y": p["y"] + 2.5 * NM,
                        "heading": 270, "label": f"{i + 4}th Tank Bn",
                        "info": f"{i + 4}th Tank Battalion (red)\nIn contact: attacking west\nAbout {30 - 4 * i} tanks"})
        objects.append({"kind": "icon", "sym": "ifv", "color": BLUE, "x": p["x"], "y": p["y"] - 2.5 * NM,
                        "heading": 90, "label": f"{i + 1}/7 Mech",
                        "info": f"{i + 1}st Bn, 7th Mechanized Brigade (blue)\nDefending\nStrength {80 - 15 * i}%"})
    battle = at(-2, FRONT_NM_EAST - 2 * step)
    objects += [
        {"kind": "text", **at(-2, FRONT_NM_EAST - 2 * step + 6), "text": "Battle of Tell Hara",
         "color": YELLOW, "size": 14, "info": "Battle of Tell Hara: day 2\nBlue holding, red reinforcing"},
        {"kind": "icon", "sym": "target", "color": YELLOW, **battle, "info": "Battle of Tell Hara: day 2"},
        {"kind": "icon", "sym": "sam_medium", "color": RED, **at(8, 45), "label": "SA-6",
         "info": "SA-6 site (red)\nThreat ring 13 nm\nLast seen 40 min ago"},
        {"kind": "ring", **at(8, 45), "r": 13 * NM, "color": RED, "fill": [1, 0.3, 0.25, 0.08], "style": "dash",
         "info": "SA-6 threat ring, 13 nm"},
        {"kind": "icon", "sym": "sam_long", "color": RED, **at(-15, 75), "label": "SA-10",
         "info": "SA-10 site (red)\nThreat ring 40 nm"},
        {"kind": "ring", **at(-15, 75), "r": 40 * NM, "color": RED, "style": "solid2",
         "info": "SA-10 threat ring, 40 nm"},
        {"kind": "line", "points": [at(*p) for p in ROUTE_NM], "color": YELLOW, "style": "dash",
         "info": "Enfield 1: CAP over the front\n4x F/A-18C, takeoff 14:30\nYour flight"},
        {"kind": "line", "points": [at(*p) for p in [(0, 0), (-18, 14), (-20, 30), (0, 0)]], "color": BLUE,
         "style": "dot", "info": "Colt 2: CAS at Tell Hara\n2x F/A-18C"},
    ]
    pos, hdg = along(ROUTE_NM, flight_t)
    objects.append({"kind": "icon", "sym": "fighter", "color": YELLOW, **pos, "heading": hdg, "label": "Enfield 1",
                    "info": f"Enfield 1, 4x F/A-18C\nOn CAP, heading {hdg:03.0f}\n(made up: the probe moves it)"})
    panel = [
        "DAY 3   14:20 local", "",
        "FRONT",
        f"  Red pushing west: {2 * step} nm today", "  Battle of Tell Hara (day 2)", "",
        "YOUR FLIGHTS",
        "  Enfield 1  CAP     14:30  4 seats", "  Colt 2     CAS     14:45  2 seats", "",
        "THREATS",
        "  SA-6  near Tell Hara, 13 nm", "  SA-10 east, 40 nm", "",
        "NEWS",
        "  14:05  Red armour crossed the M5", "  13:40  Uzi 3 lost over Sanamayn",
    ]
    return {"title": "Campaign: southern front", "center": at(0, 20), "scale": 120 * NM, "mode": "map",
            "keep_view": keep_view, "panel": panel, "objects": objects}


class Probe:
    def __init__(self) -> None:
        self.hooks: Connection | None = None
        self.step = 0
        self.flight_t = 0.3
        self.viewers: set[int] = set()       # players who opened the map
        self.live: asyncio.Task | None = None

    def on_hello(self, conn: Connection, hello: Hello, new_run: bool) -> None:
        print(f"HELLO   {conn.src} new_run={new_run}", flush=True)
        if conn.src == Src.HOOKS:
            self.hooks = conn

    def on_disconnect(self, conn: Connection) -> None:
        print(f"DISCONNECT {conn.src}", flush=True)
        if conn is self.hooks:
            self.hooks = None

    def on_message(self, conn: Connection, env: Envelope) -> None:
        if env.type not in ("event", "player"):
            return
        d = env.data or {}
        kind = d.get("kind")
        if kind == "client_hello":
            print(f"CLIENT  player id {d.get('player_id')} has the client script (version {d.get('version')})",
                  flush=True)
            asyncio.create_task(self.reply(d.get("player_id"), "war map probe: type -map"))
        elif kind == "client_web" and str(d.get("stage", "")).startswith("map"):
            tag = "CLICK  " if d.get("stage") == "map_click" else "MAP    "
            print(f"{tag} player id {d.get('player_id')}: {d.get('detail') or ''}".replace("\n", " | "), flush=True)
        elif kind == "chat_command":
            asyncio.create_task(self.command(d))

    async def reply(self, player_id: int | None, text: str) -> None:
        print(f"REPLY   to {player_id}: {text}", flush=True)
        if self.hooks is None or player_id is None:
            return
        try:
            await self.hooks.call("chat_to", {"player_id": player_id, "text": "> " + text}, timeout=10)
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"REPLY   failed: {e}", flush=True)

    async def send_map(self, player_id: int, keep_view: bool) -> None:
        if self.hooks is None:
            raise BridgeError("the Hooks aren't connected")
        spec = war_picture(self.step, self.flight_t, keep_view)
        await self.hooks.call("client_rpc", {"player_id": player_id, "method": "campaignMap", "args": [spec]},
                              timeout=10)
        self.viewers.add(player_id)

    async def command(self, d: dict[str, Any]) -> None:
        pid, words = d.get("player_id"), (d.get("text") or "").split()
        print(f"CHAT    {d.get('name')} (id {pid}): -{' '.join(words)}", flush=True)
        if not words or pid is None:
            return
        cmd, rest = words[0].lower(), [w.lower() for w in words[1:]]
        try:
            if cmd == "help":
                await self.reply(pid, "-map | -map move | -map live S | -map off")
            elif cmd != "map":
                await self.reply(pid, f"unknown command -{cmd}; try -help")
            elif not rest:
                await self.send_map(pid, keep_view=False)
                await self.reply(pid, "war map sent (Ctrl+Shift+F8 shows or hides it)")
            elif rest[0] == "move":
                self.step += 1
                await self.send_map(pid, keep_view=True)
                await self.reply(pid, f"the war moved on (step {self.step})")
            elif rest[0] == "live":
                every = float(rest[1]) if len(rest) > 1 else 5.0
                self.stop_live()
                self.live = asyncio.create_task(self.live_loop(max(1.0, every)))
                await self.reply(pid, f"moving Enfield 1 every {max(1.0, every):g} s; -map off stops")
            elif rest[0] == "off":
                self.stop_live()
                await self.reply(pid, "live updates off")
            else:
                await self.reply(pid, f"unknown: -map {rest[0]}; try -help")
        except (BridgeError, asyncio.TimeoutError, ValueError) as e:
            await self.reply(pid, f"-{' '.join(words)} failed: {e}")

    def stop_live(self) -> None:
        if self.live and not self.live.done():
            self.live.cancel()
        self.live = None

    async def live_loop(self, every: float) -> None:
        while True:
            await asyncio.sleep(every)
            self.flight_t = (self.flight_t + 0.02) % 1.0
            for pid in list(self.viewers):
                try:
                    await self.send_map(pid, keep_view=True)
                except (BridgeError, asyncio.TimeoutError) as e:
                    print(f"LIVE    player id {pid}: {e}", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7341)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    probe = Probe()
    bridge = Bridge(probe.on_message, probe.on_hello, probe.on_disconnect, port=args.port)
    await bridge.start()
    print(f"listening on 127.0.0.1:{bridge.port}; host campaign-dev/campaign-p20.miz (MULTIPLAYER > New server) "
          f"and type -map in chat", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
