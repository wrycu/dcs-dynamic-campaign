"""A picture of a scenario's starting state: who holds what, the front, squadrons, formations and
air defence ranges. Used by ``python3 -m campaign.scenario_file <name>``."""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from campaign.bubble import strength
from campaign.catalog import threat_range
from campaign.geography import load_geography
from campaign.model import GroundRole, ObjectiveType, Pos, Side
from campaign.theater import theater_dir

NM = 1852.0
SIDE_COLOR = {Side.BLUE: (40, 90, 200), Side.RED: (200, 40, 40), Side.NEUTRAL: (130, 130, 130)}
ROLE_MARK = {GroundRole.ARMOR: "T", GroundRole.MECH: "M", GroundRole.ARTILLERY: "A", GroundRole.SAM: "S",
             GroundRole.AAA: "a", GroundRole.INFANTRY: "I", GroundRole.SUPPLY: "L", GroundRole.HQ: "H"}


def draw(path: Path, sd, built) -> None:
    from campaign.scenario_file import front_links
    theater, st = built.theater, built.state
    objs = theater.objectives
    center = objs[sd.center].pos
    radius = sd.radius_nm * NM * 1.08
    size = 2000
    scale = size / (2 * radius)
    img = Image.new("RGB", (size, size), (240, 235, 220))
    d = ImageDraw.Draw(img, "RGBA")

    def px(p: Pos) -> tuple[float, float]:
        return size / 2 + (p.y - center.y) * scale, size / 2 - (p.x - center.x) * scale

    small = ImageFont.load_default(size=12)
    mid = ImageFont.load_default(size=15)
    big = ImageFont.load_default(size=22)
    geo = load_geography(theater_dir(sd.map))
    water = (170, 200, 225)
    if geo.water is not None:
        w = geo.water
        for i, runs in enumerate(w.rows):
            j, wet = 0, False
            for run in runs:
                if wet:
                    a = px(Pos(w.x0 + (i + 0.5) * w.cell_m, w.y0 + (j - 0.5) * w.cell_m))
                    b = px(Pos(w.x0 + (i - 0.5) * w.cell_m, w.y0 + (j + run - 0.5) * w.cell_m))
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
            d.line([px(Pos(*q)) for q in poly[0] + [poly[0][0]]], fill=(120, 60, 60), width=2)
    r = sd.radius_nm * NM * scale
    d.ellipse([size / 2 - r, size / 2 - r, size / 2 + r, size / 2 + r], outline=(150, 140, 120), width=2)

    # Air defence ranges under everything else.
    for u in st.ground_units.values():
        rng = threat_range(u.composition)
        if u.role is GroundRole.SAM and rng >= 8_000:
            x, y = px(u.pos)
            rr = rng * scale
            col = SIDE_COLOR[u.side]
            d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=col + (18,), outline=col + (90,))

    front = {frozenset(p) for p in front_links(built)}
    for ln in theater.links:
        if ln.a not in st.objectives or ln.b not in st.objectives:
            continue
        pts = [px(p) for p in ln.route] if ln.route else [px(objs[ln.a].pos), px(objs[ln.b].pos)]
        if frozenset((ln.a, ln.b)) in front:
            d.line(pts, fill=(230, 140, 0), width=6)
        else:
            d.line(pts, fill=(110, 150, 100), width=2)

    for oid, os_ in st.objectives.items():
        o = objs[oid]
        x, y = px(o.pos)
        col = SIDE_COLOR[os_.owner]
        if o.type is ObjectiveType.AIRBASE:
            d.rectangle([x - 8, y - 8, x + 8, y + 8], fill=col)
            d.text((x + 11, y - 9), o.name, fill=col, font=mid)
        elif o.type is ObjectiveType.CITY:
            rr = 3 + 2 * o.priority
            d.ellipse([x - rr, y - rr, x + rr, y + rr], outline=col, width=2)
            if o.priority >= 3:
                d.text((x + rr + 2, y - 7), o.name, fill=col, font=small)
        elif o.type is ObjectiveType.BRIDGE:
            d.line([x - 3, y - 3, x + 3, y + 3], fill=col, width=2)
            d.line([x - 3, y + 3, x + 3, y - 3], fill=col, width=2)
        elif o.area_of is not None:
            rr = 2 + o.priority
            d.polygon([(x, y - rr), (x + rr, y), (x, y + rr), (x - rr, y)], fill=col)
        else:
            d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=col)

    for u in st.ground_units.values():
        x, y = px(u.pos)
        col = SIDE_COLOR[u.side]
        d.rectangle([x - 6, y - 6, x + 6, y + 6], fill=(255, 255, 255), outline=col, width=2)
        d.text((x - 3, y - 7), ROLE_MARK.get(u.role, "?"), fill=col, font=small)

    by_base: dict[str, list] = {}
    for sq in st.squadrons.values():
        by_base.setdefault(sq.base_id, []).append(sq)
    for base, sqs in by_base.items():
        x, y = px(objs[base].pos)
        for i, sq in enumerate(sqs):
            d.text((x + 11, y + 8 + 15 * i), f"{sq.aircraft_type} x{sq.aircraft_ready}{' (players)' if sq.player_flyable else ''}",
                   fill=SIDE_COLOR[sq.side], font=small)

    blue = [u for u in st.ground_units.values() if u.side is Side.BLUE]
    red = [u for u in st.ground_units.values() if u.side is Side.RED]
    d.text((16, 14), f"{sd.title}: start", fill=(0, 0, 0), font=big)
    d.text((16, 44), f"Blue {sd.sides[Side.BLUE].get('name', '')}: {len(blue)} formations "
                     f"({sum(strength(u) for u in blue)} vehicles), "
                     f"{sum(s.aircraft_ready for s in st.squadrons.values() if s.side is Side.BLUE)} aircraft. "
                     f"Red {sd.sides[Side.RED].get('name', '')}: {len(red)} formations "
                     f"({sum(strength(u) for u in red)} vehicles), "
                     f"{sum(s.aircraft_ready for s in st.squadrons.values() if s.side is Side.RED)} aircraft. "
                     f"Front: {len(front)} links.", fill=(0, 0, 0), font=mid)
    d.text((16, 66), "Squares: airbases; circles: towns; diamonds: infrastructure; x: bridges (colour = owner, grey = "
                     "neutral). Boxes: formations (T tank, M mech, A artillery, S SAM, a AAA); shaded: SAM ranges. "
                     "Orange: the front (links between blue and red).", fill=(60, 60, 60), font=small)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
