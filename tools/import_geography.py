"""Country borders, rivers and lakes for a map, from Natural Earth (public domain), no DCS needed.

    python3 -m tools.import_geography [--map Syria]

Downloads the 1:10m Natural Earth layers once (into build/naturalearth/, from the project's GitHub
at a fixed version), keeps what touches the map's area (from data/theaters/<map>/map.json),
converts it to map coordinates (campaign.geo, which matches DCS within centimetres), simplifies
it, and writes data/theaters/<map>/geography.json. Then rebuild the map layer
(``python3 -m tools.extract_theater --offline``) to give every objective its country.

The files are only parsed as JSON.
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from campaign.geo import TERRAINS, ll_to_xy
from campaign.model import Pos
from campaign.roads import simplify
from campaign.theater import theater_dir

ROOT = Path(__file__).resolve().parent.parent
NE_VERSION = "v5.1.2"
NE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/{v}/geojson/{name}.geojson"
LAYERS = {"countries": "ne_10m_admin_0_countries", "rivers": "ne_10m_rivers_lake_centerlines",
          "lakes": "ne_10m_lakes"}
SIMPLIFY_M = 150
SOURCE = f"Natural Earth 1:10m {NE_VERSION} (public domain, naturalearthdata.com)"


def fetch(name: str) -> dict:
    path = ROOT / "build" / "naturalearth" / NE_VERSION / f"{name}.geojson"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        url = NE_URL.format(v=NE_VERSION, name=name)
        print(f"downloading {url}", flush=True)
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
        json.loads(data)   # only JSON is kept
        path.write_bytes(data)
    return json.loads(path.read_text(encoding="utf-8"))


def rings_of(geom: dict) -> list[list[list]]:
    """Polygons (outer ring + holes) of a Polygon/MultiPolygon, as lon/lat lists."""
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return geom["coordinates"]
    return []


def lines_of(geom: dict) -> list[list]:
    if geom["type"] == "LineString":
        return [geom["coordinates"]]
    if geom["type"] == "MultiLineString":
        return geom["coordinates"]
    return []


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--map", default="Syria")
    a = ap.parse_args()
    if a.map not in TERRAINS:
        raise SystemExit(f"no projection for {a.map!r} in campaign/geo.py")
    out_dir = theater_dir(a.map)
    header = json.loads((out_dir / "map.json").read_text(encoding="utf-8").split(', "theater":')[0] + "}")
    ar = header["area"]
    r = ar["radius_nm"] * 1852 + 50_000
    box = (ar["x"] - r, ar["y"] - r, ar["x"] + r, ar["y"] + r)

    def conv(pts: list) -> list[Pos]:
        return [ll_to_xy(a.map, lat, lon) for lon, lat, *_ in pts]

    def touches(ps: list[Pos]) -> bool:
        return any(box[0] <= p.x <= box[2] and box[1] <= p.y <= box[3] for p in ps)

    def tidy(ps: list[Pos]) -> list[list[float]]:
        return [[round(p.x), round(p.y)] for p in simplify(ps, SIMPLIFY_M)]

    result: dict = {"format": 1, "source": SOURCE, "countries": [], "rivers": [], "lakes": []}
    for kind in ("countries", "lakes"):
        for f in fetch(LAYERS[kind])["features"]:
            if not f.get("geometry"):
                continue
            name = f["properties"].get("NAME") or f["properties"].get("name") or "?"
            polys = []
            for poly in rings_of(f["geometry"]):
                rings = [conv(ring) for ring in poly]
                if rings and touches(rings[0]):
                    polys.append([tidy(rg) for rg in rings if len(rg) >= 3])
            if polys:
                result[kind].append({"name": name, "polygons": polys})
    rivers: dict[str, list] = {}
    for f in fetch(LAYERS["rivers"])["features"]:
        if not f.get("geometry"):
            continue
        name = f["properties"].get("name") or f["properties"].get("name_en") or "river"
        for ln in lines_of(f["geometry"]):
            ps = conv(ln)
            if touches(ps):
                rivers.setdefault(name, []).append(tidy(ps))
    result["rivers"] = [{"name": k, "lines": v} for k, v in sorted(rivers.items())]
    path = out_dir / "geography.json"
    path.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    print(f"WROTE   {path}: {len(result['countries'])} countries ({', '.join(c['name'] for c in result['countries'])}), "
          f"{len(result['lakes'])} lakes, {len(result['rivers'])} rivers; {path.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
