"""Build a map layer (backlog item 7, roadmap D3) from DCS: data/theaters/<map>/map.json.

    python3 -m tools.extract_theater [--min-buildings 800]          # the whole map
    python3 -m tools.extract_theater --center Khalkhalah --radius-nm 100   # just an area
    python3 -m tools.extract_theater --offline      # rebuild from the cache, no DCS

With DCS: start this, then start campaign-dev/campaign-smoke.miz and take the jet; keep DCS
focused until "WROTE" (a few minutes; expect stutters). It asks DCS for airbases, places
(Mods/terrains/<map>/map/towns.lua), buildings around each town, bridges and road routes, and
applies the rules in campaign/theater_build.py and the hand edits in data/theaters/<map>/edits.toml.

Every DCS answer is cached in build/theater/<map>-cache.json. After changing edits.toml,
``--offline`` rebuilds without DCS, unless the edits need something new (a link's road route,
a new town's size); it then lists what's missing, and a run with DCS fills it in.

Writes data/theaters/<map>/map.json and a picture to check it by, build/theater/<map>-map.png.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import math
import time
import tomllib
from pathlib import Path
from typing import Any, Awaitable, Callable

from PIL import Image, ImageDraw, ImageFont

from campaign.bridge import Bridge, BridgeError, Connection
from campaign.geography import Geography, load_geography, water_rows
from campaign.model import Link, ObjectiveType, Pos, Theater
from campaign.protocol import Envelope, Hello, Src
from campaign.roads import simplify
from campaign.theater import dumps, theater_dir
from campaign.theater_build import (Edits, Place, Town, airbase_objective, apply_edits_to, candidate_links,
                                    clean_name, dist, infra_objectives,
                                    is_building, is_helipad,
                                    make_link, merge_bridges, merge_places, pick_objectives)
from tools.build_test_missions import PREFIX, ROOT

NM = 1852.0
DCS_DIR = PREFIX / "drive_c" / "Program Files" / "Eagle Dynamics" / "DCS World"
LONG = 300.0
TOWN_RADIUS_M = 1_500
BRIDGE_TILE_M = 15_000
BRIDGE_WORDS = ["bridge", "most"]
# Scenery type names that may be infrastructure worth a target: positions are kept for a survey
# (build/theater/<map>-scenery.json) until there are rules that make objectives of them.
INFRA_WORDS = ["refin", "oil_", "_oil", "tank", "fuel", "petrol", "power", "plant", "tpp", "chimney", "pipe",
               "factory", "industr", "silo", "elevator", "substation", "transformer", "electr", "dam_", "_dam",
               "port", "crane", "pier", "dock", "antenna", "radio", "mast", "station", "depot", "storage", "cement",
               "mill", "water", "pump", "hangar", "warehouse", "rail", "train"]
ROUTE_SIMPLIFY_M = 10
WATER_CELL_M = 1_000
WATER_ROWS_PER_QUERY = 40


class Missing(Exception):
    """Offline, and the cache doesn't have an answer."""


Query = Callable[[dict[str, Any]], Awaitable[Any]]


class Cache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict] = {}
        if path.exists():
            self.data = json.loads(path.read_text())

    def get(self, section: str, key: str) -> Any:
        return self.data.get(section, {}).get(key)

    def put(self, section: str, key: str, value: Any) -> Any:
        self.data.setdefault(section, {})[key] = value
        return value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data))


def read_towns(path: Path) -> list[dict[str, Any]]:
    import re
    rx = re.compile(r'^\["(?P<name>(?:[^"\\]|\\.)*)"\]\s*=\s*\{\s*latitude\s*=\s*(?P<lat>-?[\d.]+),\s*'
                    r'longitude\s*=\s*(?P<lon>-?[\d.]+)', re.M)
    return [{"name": m["name"], "lat": float(m["lat"]), "lon": float(m["lon"])}
            for m in rx.finditer(path.read_text(encoding="utf-8", errors="replace"))]


def key_pos(p: Pos) -> str:
    return f"{round(p.x, -1):.0f},{round(p.y, -1):.0f}"


class Builder:
    def __init__(self, args: argparse.Namespace, q: Query | None, cache: Cache) -> None:
        self.a = args
        self.q = q
        self.cache = cache
        self.missing: list[str] = []

    async def ask(self, section: str, key: str, req: dict[str, Any], reduce=lambda r: r) -> Any:
        hit = self.cache.get(section, key)
        if hit is not None:
            return hit
        if self.q is None:
            raise Missing(f"{section} {key}")
        value = self.cache.put(section, key, reduce(await self.q(req)))
        self.asked()
        return value

    def asked(self) -> None:
        """Save the cache now and then, so an interrupted run keeps what DCS answered."""
        self.n_asked = getattr(self, "n_asked", 0) + 1
        if self.n_asked % 200 == 0:
            self.cache.save()

    def log(self, text: str) -> None:
        print(text, flush=True)

    async def build(self) -> tuple[Theater, dict[str, Any]]:
        a = self.a
        t_all = time.monotonic()
        res = await self.ask("airbases", "all", {"what": "airbases"})
        airbases = [b for b in res["airbases"] if b.get("pos")]
        if a.center:
            center = next((b for b in airbases if b["name"] == a.center), None)
            if center is None:
                raise SystemExit(f"no airbase {a.center!r} on this map")
            home = Pos(center["pos"]["x"], center["pos"]["y"])
            radius = a.radius_nm * NM
            box = (home.x - radius, home.y - radius, home.x + radius, home.y + radius)
            where = f"within {a.radius_nm:.0f} nm of {a.center}"
        else:   # the whole map: every airbase plus a margin
            m = 30 * NM
            box = (min(b["pos"]["x"] for b in airbases) - m, min(b["pos"]["y"] for b in airbases) - m,
                   max(b["pos"]["x"] for b in airbases) + m, max(b["pos"]["y"] for b in airbases) + m)
            home = Pos((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            radius = max(box[2] - box[0], box[3] - box[1]) / 2
            where = f"on the map ({(box[2] - box[0]) / NM:.0f} x {(box[3] - box[1]) / NM:.0f} nm)"
        self.area = (home, radius)

        def inside(p: Pos) -> bool:
            if a.center:
                return dist(p, home) <= radius
            return box[0] <= p.x <= box[2] and box[1] <= p.y <= box[3]

        airdromes = [o for o in (airbase_objective(b) for b in airbases) if o and inside(o.pos)]
        helipads = sum(1 for b in airbases if is_helipad(b.get("name") or ""))
        self.log(f"AIRBASES {len(airbases)} on the map; {len(airdromes)} airbases {where} ({helipads} helipads "
                 f"on the map, not used)")

        # Places, converted with DCS's projection.
        towns_lua = DCS_DIR / "Mods" / "terrains" / a.map / "map" / "towns.lua"
        raw = read_towns(towns_lua)
        need = [x for x in raw if self.cache.get("ll", f"{x['lat']},{x['lon']}") is None]
        if need and self.q is None:
            raise Missing(f"{len(need)} place positions")
        for i in range(0, len(need), 200):
            chunk = need[i:i + 200]
            res = await self.q({"what": "ll_to_xy", "points": [{"lat": x["lat"], "lon": x["lon"]} for x in chunk]})
            for x, p in zip(chunk, res["points"]):
                self.cache.put("ll", f"{x['lat']},{x['lon']}", [p["x"], p["y"]])
        places = []
        for x in raw:
            xy = self.cache.get("ll", f"{x['lat']},{x['lon']}")
            p = Pos(xy[0], xy[1])
            if inside(p):
                places.append(Place(clean_name(x["name"]), p))
        towns, military = merge_places(places)
        self.log(f"PLACES  {len(raw)} in towns.lua, {len(places)} in the area: {len(towns)} towns after merging, "
                 f"{len(military)} military sites")

        # Town size: buildings within TOWN_RADIUS_M.
        t0 = time.monotonic()
        for i, t in enumerate(towns):
            if i and i % 200 == 0:
                self.log(f"        {i}/{len(towns)} towns sized ({time.monotonic() - t0:.0f} s)")
            try:
                t.buildings = await self.ask(
                    "buildings", f"{key_pos(t.pos)}/{TOWN_RADIUS_M}",
                    {"what": "scenery", "center": {"x": t.pos.x, "y": t.pos.y}, "radius": TOWN_RADIUS_M},
                    lambda r: sum(n for k, n in (r.get("counts") or {}).items() if is_building(k)))
            except Missing as e:
                self.missing.append(f"size of {t.name}: {e}")
        towns.sort(key=lambda t: -t.buildings)
        self.log(f"SIZE    {len(towns)} towns in {time.monotonic() - t0:.1f} s; largest: "
                 + ", ".join(f"{t.name} {t.buildings}" for t in towns[:10]))

        edits = load_edits(theater_dir(a.map) / "edits.toml")
        objs = pick_objectives(airdromes, towns, military, a.min_buildings, edits, a.max_towns)

        # Bridges (and a survey of other scenery) in the area, in tiles. Tiles overlap, so the
        # survey's counts are approximate; positions are de-duplicated by object id.
        t0 = time.monotonic()
        found: list[dict] = []
        census: dict[str, int] = {}
        step = BRIDGE_TILE_M * 1.4
        n = int(math.ceil(radius / step))
        tiles = [Pos(home.x + ix * step, home.y + iy * step) for ix in range(-n, n + 1) for iy in range(-n, n + 1)]
        tiles = [c for c in tiles if inside(c) or any(inside(Pos(c.x + dx, c.y + dy)) for dx, dy in
                                                      ((step, 0), (-step, 0), (0, step), (0, -step)))]
        self.log(f"SCENERY searching {len(tiles)} tiles of {BRIDGE_TILE_M / 1000:.0f} km...")
        for i, c in enumerate(tiles):
            if i and i % 200 == 0:
                self.log(f"        {i}/{len(tiles)} tiles ({time.monotonic() - t0:.0f} s)")
            try:
                tile = await self.ask(
                    "scenery", f"{key_pos(c)}/{BRIDGE_TILE_M}",
                    {"what": "scenery", "center": {"x": c.x, "y": c.y}, "radius": BRIDGE_TILE_M,
                     "match": BRIDGE_WORDS + INFRA_WORDS, "limit": 20000},
                    lambda r: {"counts": r.get("counts") or {},
                               "found": [{"id": f.get("id"), "type": f["type"], "x": round(f["pos"]["x"], 1),
                                          "y": round(f["pos"]["y"], 1)}
                                         for f in r.get("found") or [] if f.get("pos")]})
                found += tile["found"]
                for k, v in tile["counts"].items():
                    census[k] = census.get(k, 0) + v
            except Missing as e:
                self.missing.append(f"scenery: {e}")
        seen: set = set()
        unique = []
        for f in found:
            if f.get("id") is not None and f["id"] not in seen:
                seen.add(f["id"])
                unique.append(f)
        is_bridge = lambda f: any(w in f["type"].lower() for w in BRIDGE_WORDS)  # noqa: E731
        bridges = merge_bridges([f for f in unique if is_bridge(f)])
        self.survey = {"census": dict(sorted(census.items(), key=lambda kv: -kv[1])),
                       "infra": [f for f in unique if not is_bridge(f)]}
        infra_types: dict[str, int] = {}
        for f in self.survey["infra"]:
            infra_types[f["type"]] = infra_types.get(f["type"], 0) + 1
        self.log(f"BRIDGES {len(bridges)} in the area ({time.monotonic() - t0:.1f} s); survey: {len(census)} scenery "
                 f"types, {len(self.survey['infra'])} objects that may be infrastructure in {len(infra_types)} types")

        # Links and their road routes.
        t0 = time.monotonic()
        pairs = candidate_links(objs, a.link_max_nm * NM, edits)
        links: list[Link] = []
        no_road = []
        self.log(f"ROUTES  {len(pairs)} links between {len(objs)} objectives...")
        for i, (x, y) in enumerate(pairs):
            if i and i % 100 == 0:
                self.log(f"        {i}/{len(pairs)} routes ({time.monotonic() - t0:.0f} s)")
            A, B = objs[x].pos, objs[y].pos
            try:
                route = await self.route(A, B)
            except Missing as e:
                self.missing.append(f"route {x} - {y}: {e}")
                route = []
            ln = make_link(x, y, objs, route, bridges)
            if not route:
                ln.kind = "offroad"
                no_road.append(f"{objs[x].name} - {objs[y].name}")
            links.append(ln)
        long = [ln for ln in links if ln.route and ln.length_m > 2.5 * dist(objs[ln.a].pos, objs[ln.b].pos)]
        self.log(f"LINKS   {len(links)} ({time.monotonic() - t0:.1f} s); {len(no_road)} without a road route; "
                 f"{len(long)} over 2.5x the straight line; "
                 f"{sum(1 for o in objs.values() if o.type is ObjectiveType.BRIDGE)} bridges on routes")
        for s in no_road:
            self.log(f"        no road: {s}")
        for ln in long:
            self.log(f"        long way round: {objs[ln.a].name} - {objs[ln.b].name}: "
                     f"{ln.length_m / NM:.0f} nm by road, {dist(objs[ln.a].pos, objs[ln.b].pos) / NM:.0f} nm straight")
        self.log(f"TOTAL   {time.monotonic() - t_all:.0f} s")

        infra = infra_objectives(self.survey["infra"], objs)
        apply_edits_to(infra, edits)
        objs.update(infra)
        kinds: dict[str, int] = {}
        for o in infra.values():
            kinds[o.type.name] = kinds.get(o.type.name, 0) + 1
        self.log(f"INFRA   {len(infra)} infrastructure objectives: {kinds}")
        known = ({o.id for o in airdromes} | {t.id for t in towns} | {m.id for m in military}
                 | {x.id for x in edits.add} | set(infra))
        unknown = (edits.drop | edits.keep | set(edits.rename)) - known
        for oid in sorted(unknown):
            self.log(f"WARNING edits.toml names {oid!r}, which isn't an objective")

        # Water, from DCS's own surface types on a grid.
        t0 = time.monotonic()
        cell = WATER_CELL_M
        x0, y0 = home.x - radius, home.y - radius
        nx = ny = int(2 * radius // cell) + 1
        rows: list[str] = []
        for i in range(0, nx, WATER_ROWS_PER_QUERY):
            n = min(WATER_ROWS_PER_QUERY, nx - i)
            try:
                rows += await self.ask("surface", f"{round(x0 + i * cell)},{round(y0)}/{cell}/{n}x{ny}",
                                       {"what": "surface_grid", "x0": x0 + i * cell, "y0": y0, "cell": cell,
                                        "nx": n, "ny": ny}, lambda r: r["rows"])
            except Missing:
                self.log("WATER   not in the cache yet: a run with DCS adds it (the rest is built anyway)")
                rows = []
                break
        self.water = None
        if rows:
            self.water = {"x0": x0, "y0": y0, "cell_m": cell, "rows": water_rows(rows)}
            wet = sum(r.count("2") + r.count("3") for r in rows)
            self.log(f"WATER   {nx} x {ny} cells of {cell / 1000:.0f} km ({time.monotonic() - t0:.0f} s): "
                     f"{100 * wet / max(1, nx * ny):.0f}% water")

        # Countries, from geography.json (tools/import_geography.py).
        geo = load_geography(theater_dir(a.map))
        if geo.countries:
            for o in objs.values():
                o.country = geo.country_near(o.pos)
            self.log(f"COUNTRY {sum(1 for o in objs.values() if o.country)}/{len(objs)} objectives placed in a country; "
                     f"not: {[o.name for o in objs.values() if not o.country][:8]}")
        else:
            self.log("COUNTRY no geography.json yet: run python3 -m tools.import_geography, then --offline")
        self.geo = geo

        theater = Theater(map=a.map, objectives=objs, links=links)
        header = {"built_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
                  "area": {"center": a.center or "whole map", "x": round(home.x), "y": round(home.y),
                           "radius_nm": round(radius / NM, 1)},
                  "rules": {"min_buildings": a.min_buildings, "max_towns": a.max_towns, "link_max_nm": a.link_max_nm}}
        return theater, header

    async def route(self, A: Pos, B: Pos) -> list[Pos]:
        key = f"{key_pos(A)}|{key_pos(B)}"
        hit = self.cache.get("routes", key)
        if hit is None:
            if self.q is None:
                raise Missing("not cached")
            sa = await self.q({"what": "road_point", "pos": {"x": A.x, "y": A.y}})
            sb = await self.q({"what": "road_point", "pos": {"x": B.x, "y": B.y}})
            pts: list[Pos] = []
            if sa and sa.get("found") and sb and sb.get("found"):
                res = await self.q({"what": "road_path", "from": sa["pos"], "to": sb["pos"]})
                pts = [Pos(p["x"], p["y"]) for p in res.get("points") or []]
            pts = simplify(pts, ROUTE_SIMPLIFY_M) if len(pts) >= 2 else []
            hit = self.cache.put("routes", key, [[round(p.x, 1), round(p.y, 1)] for p in pts])
            self.asked()
        return [Pos(x, y) for x, y in hit]


def load_edits(path: Path) -> Edits:
    if not path.exists():
        return Edits()
    try:
        return Edits.from_dict(tomllib.loads(path.read_text()))
    except (tomllib.TOMLDecodeError, ValueError) as e:
        raise SystemExit(f"{path}: {e}")


COLORS = {ObjectiveType.AIRBASE: (40, 80, 170), ObjectiveType.CITY: (20, 20, 20), ObjectiveType.DEPOT: (150, 60, 150),
          ObjectiveType.BRIDGE: (200, 30, 30)}
INFRA_COLOR = (215, 110, 0)


def draw_map(path: Path, theater: Theater, home: Pos, radius: float, geo: Geography | None = None) -> None:
    size = 2000
    img = Image.new("RGB", (size, size), (238, 232, 215))
    d = ImageDraw.Draw(img)
    scale = size / (2 * radius * 1.05)

    def px(p: Pos) -> tuple[float, float]:   # north up, east right
        return size / 2 + (p.y - home.y) * scale, size / 2 - (p.x - home.x) * scale

    small = ImageFont.load_default(size=13)
    big = ImageFont.load_default(size=17)
    for ring in range(20, int(radius / NM) + 1, 20):
        r = ring * NM * scale
        d.ellipse([size / 2 - r, size / 2 - r, size / 2 + r, size / 2 + r], outline=(225, 218, 200))
    if geo is not None:
        water = (170, 200, 225)
        if geo.water is not None:
            w = geo.water
            for i, runs in enumerate(w.rows):
                j, wet = 0, False
                for run in runs:
                    if wet:
                        a, b = px(Pos(w.x0 + (i + 0.5) * w.cell_m, w.y0 + (j - 0.5) * w.cell_m)), \
                            px(Pos(w.x0 + (i - 0.5) * w.cell_m, w.y0 + (j + run - 0.5) * w.cell_m))
                        d.rectangle([a[0], a[1], b[0], b[1]], fill=water)
                    j += run
                    wet = not wet
        for lake in geo.lakes:
            for poly in lake.polygons:
                d.polygon([px(Pos(*q)) for q in poly[0]], fill=water)
        for lines in geo.rivers.values():
            for ln in lines:
                d.line([px(Pos(*q)) for q in ln], fill=(90, 140, 200), width=2)
        for c in geo.countries:
            for poly in c.polygons:
                d.line([px(Pos(*q)) for q in poly[0] + [poly[0][0]]], fill=(170, 60, 60), width=1)
    objs = theater.objectives
    for ln in theater.links:
        if ln.route:
            d.line([px(p) for p in ln.route], fill=(60, 140, 70), width=3)
        else:
            d.line([px(objs[ln.a].pos), px(objs[ln.b].pos)], fill=(230, 120, 30), width=2)
    for o in objs.values():
        x, y = px(o.pos)
        c = COLORS.get(o.type, (0, 0, 0))
        if o.type is ObjectiveType.BRIDGE:
            d.line([x - 4, y - 4, x + 4, y + 4], fill=c, width=2)
            d.line([x - 4, y + 4, x + 4, y - 4], fill=c, width=2)
        elif o.type is ObjectiveType.AIRBASE:
            d.rectangle([x - 7, y - 7, x + 7, y + 7], fill=c)
            d.text((x + 10, y - 10), o.name, fill=c, font=big)
        elif o.area_of is not None:   # infrastructure: a diamond; named if it matters most
            r = 2 + o.priority
            d.polygon([(x, y - r), (x + r, y), (x, y + r), (x - r, y)], fill=INFRA_COLOR)
            if o.priority >= 4 or o.type is ObjectiveType.DAM:
                d.text((x + r + 3, y - 6), o.name.split(" near ")[0], fill=INFRA_COLOR, font=small)
        else:
            r = 3 + 2 * o.priority
            d.ellipse([x - r, y - r, x + r, y + r], outline=c, width=2)
            d.text((x + r + 3, y - 8), o.name, fill=c, font=small)
    d.text((16, 16), f"{theater.map} map layer, {radius / NM:.0f} nm around the centre. Blue squares: airbases; "
                     "circles: towns (size = priority); purple: military sites; orange diamonds: infrastructure; red x: bridges on routes; "
                     "green: road links; orange: links without a road route; dark red: borders (Natural Earth); blue: water", fill=(0, 0, 0), font=small)
    img.save(path)


def write(builder: Builder, theater: Theater, header: dict[str, Any]) -> None:
    a = builder.a
    out = theater_dir(a.map)
    out.mkdir(parents=True, exist_ok=True)
    (out / "map.json").write_text(dumps(theater, header))
    if getattr(builder, "water", None):
        (out / "water.json").write_text(json.dumps(builder.water, separators=(",", ":")))
        builder.log(f"WROTE   {out / 'water.json'}")
    png = ROOT / "build" / "theater" / f"{a.map.lower()}-map.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    geo = load_geography(out)
    draw_map(png, theater, Pos(header["area"]["x"], header["area"]["y"]), header["area"]["radius_nm"] * NM, geo)
    counts: dict[str, int] = {}
    for o in theater.objectives.values():
        counts[o.type.name] = counts.get(o.type.name, 0) + 1
    builder.log(f"WROTE   {out / 'map.json'}: {counts}, {len(theater.links)} links; picture: {png}")
    if getattr(builder, "survey", None):
        sv = png.parent / f"{a.map.lower()}-scenery.json"
        sv.write_text(json.dumps(builder.survey))
        builder.log(f"WROTE   {sv} (scenery survey)")


async def run_offline(args: argparse.Namespace, cache: Cache) -> None:
    b = Builder(args, None, cache)
    try:
        theater, header = await b.build()
    except Missing as e:
        raise SystemExit(f"offline: the cache doesn't have {e}; run with DCS")
    if b.missing:
        for m in b.missing[:20]:
            b.log(f"MISSING {m}")
        raise SystemExit(f"offline: {len(b.missing)} answers missing from the cache; run with DCS to fill them in")
    write(b, theater, header)


async def run_dcs(args: argparse.Namespace, cache: Cache) -> None:
    done = asyncio.Event()

    async def go(conn: Connection) -> None:
        async def q(req: dict[str, Any]) -> Any:
            return await conn.call("query", req, LONG)

        try:
            await conn.call("message", {"audience": "all", "duration": 30,
                                        "text": "Building the map layer; expect stutters for a few minutes."})
            b = Builder(args, q, cache)
            theater, header = await b.build()
            cache.save()
            write(b, theater, header)
            await conn.call("message", {"audience": "all", "duration": 20, "text": "Map layer: done."})
        except (BridgeError, asyncio.TimeoutError) as e:
            cache.save()
            print(f"FAILED  {type(e).__name__}: {e} (what DCS answered so far is cached)", flush=True)
        finally:
            done.set()

    started = False

    def on_hello(conn: Connection, hello: Hello, new_run: bool) -> None:
        nonlocal started
        if conn.src == Src.MISSION and not started:
            started = True
            asyncio.get_running_loop().create_task(go(conn))

    def on_message(conn: Connection, env: Envelope) -> None:
        pass

    bridge = Bridge(on_message, on_hello, None, port=args.port)
    await bridge.start()
    print(f"listening on 127.0.0.1:{bridge.port}; start campaign-dev/campaign-smoke.miz and take the jet", flush=True)
    await done.wait()
    await bridge.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a map layer from DCS.")
    ap.add_argument("--map", default="Syria", help="DCS terrain name (Mods/terrains/<map>)")
    ap.add_argument("--center", help="only the area around this airbase (default: the whole map)")
    ap.add_argument("--radius-nm", type=float, default=100.0, help="with --center: the area's radius")
    ap.add_argument("--min-buildings", type=int, default=800, help="towns with at least this many buildings")
    ap.add_argument("--max-towns", type=int, default=0, help="at most this many towns, the largest (0: no limit)")
    ap.add_argument("--link-max-nm", type=float, default=25.0, help="longest link, except to connect the network")
    ap.add_argument("--offline", action="store_true", help="rebuild from the cache only, without DCS")
    ap.add_argument("--port", type=int, default=7341)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    cache = Cache(ROOT / "build" / "theater" / f"{args.map.lower()}-cache.json")
    try:
        asyncio.run(run_offline(args, cache) if args.offline else run_dcs(args, cache))
    except KeyboardInterrupt:
        cache.save()


if __name__ == "__main__":
    main()
