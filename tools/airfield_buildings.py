"""Survey an airfield's buildings, to learn which are administrative (where a ride to the jets
should start; Run 31: the tower was the wrong place, and a building's type name doesn't say what
it is).

    python3 -m tools.airfield_buildings [--airbase "Prince Hassan" ...] [--min-size 8]

Host any mission that loads the campaign bridge (campaign-dev/campaign-p20.miz). For each
airbase, the tool lists the scenery buildings on its grounds (within GROUNDS_M of its taxiways,
from DCS's own taxiway file, and RADIUS_M of the airbase) at least --min-size metres long or
wide, numbers them like a page, from the north-west corner to the south-east (rows ROW_M deep, north
to south, west to east within each), and labels each on the F10 map (``B12``, for
everyone). Look around them (F10, then F11 or F7 near a label) and send back which numbers are
offices, operations or squadron buildings, and any that are clearly something else (hangars,
shelters, warehouses, barracks). Printed: one line per building (number, type, size, distance
to the taxiways and the nearest parking spot) and a count per type. Written to
build/airfield_buildings/<airbase>.json, with each building's map object id.

In chat (any side): ``-label`` draws the labels again, ``-unlabel`` removes them, ``-survey``
surveys again.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import math
from pathlib import Path
from typing import Any

from campaign import taxinet
from campaign.bridge import Bridge, BridgeError, Connection
from campaign.model import Pos
from campaign.protocol import Envelope, Hello, Src
from tools.extract_theater import DCS_DIR

RADIUS_M = 4000.0          # buildings this far from the airbase's reference point
GROUNDS_M = 600.0          # and this close to its taxiways: on the grounds
DRAW_ID = 3600             # the labels' drawing ids (ride starts 3700 up, ghosts 3900 up)
OUT = Path("build/airfield_buildings")
DEFAULT_AIRBASES = ["Prince Hassan", "Khalkhalah"]
# Not buildings: left out by type name (Run 32: DCS gives most scenery no size, so the size
# filter alone kept only fuel tanks).
CLUTTER = ("taxi", "light", "lamp", "trash", "power", "line", "pole", "fence", "wall", "tree", "bush", "sign",
           "omni", "pylon", "mast", "lantern", "cone", "barrier", "bench", "tank", "road", "column", "flag",
           "rail", "pipe", "stolb", "lep", "znak", "gate", "kiosk", "bus_stop", "cistern", "fonar",
           "market")   # STREET_MARKET: stalls
ROW_M = 200.0              # numbered like a page: rows this deep, north to south, west to east in each


def number(buildings: list[dict[str, Any]]) -> None:
    """``n`` for each, in reading order from the north-west corner to the south-east (asked after
    Run 32: nearest-the-parking order looked random on the map), and ``row`` (1 = northernmost)."""
    if not buildings:
        return
    top = max(b["x"] for b in buildings)   # DCS x is north, y east
    for b in buildings:
        b["row"] = int((top - b["x"]) // ROW_M) + 1
    buildings.sort(key=lambda b: (b["row"], b["y"]))
    for n, b in enumerate(buildings, 1):
        b["n"] = n


def dist(a: Pos, b: Pos) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


class Survey:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.mission: Connection | None = None
        self.hooks: Connection | None = None
        self.drawn: list[int] = []
        self.buildings: dict[str, list[dict[str, Any]]] = {}

    # ---------- bridge ----------

    def on_hello(self, conn: Connection, hello: Hello, new_run: bool) -> None:
        print(f"HELLO   {conn.src}", flush=True)
        if conn.src == Src.MISSION:
            self.mission = conn
            self.drawn = []
            asyncio.create_task(self.run())
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
        if env.type in ("event", "player") and d.get("kind") == "chat_command":
            asyncio.create_task(self.command(str(d.get("text") or "").split()))

    async def call(self, op: str, data: dict[str, Any], timeout: float = 60) -> Any:
        if self.mission is None:
            raise BridgeError("no mission")
        return await self.mission.call(op, data, timeout=timeout)

    async def command(self, words: list[str]) -> None:
        if not words:
            return
        try:
            if words[0] == "label":
                await self.label()
            elif words[0] == "unlabel":
                await self.unlabel()
            elif words[0] == "survey":
                await self.run()
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"CHAT    -{words[0]} failed: {e}", flush=True)

    # ---------- the survey ----------

    async def run(self) -> None:
        try:
            info = await self.call("query", {"what": "mission"})
            theatre = str(info.get("theatre") or "Syria")
            res = await self.call("query", {"what": "airbases"})
            airbases = {ab["name"]: ab for ab in res.get("airbases") or [] if isinstance(ab, dict) and ab.get("name")}
            self.buildings = {}
            for name in self.args.airbase:
                ab = airbases.get(name)
                if ab is None:
                    print(f"SURVEY  no airbase {name!r} in this mission ({theatre})", flush=True)
                    continue
                saved = OUT / f"{name.replace(' ', '_')}.json"
                if self.args.saved and saved.is_file():
                    self.buildings[name] = json.loads(saved.read_text())["buildings"]
                    print(f"SAVED   {name}: {len(self.buildings[name])} buildings from {saved}", flush=True)
                    continue
                self.buildings[name] = await self.survey(theatre, name, ab)
            await self.label()
            print("Look around the labels in DCS (F10, then F11 near one) and send back which numbers are "
                  "offices, operations or squadron buildings. -label / -unlabel / -survey in chat.", flush=True)
        except (BridgeError, asyncio.TimeoutError, KeyError, TypeError, ValueError) as e:
            print(f"SURVEY  failed: {type(e).__name__} {e}", flush=True)

    async def survey(self, theatre: str, name: str, ab: dict[str, Any]) -> list[dict[str, Any]]:
        center = Pos(float(ab["pos"]["x"]), float(ab["pos"]["y"]))
        try:
            net = taxinet.load(DCS_DIR, theatre, name)
        except (OSError, ValueError) as e:
            print(f"SURVEY  {name}: can't read its taxiways ({e}): using the radius alone", flush=True)
            net = None
        spots: list[Pos] = []
        try:
            res = await self.call("query", {"what": "parking", "airbase": name})
            spots = [Pos(float(s["pos"]["x"]), float(s["pos"]["y"])) for s in res.get("spots") or []
                     if isinstance(s.get("pos"), dict)]
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"SURVEY  {name}: no parking ({e})", flush=True)
        res = await self.call("query", {"what": "footprints", "center": {"x": center.x, "y": center.y},
                                        "radius": RADIUS_M, "scenery_only": True, "min_size": self.args.min_size,
                                        "keep_unboxed": True, "limit": 5000}, timeout=120)
        found = res.get("found") or []
        out = []
        clutter = collections.Counter()
        for o in found:
            t = str(o.get("type") or "")
            if any(part.startswith(w) for part in t.lower().split("_") for w in CLUTTER):   # by word: not "s-tree-t"
                clutter[t] += 1
                continue
            p = Pos(float(o["pos"]["x"]), float(o["pos"]["y"]))
            to_taxi = net.nearest(p)[3] if net is not None else None
            if to_taxi is not None and to_taxi > GROUNDS_M:
                continue
            out.append({"type": o.get("type"), "id": o.get("id"), "x": round(p.x, 1), "y": round(p.y, 1),
                        "length": round(float(o.get("length") or 0), 1), "width": round(float(o.get("width") or 0), 1),
                        "height": round(float(o.get("height") or 0), 1),
                        "to_taxiway_m": None if to_taxi is None else round(to_taxi),
                        "to_parking_m": round(min((dist(p, s) for s in spots), default=-1)),
                        "box": o.get("box")})
        number(out)
        print(f"SURVEY  {name}: {len(found)} scenery objects within {RADIUS_M:.0f} m; left out as clutter: "
              + (", ".join(f"{t} {n}" for t, n in clutter.most_common(15)) or "none"), flush=True)
        print(f"SURVEY  {name}: {len(out)} buildings on the grounds (within {GROUNDS_M:.0f} m of the taxiways)",
              flush=True)
        print(f"{'#':>4}  {'type':<36} {'L x W x H (m)':<18} {'taxiway':>8} {'parking':>8}", flush=True)
        row = 0
        for b in out:
            if b["row"] != row:
                row = b["row"]
                print(f"      row {row} ({(row - 1) * ROW_M:.0f}-{row * ROW_M:.0f} m from the north edge)", flush=True)
            size = f"{b['length']:.0f} x {b['width']:.0f} x {b['height']:.0f}" if b["box"] else "(no size)"
            print(f"{'B' + str(b['n']):>4}  {str(b['type']):<36} {size:<18} {b['to_taxiway_m'] or '?':>7}m "
                  f"{b['to_parking_m']:>7}m", flush=True)
        counts = collections.Counter(str(b["type"]) for b in out)
        print(f"TYPES   {name}: " + ", ".join(f"{t} {n}" for t, n in counts.most_common()), flush=True)
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"{name.replace(' ', '_')}.json"
        path.write_text(json.dumps({"airbase": name, "theatre": theatre, "min_size": self.args.min_size,
                                    "buildings": out}, indent=1))
        print(f"WROTE   {path}", flush=True)
        return out

    async def label(self) -> None:
        await self.unlabel()
        items = []
        for name, buildings in self.buildings.items():
            for b in buildings:
                did = DRAW_ID + len(items)
                items.append({"id": did, "audience": "all", "shape": "text", "pos": {"x": b["x"], "y": b["y"]},
                              "font_size": 12, "text": f"{name[:3]} B{b['n']}", "color": [1, 1, 0, 1],
                              "fill": [0, 0, 0, 0.5], "read_only": True})
        if items:
            await self.call("draw_batch", {"items": items})
            self.drawn = [i["id"] for i in items]
        print(f"LABELS  {len(items)} on the F10 map", flush=True)

    async def unlabel(self) -> None:
        if self.drawn:
            await self.call("undraw", {"ids": self.drawn})
            self.drawn = []


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--airbase", action="append", help=f"DCS airbase name (repeat; default {DEFAULT_AIRBASES})")
    ap.add_argument("--min-size", type=float, default=8.0, help="buildings at least this long or wide (m)")
    ap.add_argument("--saved", action="store_true",
                    help="label the last survey's buildings (build/airfield_buildings) instead of surveying again")
    ap.add_argument("--port", type=int, default=7341)
    args = ap.parse_args()
    args.airbase = args.airbase or DEFAULT_AIRBASES
    survey = Survey(args)
    bridge = Bridge(survey.on_message, survey.on_hello, survey.on_disconnect, port=args.port)
    await bridge.start()
    print(f"listening on 127.0.0.1:{bridge.port}; host a Syria mission with the campaign bridge "
          "(campaign-dev/campaign-p20.miz), MULTIPLAYER > New server", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
