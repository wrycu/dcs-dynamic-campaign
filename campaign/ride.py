"""The ride to the jets (proof P22, Run 30): where the Humvee starts, the order it visits the jets,
where it stops beside each, and its waypoints along the base's taxiways (campaign/taxinet.py).

Pure planning; tools/ride_probe.py drives it in DCS (and the campaign, later). Positions are DCS
map coordinates (x north, y east), metres.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from .model import Pos
from .taxinet import TaxiNet, route_length

STOP_M = 15.0            # the Humvee stops this far ahead of a jet's centre, beside its nose (a
                         # Hornet is 17 m long), side on to it (Run 30: driving into each stand to
                         # face the nose, then looping back out, looked wrong)
APPROACH_M = 15.0        # leaving the last stop, it first drives this far on along the row
IN_M = 35.0              # it joins the row this far before the first stop, so it arrives driving
                         # along the row, not back from past the jet (Run 30, second try)
DIRECT_M = 150.0         # the next stop this close: straight there along the row, not by the taxiways
AWAY_M = 250.0           # after the last stop it drives on to a junction about this far away, as
AWAY_RANGE_M = (120.0, 400.0)   # nearly ahead as there is (Run 30: it went back the way it came);
AWAY_FAR_M = 1500.0      # none ahead that close: look this far
DETOUR = 1.5             # a way off along the taxiways longer than this times the straight line turns
                         # back (jets on an open ramp past the taxiway's end): straight on instead
BESIDE_M = 20.0          # the briefing-room start: this far from the building, towards the taxiways
TOWER_WORDS = ("tower", "kdp")
BUILDING_WORDS = ("hangar", "angar", "build", "bld", "house", "barrack", "office", "shelter", "ukrytie")


@dataclass
class Leg:
    to: Pos                  # where this leg ends
    points: list[Pos]        # the way there, starting where the last leg ended
    jet: int | None          # the jet it stops at (its index), or None (driving off)

    @property
    def length(self) -> float:
        return route_length(self.points)


def _d(a: Pos, b: Pos) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _toward(a: Pos, b: Pos, m: float) -> Pos:
    """The point ``m`` metres from ``a`` towards ``b`` (``b`` itself if nearer)."""
    d = _d(a, b)
    if d <= m or d == 0:
        return b
    return Pos(a.x + (b.x - a.x) * m / d, a.y + (b.y - a.y) * m / d)


ON_NET_M = 5.0           # a jet this close to the network is on its stand's lead-in (DCS's stands
                         # are the ends of short taxiway spokes)
LEAD_IN_M = 30.0         # how far out along the lead-in a parked jet faces


def taxi_entry(net: TaxiNet | None, p: Pos) -> Pos | None:
    """The nearest point on the taxiways (off the runway), or None without a network."""
    return net.nearest(p)[2] if net is not None else None


def lead_in(net: TaxiNet | None, jet: Pos, m: float = LEAD_IN_M) -> Pos | None:
    """The way out of a jet's stand: ``m`` metres from the jet along the taxiway it leaves by. A jet
    on the network (its stand is the end of a spoke) walks out along that spoke from its end; one off
    it goes to the nearest point on it. None without a network."""
    if net is None:
        return None
    e, i, q, d = net.nearest(jet)
    if d > ON_NET_M:
        return q
    pts = e.points
    # q is on segment i (pts[i] to pts[i + 1]); walk away from the end nearer the jet
    if _d(pts[0], jet) <= _d(pts[-1], jet):
        path = [q, *pts[i + 1:]]
    else:
        path = [q, *reversed(pts[:i + 1])]
    left = m
    for a, b in zip(path, path[1:]):
        seg = _d(a, b)
        if seg >= left:
            return _toward(a, b, left)
        left -= seg
    # the spoke is shorter than m: carry on from its far end towards the next point on the network
    return path[-1]


def stop_beside(net: TaxiNet | None, jet: Pos, heading: float | None = None, came_from: Pos | None = None) -> Pos:
    """Where the Humvee stops for a jet: STOP_M ahead of its centre along its heading (radians from
    north; or the way out of its stand, or towards ``came_from``), beside its nose."""
    h = heading if heading is not None else parked_heading(net, jet)
    if h is None:
        if came_from is None or _d(came_from, jet) < 1:
            return jet
        return _toward(jet, came_from, STOP_M)
    return Pos(jet.x + STOP_M * math.cos(h), jet.y + STOP_M * math.sin(h))


def parked_heading(net: TaxiNet | None, jet: Pos) -> float | None:
    """The heading (radians, from north) a jet parked at ``jet`` most likely has: nose towards the
    taxiway it leaves by. None without a network."""
    entry = lead_in(net, jet)
    if entry is None or _d(entry, jet) < 1:
        return None
    return math.atan2(entry.y - jet.y, entry.x - jet.x) % (2 * math.pi)


def way(net: TaxiNet | None, a: Pos, b: Pos) -> list[Pos]:
    """The way from ``a`` to ``b``: along the taxiways, or straight without a network."""
    if net is None:
        return [a, b]
    try:
        return net.route(a, b)
    except ValueError:
        return [a, b]


def _unit(a: Pos, b: Pos) -> tuple[float, float]:
    d = _d(a, b)
    return ((b.x - a.x) / d, (b.y - a.y) / d) if d > 0 else (1.0, 0.0)


def _along(p: Pos, u: tuple[float, float], m: float) -> Pos:
    return Pos(p.x + u[0] * m, p.y + u[1] * m)


def plan(net: TaxiNet | None, start: Pos, jets: list[Pos], headings: list[float | None] | None = None,
         away_m: float = AWAY_M) -> list[Leg]:
    """The ride: from ``start`` past each jet in turn (the nearest next), stopping beside each nose,
    side on, then on and away. Along the taxiways to the row, joining it IN_M before the first stop;
    straight down the row between stops close together (by the taxiways if far apart); then straight
    on along the row, and to a taxiway ahead only if one carries on forward."""
    headings = headings or [None] * len(jets)
    stops = [stop_beside(net, j, h, start) for j, h in zip(jets, headings)]
    order: list[int] = []
    here = start
    left = list(range(len(jets)))
    while left:
        i = min(left, key=lambda k: route_length(way(net, here, stops[k])))
        order.append(i)
        left.remove(i)
        here = stops[i]
    # The direction along the row at each stop: towards the next stop; at the last, the way it came
    # in; for one jet, across its nose, away from the start.
    dirs: list[tuple[float, float]] = []
    for n, i in enumerate(order):
        if n + 1 < len(order):
            dirs.append(_unit(stops[i], stops[order[n + 1]]))
        elif n > 0:
            dirs.append(dirs[-1])
        else:
            h = headings[i] if headings[i] is not None else parked_heading(net, jets[i])
            if h is None:
                dirs.append(_unit(start, stops[i]))
            else:
                u = (-math.sin(h), math.cos(h))          # across the nose
                v = _unit(start, stops[i])
                dirs.append(u if u[0] * v[0] + u[1] * v[1] >= 0 else (-u[0], -u[1]))
    legs: list[Leg] = []
    here = start
    for n, i in enumerate(order):
        s = stops[i]
        if n == 0:
            # Along the taxiways to the row, joining it IN_M before the stop, then down the row.
            u0 = dirs[0]
            into = _along(s, u0, -IN_M)
            pts = [*way(net, here, into), s] if _d(here, s) > IN_M else [here, s]
        elif _d(here, s) <= DIRECT_M:
            pts = [here, s]           # straight down the row
        else:
            pts = way(net, here, s)
        legs.append(Leg(s, pts, i))
        here = s
    # On and away, the way it was going: straight on along the row, then to the taxiway junction
    # most nearly ahead if the way there carries on forward; otherwise straight on (the jets are
    # on an open ramp and the taxiways all lie behind).
    u = dirs[-1] if dirs else (1.0, 0.0)
    out = _along(here, u, APPROACH_M)
    pts = [here, _along(here, u, away_m)]
    if net is not None:
        def ahead_of(p: Pos) -> float:
            v = _unit(out, p)
            return v[0] * u[0] + v[1] * u[1] - abs(_d(out, p) - away_m) / (10 * away_m)
        def is_ahead(p: Pos) -> bool:
            v = _unit(out, p)
            return v[0] * u[0] + v[1] * u[1] > 0.3
        near = [p for p in net.nodes.values() if AWAY_RANGE_M[0] <= _d(out, p) <= AWAY_RANGE_M[1] and is_ahead(p)]
        farther = [p for p in net.nodes.values() if AWAY_RANGE_M[0] <= _d(out, p) <= AWAY_FAR_M and is_ahead(p)]
        for options in (near, farther):
            if not options:
                continue
            target = max(options, key=ahead_of)
            on = way(net, out, target)
            if route_length(on) <= DETOUR * _d(out, target):
                pts = [here, *on]
                break
    pts = cut(pts, away_m)   # gone after away_m: out of sight, not a tour of the base
    legs.append(Leg(pts[-1], pts, None))
    return legs


def cut(points: list[Pos], m: float) -> list[Pos]:
    """The first ``m`` metres of a route."""
    out = [points[0]]
    left = m
    for a, b in zip(points, points[1:]):
        seg = _d(a, b)
        if seg >= left:
            out.append(_toward(a, b, left))
            return out
        out.append(b)
        left -= seg
    return out


def waypoints(points: list[Pos], speed_ms: float) -> list[dict[str, Any]]:
    """DCS ground route points ("Off Road": straight between them; the taxiways aren't DCS roads)."""
    return [{"x": round(p.x, 1), "y": round(p.y, 1), "action": "Off Road", "type": "Turning Point",
             "speed": speed_ms, "speed_locked": True, "ETA": 0, "ETA_locked": False, "formation_template": ""}
            for p in points]


# ---------- a clear spot to start (Run 31: the Humvee started inside a terminal) ----------

CLEAR_M = 4.0            # the start this far from anything (the Humvee is 5 m long)
PATH_CLEAR_M = 2.5       # and its straight line onto the taxiways this far
NO_BOX_M = {"build": 15.0, "hangar": 25.0, "angar": 25.0, "tower": 12.0, "house": 8.0, "barrack": 12.0,
            "shelter": 20.0, "ukrytie": 20.0, "office": 12.0, "bld": 12.0}   # radius when DCS gives no box
NO_BOX_DEFAULT_M = 2.0
SEARCH_M = 150.0         # look this far out from the building
SEARCH_STEP_M = 3.0
SEARCH_DIRS = 24


@dataclass
class Footprint:
    """An object's ground footprint: a polygon (its box, turned as it stands), or a circle."""
    pos: Pos
    corners: list[Pos] | None = None
    radius: float = 0.0
    type: str = ""

    def distance(self, p: Pos) -> float:
        """From ``p`` to the footprint's edge; 0 inside it."""
        if not self.corners:
            return max(0.0, _d(p, self.pos) - self.radius)
        if _inside(p, self.corners):
            return 0.0
        return min(_d(p, _seg_point(p, a, b)) for a, b in zip(self.corners, self.corners[1:] + self.corners[:1]))


def footprints(objects: Iterable[dict[str, Any]], boxes: dict[str, list[float]] | None = None) -> list[Footprint]:
    """From the mission bridge's ``footprints`` query: ``{type, pos, box?: [4 corners], fwd?, right?}``.
    Without a box from DCS, the model's box from the terrain's files (``boxes``, type name lower
    case -> [min x, min up, min z, max x, max up, max z]; campaign/model_boxes.py), turned by the
    object's axes; else a radius by its type name."""
    out = []
    for o in objects:
        if not isinstance(o.get("pos"), dict):
            continue
        pos = Pos(float(o["pos"]["x"]), float(o["pos"]["y"]))
        t = str(o.get("type") or "")
        box = o.get("box")
        mb = (boxes or {}).get(t.lower())
        fwd, right = o.get("fwd"), o.get("right")
        if isinstance(box, list) and len(box) >= 3 and all(isinstance(c, dict) for c in box):
            out.append(Footprint(pos, [Pos(float(c["x"]), float(c["y"])) for c in box], type=t))
        elif mb and isinstance(fwd, dict) and isinstance(right, dict):
            fx, fy, rx, ry = float(fwd["x"]), float(fwd["y"]), float(right["x"]), float(right["y"])
            corners = [Pos(pos.x + fx * a + rx * c, pos.y + fy * a + ry * c)
                       for a, c in ((mb[0], mb[2]), (mb[3], mb[2]), (mb[3], mb[5]), (mb[0], mb[5]))]
            out.append(Footprint(pos, corners, type=t))
        elif mb:   # its size but not which way it faces: a circle round the whole box
            r = max(math.hypot(a, c) for a in (mb[0], mb[3]) for c in (mb[2], mb[5]))
            out.append(Footprint(pos, radius=r, type=t))
        else:
            lt = t.lower()
            r = next((m for w, m in NO_BOX_M.items() if w in lt), NO_BOX_DEFAULT_M)
            out.append(Footprint(pos, radius=r, type=t))
    return out


def clearance(p: Pos, obstacles: list[Footprint]) -> float:
    return min((o.distance(p) for o in obstacles), default=math.inf)


def _line_clear(a: Pos, b: Pos, obstacles: list[Footprint], m: float) -> bool:
    n = max(1, int(_d(a, b) / 2.0))
    return all(clearance(Pos(a.x + (b.x - a.x) * i / n, a.y + (b.y - a.y) * i / n), obstacles) >= m
               for i in range(n + 1))


def clear_spot(center: Pos, net: TaxiNet | None, obstacles: list[Footprint],
               toward: Pos | None = None) -> tuple[Pos, float] | None:
    """A spot beside the building at ``center``: out from it in some direction to the first point
    CLEAR_M from everything, with a clear straight line onto the taxiways (or towards ``toward``
    without a network). The nearest such spot to the building wins, ties going to the side
    facing the taxiways. Returns (spot, metres from the building's centre), or None."""
    best: tuple[float, Pos, float] | None = None
    aim = taxi_entry(net, center) or toward
    base = math.atan2(aim.y - center.y, aim.x - center.x) if aim is not None else 0.0
    for k in range(SEARCH_DIRS):
        a = base + 2 * math.pi * k / SEARCH_DIRS
        u = (math.cos(a), math.sin(a))
        d = SEARCH_STEP_M
        while d <= SEARCH_M:
            p = _along(center, u, d)
            if clearance(p, obstacles) >= CLEAR_M:
                break
            d += SEARCH_STEP_M
        else:
            continue
        exit_ = taxi_entry(net, p) or toward
        if exit_ is not None and not _line_clear(p, exit_, obstacles, PATH_CLEAR_M):
            continue
        turn = abs((a - base + math.pi) % (2 * math.pi) - math.pi)
        score = d + 10.0 * turn / math.pi + 0.05 * (_d(p, exit_) if exit_ is not None else 0.0)
        if best is None or score < best[0]:
            best = (score, p, d)
    return (best[1], best[2]) if best else None


def _inside(p: Pos, poly: list[Pos]) -> bool:
    inside = False
    for a, b in zip(poly, poly[1:] + poly[:1]):
        if (a.y > p.y) != (b.y > p.y):
            x = a.x + (p.y - a.y) * (b.x - a.x) / (b.y - a.y)
            if p.x < x:
                inside = not inside
    return inside


def _seg_point(p: Pos, a: Pos, b: Pos) -> Pos:
    dx, dy = b.x - a.x, b.y - a.y
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return a
    t = max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / l2))
    return Pos(a.x + dx * t, a.y + dy * t)


# Administrative buildings, best first, with the metres a ride's start may lie farther from the jets
# to be at one rather than the next (Run 32: identified by eye at Prince Hassan and Khalkhalah;
# DCS's shared airfield set, used across theatres).
ADMIN_MODELS = {
    "airfields_building_21": 0.0,      # two-storey headquarters, flags and trucks outside
    "airfields_building_13": 100.0,    # briefing-type building
    "airfields_building_2": 100.0,     # briefing-type building
    "airfields_building_7": 150.0,     # office
    "airfields_building_19_2": 200.0,  # administrative block (several together)
    "airfields_building_6": 250.0,     # operations building with a radar mast
    "airfields_building_27": 500.0,    # the tower's annex: only if nothing else
}
ADMIN_GROUNDS_M = 600.0  # an admin building this close to the taxiways is on the airfield's grounds


def pick_admin(objects: Iterable[dict[str, Any]], net: TaxiNet | None, jets: list[Pos]) -> dict[str, Any] | None:
    """The administrative building (ADMIN_MODELS) for a ride to these jets: the nearest to them,
    the better models counting as nearer by their allowance."""
    c = Pos(sum(j.x for j in jets) / len(jets), sum(j.y for j in jets) / len(jets)) if jets else None
    best: tuple[float, dict[str, Any]] | None = None
    for o in objects:
        t = str(o.get("type") or "").lower()
        if t not in ADMIN_MODELS or not isinstance(o.get("pos"), dict):
            continue
        p = Pos(float(o["pos"]["x"]), float(o["pos"]["y"]))
        entry = taxi_entry(net, p)
        if entry is not None and _d(entry, p) > ADMIN_GROUNDS_M:
            continue
        score = (_d(p, c) if c is not None else 0.0) + ADMIN_MODELS[t]
        if best is None or score < best[0]:
            best = (score, o)
    return best[1] if best else None


def pick_building(objects: Iterable[dict[str, Any]], net: TaxiNet | None, jets: list[Pos],
                  max_from_taxiway_m: float = 400.0) -> dict[str, Any] | None:
    """The building (a tower counts) nearest the jets, near the taxiways (scenery from the mission
    bridge's ``scenery`` query: ``{type, pos}``). Only a guess: bases' real starts are mapped by
    hand (campaign/ride_starts.py; Run 31: the tower is rarely the right place)."""
    def near_jets(o: dict[str, Any]) -> float:
        p = Pos(o["pos"]["x"], o["pos"]["y"])
        return min((_d(p, j) for j in jets), default=0.0)

    usable = []
    for o in objects:
        if not isinstance(o.get("pos"), dict):
            continue
        p = Pos(o["pos"]["x"], o["pos"]["y"])
        entry = taxi_entry(net, p)
        if entry is not None and _d(entry, p) > max_from_taxiway_m:
            continue
        usable.append(o)
    buildings = [o for o in usable if any(w in str(o.get("type", "")).lower() for w in TOWER_WORDS + BUILDING_WORDS)]
    return min(buildings, key=near_jets) if buildings else None


def briefing_point(objects: Iterable[dict[str, Any]], net: TaxiNet | None, jets: list[Pos],
                   max_from_taxiway_m: float = 400.0) -> tuple[Pos, str] | None:
    """Where the ride starts without footprints: BESIDE_M from the building ``pick_building``
    chooses, towards the taxiways. Returns (point, what it's beside), or None."""
    pick = pick_building(objects, net, jets, max_from_taxiway_m)
    if pick is None:
        return None
    p = Pos(pick["pos"]["x"], pick["pos"]["y"])
    entry = taxi_entry(net, p) or (jets[0] if jets else p)
    return _toward(p, entry, BESIDE_M), str(pick.get("type"))
