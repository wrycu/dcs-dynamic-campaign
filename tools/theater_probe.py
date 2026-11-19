"""Proof P6: can the map layer (airbases, towns, roads, bridges) be extracted from DCS well enough
to build objectives and the links between them? Single player is fine.

    python3 -m tools.theater_probe [--radius-nm 60] [--towns 40]

Start it, then start build/campaign-smoke.miz (campaign-dev/) and take the jet; leave DCS focused,
it will stutter while the probe works (this is a one-off extraction, not play). The probe:

1. lists every airbase DCS knows (runways, parking);
2. reads Mods/terrains/Syria/map/towns.lua and converts each place to map coordinates
   (coord.LLtoLO), keeping those within --radius-nm of Khalkhalah;
3. ranks those places by size: scenery objects (buildings and the like) within 1 km;
4. looks for bridges: scenery whose type name says so, within --bridge-radius-nm, and prints
   the most common scenery type names so we can see what DCS calls things;
5. picks objectives (the airdromes and the --towns largest places in the area), links each to
   its nearest neighbours, and asks DCS for the road route of every link;
6. writes build/theater/syria-probe.json and a map image, build/theater/syria-probe.png.
Every step is timed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import re
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from campaign.bridge import Bridge, BridgeError, Connection
from campaign.model import Pos
from campaign.protocol import Envelope, Hello, Src
from campaign.roads import Path as RoadPath, simplify
from tools.build_test_missions import AIRBASE_NAME, AIRBASE_XY, PREFIX, ROOT

NM = 1852.0
DCS_DIR = PREFIX / "drive_c" / "Program Files" / "Eagle Dynamics" / "DCS World"
TOWNS_LUA = DCS_DIR / "Mods" / "terrains" / "Syria" / "map" / "towns.lua"
TOWN_RE = re.compile(r'^\["(?P<name>(?:[^"\\]|\\.)*)"\]\s*=\s*\{\s*latitude\s*=\s*(?P<lat>-?[\d.]+),\s*'
                     r'longitude\s*=\s*(?P<lon>-?[\d.]+)', re.M)
BRIDGE_WORDS = ["bridge", "most"]
LONG = 300.0


def read_towns(path: Path) -> list[dict[str, Any]]:
    return [{"name": m["name"], "lat": float(m["lat"]), "lon": float(m["lon"])}
            for m in TOWN_RE.finditer(path.read_text(encoding="utf-8", errors="replace"))]


def dist(a: Pos, b: Pos) -> float:
    return math.dist((a.x, a.y), (b.x, b.y))


def pick_links(objs: list[dict[str, Any]], k: int, max_m: float) -> list[tuple[int, int]]:
    """Each objective to its k nearest within max_m; undirected, no duplicates."""
    links: set[tuple[int, int]] = set()
    for i, a in enumerate(objs):
        near = sorted((dist(a["p"], b["p"]), j) for j, b in enumerate(objs) if j != i)
        for d, j in near[:k]:
            if d <= max_m:
                links.add((min(i, j), max(i, j)))
    return sorted(links)


class Probe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.done = False

    def on_hello(self, conn: Connection, hello: Hello, new_run: bool) -> None:
        print(f"HELLO   {conn.src} new_run={new_run}", flush=True)
        if conn.src == Src.MISSION and not self.done:
            self.done = True
            asyncio.create_task(self.run(conn))

    def on_disconnect(self, conn: Connection) -> None:
        print(f"DISCONNECT {conn.src}", flush=True)

    def on_message(self, conn: Connection, env: Envelope) -> None:
        pass

    async def q(self, conn: Connection, data: dict[str, Any]) -> tuple[Any, float]:
        t = time.monotonic()
        res = await conn.call("query", data, LONG)
        return res, time.monotonic() - t

    async def run(self, conn: Connection) -> None:
        try:
            await self._run(conn)
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"FAILED  {type(e).__name__}: {e}", flush=True)

    async def _run(self, conn: Connection) -> None:
        a = self.args
        home = Pos(*AIRBASE_XY)
        radius = a.radius_nm * NM
        await conn.call("message", {"audience": "all", "duration": 30,
                                    "text": "P6 theater probe: extracting map data; expect stutters for a few minutes."})

        # 1. airbases
        res, t = await self.q(conn, {"what": "airbases"})
        abs_ = [b for b in res["airbases"] if b.get("pos")]
        cats: dict[Any, int] = {}
        for b in abs_:
            cats[b.get("category")] = cats.get(b.get("category"), 0) + 1
        airdromes = [b for b in abs_ if b.get("category") == 0]
        print(f"AIRBASES {len(abs_)} in {t * 1000:.0f} ms; by category (0 airdrome, 1 helipad, 2 ship): {cats}", flush=True)
        sample = next((b for b in airdromes if b["name"] == AIRBASE_NAME), airdromes[0] if airdromes else None)
        if sample:
            print(f"         e.g. {sample['name']}: id {sample['id']}, {len(sample.get('runways') or [])} runway(s) "
                  f"{[(r.get('name'), round(r.get('length') or 0)) for r in sample.get('runways') or []]}, "
                  f"{sample.get('parking')} parking spots", flush=True)

        # 2. towns
        towns = read_towns(TOWNS_LUA)
        t0 = time.monotonic()
        for i in range(0, len(towns), 200):
            chunk = towns[i:i + 200]
            res = await conn.call("query", {"what": "ll_to_xy", "points": [{"lat": x["lat"], "lon": x["lon"]}
                                                                         for x in chunk]}, LONG)
            for x, p in zip(chunk, res["points"]):
                x["p"] = Pos(p["x"], p["y"])
        near = [x for x in towns if dist(x["p"], home) <= radius]
        print(f"TOWNS   {len(towns)} in towns.lua, converted in {(time.monotonic() - t0) * 1000:.0f} ms; "
              f"{len(near)} within {a.radius_nm:.0f} nm of {AIRBASE_NAME}", flush=True)

        # 3. size by scenery count
        type_counts: dict[str, int] = {}
        t0 = time.monotonic()
        slowest = 0.0
        for x in near:
            res, t = await self.q(conn, {"what": "scenery", "center": {"x": x["p"].x, "y": x["p"].y},
                                         "radius": a.town_radius_m})
            slowest = max(slowest, t)
            x["objects"] = res["total"]
            for k, n in (res.get("counts") or {}).items():
                type_counts[k] = type_counts.get(k, 0) + n
        near.sort(key=lambda x: -x["objects"])
        print(f"SIZE    {len(near)} places searched in {time.monotonic() - t0:.1f} s (slowest {slowest * 1000:.0f} ms); "
              f"largest: " + ", ".join(f"{x['name']} {x['objects']}" for x in near[:12]), flush=True)
        top = sorted(type_counts.items(), key=lambda kv: -kv[1])[:40]
        print(f"TYPES   {len(type_counts)} scenery type names near towns; most common: "
              + ", ".join(f"{k} {n}" for k, n in top), flush=True)

        # 4. bridges
        bridges: list[dict[str, Any]] = []
        bridge_types: dict[str, int] = {}
        br = a.bridge_radius_nm * NM
        tile = 10_000.0
        t0 = time.monotonic()
        n_tiles = 0
        steps = int(math.ceil(br / (tile * 1.4)))
        for ix in range(-steps, steps + 1):
            for iy in range(-steps, steps + 1):
                c = Pos(home.x + ix * tile * 1.4, home.y + iy * tile * 1.4)
                if dist(c, home) > br + tile:
                    continue
                res, t = await self.q(conn, {"what": "scenery", "center": {"x": c.x, "y": c.y}, "radius": tile,
                                             "match": BRIDGE_WORDS, "limit": 2000})
                n_tiles += 1
                for f in res.get("found") or []:
                    if f.get("pos") and not any(dist(Pos(f["pos"]["x"], f["pos"]["y"]), Pos(b["pos"]["x"], b["pos"]["y"])) < 30
                                                for b in bridges):
                        bridges.append(f)
                        bridge_types[f["type"]] = bridge_types.get(f["type"], 0) + 1
        print(f"BRIDGES {len(bridges)} within {a.bridge_radius_nm:.0f} nm ({n_tiles} searches, "
              f"{time.monotonic() - t0:.1f} s); types: {bridge_types}", flush=True)

        # 5. objectives and links
        objs: list[dict[str, Any]] = []
        for b in airdromes:
            p = Pos(b["pos"]["x"], b["pos"]["y"])
            if dist(p, home) <= radius:
                objs.append({"name": b["name"], "kind": "airbase", "p": p})
        for x in near[:a.towns]:
            objs.append({"name": x["name"], "kind": "town", "p": x["p"], "objects": x["objects"]})
        links = pick_links(objs, a.k, a.link_max_nm * NM)
        print(f"LINKS   {len(objs)} objectives, {len(links)} links; asking DCS for road routes...", flush=True)
        routes = []
        t0 = time.monotonic()
        times = []
        for i, j in links:
            A, B = objs[i], objs[j]
            sa, _ = await self.q(conn, {"what": "road_point", "pos": {"x": A["p"].x, "y": A["p"].y}})
            sb, _ = await self.q(conn, {"what": "road_point", "pos": {"x": B["p"].x, "y": B["p"].y}})
            straight = dist(A["p"], B["p"])
            entry: dict[str, Any] = {"a": A["name"], "b": B["name"], "straight_m": round(straight)}
            if sa and sa.get("found") and sb and sb.get("found"):
                res, t = await self.q(conn, {"what": "road_path", "from": sa["pos"], "to": sb["pos"]})
                times.append(t)
                pts = [Pos(p["x"], p["y"]) for p in res.get("points") or []]
                if len(pts) >= 2:
                    path = RoadPath(pts)
                    entry.update(road_m=round(path.length), ratio=round(path.length / max(straight, 1), 2),
                                 points=[[round(p.x), round(p.y)] for p in simplify(pts, 10)], ms=round(t * 1000))
            routes.append(entry)
        ok = [r for r in routes if "road_m" in r]
        odd = [r for r in ok if r["ratio"] > 2.0]
        times.sort()
        print(f"ROUTES  {len(ok)}/{len(routes)} links have a road route ({time.monotonic() - t0:.0f} s; per route "
              f"median {times[len(times) // 2] * 1000:.0f} ms, max {times[-1] * 1000:.0f} ms)" if times else
              "ROUTES  none", flush=True)
        for r in odd:
            print(f"        long way round: {r['a']} - {r['b']}: {r['road_m'] / NM:.1f} nm by road, "
                  f"{r['straight_m'] / NM:.1f} nm straight", flush=True)
        for r in routes:
            if "road_m" not in r:
                print(f"        no road route: {r['a']} - {r['b']}", flush=True)

        # 6. save and draw
        out = a.out
        out.mkdir(parents=True, exist_ok=True)
        data = {
            "center": {"name": AIRBASE_NAME, "x": home.x, "y": home.y}, "radius_nm": a.radius_nm,
            "airbases": [{k: v for k, v in b.items()} for b in abs_],
            "places": [{"name": x["name"], "lat": x["lat"], "lon": x["lon"], "x": round(x["p"].x), "y": round(x["p"].y),
                        "objects": x.get("objects")} for x in near],
            "scenery_types": dict(top),
            "bridges": bridges,
            "objectives": [{"name": o["name"], "kind": o["kind"], "x": round(o["p"].x), "y": round(o["p"].y)} for o in objs],
            "links": routes,
        }
        (out / "syria-probe.json").write_text(json.dumps(data, indent=1))
        draw_map(out / "syria-probe.png", home, radius, near, objs, routes, bridges)
        print(f"WROTE   {out / 'syria-probe.json'} and {out / 'syria-probe.png'}", flush=True)
        await conn.call("message", {"audience": "all", "duration": 20, "text": "P6 theater probe: done."})


def draw_map(path: Path, home: Pos, radius: float, places: list, objs: list, routes: list, bridges: list) -> None:
    size = 1600
    img = Image.new("RGB", (size, size), (238, 232, 215))
    d = ImageDraw.Draw(img)
    scale = size / (2 * radius * 1.05)

    def px(x: float, y: float) -> tuple[float, float]:   # north up, east right
        return size / 2 + (y - home.y) * scale, size / 2 - (x - home.x) * scale

    small = ImageFont.load_default(size=14)
    big = ImageFont.load_default(size=18)
    for nm_ring in (20, 40, 60):
        r = nm_ring * NM * scale
        d.ellipse([size / 2 - r, size / 2 - r, size / 2 + r, size / 2 + r], outline=(200, 190, 170))
    for r in routes:
        if "points" in r:
            color = (60, 140, 70) if r["ratio"] <= 2.0 else (220, 140, 30)
            d.line([px(x, y) for x, y in r["points"]], fill=color, width=3)
    for p in places:
        x, y = px(p["p"].x, p["p"].y)
        s = 2 + min(8, math.sqrt(p.get("objects") or 0) / 6)
        d.ellipse([x - s, y - s, x + s, y + s], fill=(120, 120, 120))
    for b in bridges:
        x, y = px(b["pos"]["x"], b["pos"]["y"])
        d.line([x - 5, y - 5, x + 5, y + 5], fill=(200, 30, 30), width=2)
        d.line([x - 5, y + 5, x + 5, y - 5], fill=(200, 30, 30), width=2)
    for o in objs:
        x, y = px(o["p"].x, o["p"].y)
        if o["kind"] == "airbase":
            d.rectangle([x - 7, y - 7, x + 7, y + 7], fill=(40, 80, 170))
            d.text((x + 10, y - 10), o["name"], fill=(40, 80, 170), font=big)
        else:
            d.ellipse([x - 6, y - 6, x + 6, y + 6], outline=(20, 20, 20), width=2)
            d.text((x + 9, y - 8), o["name"], fill=(20, 20, 20), font=small)
    d.text((20, 20), f"P6 probe: {AIRBASE_NAME} +/- {radius / NM:.0f} nm. Blue squares: airdromes; circles: "
                     "objective towns; grey dots: places (size = buildings); green: road links; "
                     "orange: road over 2x straight; red x: bridges", fill=(0, 0, 0), font=small)
    img.save(path)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--radius-nm", type=float, default=60.0)
    ap.add_argument("--towns", type=int, default=40, help="objective towns: this many of the largest places")
    ap.add_argument("--town-radius-m", type=float, default=1000.0)
    ap.add_argument("--bridge-radius-nm", type=float, default=30.0)
    ap.add_argument("--k", type=int, default=3, help="links per objective to its nearest neighbours")
    ap.add_argument("--link-max-nm", type=float, default=25.0)
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "theater")
    ap.add_argument("--port", type=int, default=7341)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    probe = Probe(args)
    bridge = Bridge(probe.on_message, probe.on_hello, probe.on_disconnect, port=args.port)
    await bridge.start()
    print(f"listening on 127.0.0.1:{bridge.port}; start campaign-dev/campaign-smoke.miz", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
