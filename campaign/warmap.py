"""The players' war map: the campaign drawn in DCS's own map widget (proof P21, made real).

The opt-in client script (lua/client/campaign_client.lua) opens a native window with the map the
Mission Editor uses and draws what the server sends (``campaignMap``; see the spec in the client
script). This module builds that spec from the campaign state, as one side knows it ("plausible
intel": what a real side would know, not everything the engine knows):

- who holds which objective: known to everyone, shaded as territory (each point of the map belongs
  to the side holding the nearest objective), with the front line where the two meet;
- the side's own formations, airbases, squadrons, battles and flights: all of it;
- both sides' depots, factories and power (with their damage);
- air defence, in three layers (each site is sent once, tagged with its layer):
  - always: long range SAMs (``LORAD``), both sides';
  - zoomed in to ``DETAIL_SCALE_M`` or closer: every site the side knows of: all its own, the
    enemy's SAMs reaching ``LONG_SAM_M`` (they radiate), and the enemy's short range air defence
    within ``CONTACT_M`` of the side's ground or in a battle;
  - while one of the side's flights is selected (clicked): every enemy site that reaches its route
    or target, whatever its size (the mission's threat brief);
  the enemy's with a rough strength;
- the enemy's airbases (and the aircraft types reported there), and its formations only within
  ``CONTACT_M`` of the side's ground (in contact), or when in a battle, with a rough strength;
- the enemy's flights: only while airborne within radar cover (``RADAR_M`` of an airbase the
  side holds, or ``AWACS_M`` of the side's airborne AWACS), with their kind but not their type.

``WarMap`` keeps track of the players who have the client script, sends them the map when they
load in (hidden: Ctrl+Shift+F8 shows it) and on ``-map`` in chat, and refreshes it while the war
moves. Players without the script keep the F10 menu and messages.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from .bubble import strength
from .catalog import THREAT_RANGE_M, flight_cruise_ms, threat_range
from .combat import SAM_LAUNCHERS, sam_class
from .model import (CampaignState, Flight, FlightState, GroundOrder, GroundRole, GroundUnit, ObjectiveType, Pos,
                    Side, SpawnState, Theater, Waypoint)
from .seats import FLY_OPEN_S, free_seats, seat_name, seat_of

log = logging.getLogger("campaign")

NM = 1852.0
FT = 0.3048
CELL_M = 2.5 * NM          # territory grid
MARGIN_M = 8 * NM          # the territory reaches this far beyond the outermost objectives
SIMPLIFY_M = 0.3 * NM      # front line points closer than this to a straight line are dropped
CONTACT_M = 8 * NM         # enemy formations this close to our ground are seen
RADAR_M = 80 * NM          # enemy flights this close to an airbase we hold are seen
AWACS_M = 150 * NM         # ...or this close to our airborne AWACS
LORAD = {"SA-2", "SA-5", "SA-10", "SA-20", "Patriot"}   # long range SAMs: always drawn
LONG_SAM_M = 15_000        # the enemy's SAM sites reaching this far radiate: known (drawn zoomed in)
DETAIL_SCALE_M = 40 * NM   # zoomed in to this (metres across) or closer: every known site
SEAD_FIND_M = 5_000        # a SEAD flight's target: the enemy SAM site this close to its SEAD waypoint
ROUTE_MARGIN_M = 3 * NM    # a selected flight shows the enemy's air defence within its reach of the route, plus this
PANEL_AHEAD_S = 3 * 3600   # the panel lists our players' flights taking off within this...
PANEL_FLIGHTS = 6          # ...the first few, as buttons that select them
TIMED_ACTIONS = ("ORBIT", "CAS", "SEAD", "BOMB")   # waypoint actions that hold the flight there a while
WP_WORD = {"CAS": "TGT", "BOMB": "TGT", "SEAD": "TGT", "ORBIT": "STN", "LAND": "LAND"}
NEWS_CHARS = 60
NEWS_KEPT = 8

AIRBORNE = {FlightState.TAXI, FlightState.ENROUTE, FlightState.ON_STATION, FlightState.EGRESS}
DONE = {FlightState.LANDED, FlightState.LOST}

BLUE = [0.35, 0.6, 1.0, 1.0]
RED = [1.0, 0.3, 0.25, 1.0]
GREY = [0.75, 0.75, 0.75, 1.0]
YELLOW = [1.0, 0.85, 0.2, 1.0]
WHITE = [1.0, 1.0, 1.0, 1.0]
CLEAR = [0, 0, 0, 0]
SIDE_COLOR = {Side.BLUE: BLUE, Side.RED: RED, Side.NEUTRAL: GREY}
TERRITORY_FILL = {Side.BLUE: [0.3, 0.55, 1.0, 0.20], Side.RED: [1.0, 0.3, 0.25, 0.20]}

ROLE_SYM = {GroundRole.ARMOR: "tank", GroundRole.MECH: "ifv", GroundRole.INFANTRY: "apc",
            GroundRole.ARTILLERY: "artillery", GroundRole.SUPPLY: "truck", GroundRole.HQ: "hq",
            GroundRole.AAA: "aaa"}
ROLE_WORD = {GroundRole.ARMOR: "armour", GroundRole.MECH: "mechanised infantry", GroundRole.INFANTRY: "infantry",
             GroundRole.ARTILLERY: "artillery", GroundRole.SUPPLY: "supply column", GroundRole.HQ: "headquarters",
             GroundRole.AAA: "air defence guns", GroundRole.SAM: "SAM site"}
ORDER_WORD = {GroundOrder.HOLD: "Holding", GroundOrder.DEFEND: "Defending", GroundOrder.ATTACK: "Attacking",
              GroundOrder.MOVE: "Moving to", GroundOrder.RESERVE: "In reserve", GroundOrder.RETREAT: "Retreating to"}
# Reporting names of air defence types that combat.sam_class doesn't class (short range, ours).
AD_NAME = {"Tor 9A331": "SA-15", "Osa 9A33 ln": "SA-8", "2S6 Tunguska": "SA-19", "Strela-10M3": "SA-13",
           "Strela-1 9P31": "SA-9", "SA-18 Igla manpad": "SA-18", "Roland ADS": "Roland", "M1097 Avenger": "Avenger",
           "M48 Chaparral": "Chaparral", "Patriot ln": "Patriot", "NASAMS_LN_C": "NASAMS", "NASAMS_LN_B": "NASAMS",
           "ZSU-23-4 Shilka": "ZSU-23-4", "ZSU_57_2": "ZSU-57-2", "ZU-23 Emplacement": "ZU-23", "Gepard": "Gepard",
           "Vulcan": "Vulcan"}
FIGHTER_MISSIONS = {"CAP", "BARCAP", "TARCAP", "ESCORT", "SWEEP", "INTERCEPT"}
HELO_TYPES = {"AH-64D_BLK_II", "Ka-50", "Ka-50_3", "Mi-24P", "Mi-8MT", "UH-1H", "SA342M", "CH-47Fbl1", "UH-60A",
              "OH58D", "AH-1W", "Mi-28N"}
INFRA_SYM = {ObjectiveType.DEPOT: "depot", ObjectiveType.FUEL_DEPOT: "depot", ObjectiveType.FACTORY: "building",
             ObjectiveType.REFINERY: "building", ObjectiveType.POWER_PLANT: "building",
             ObjectiveType.SUBSTATION: "building", ObjectiveType.RADAR: "radar", ObjectiveType.PORT: "building",
             ObjectiveType.DAM: "building"}
TYPE_WORD = {t: t.name.lower().replace("_", " ") for t in ObjectiveType}


def enemy_of(side: Side) -> Side:
    return Side.RED if side is Side.BLUE else Side.BLUE


def xy(p: Pos) -> dict[str, float]:
    return {"x": round(p.x), "y": round(p.y)}


def about(n: int) -> str:
    """A rough count, as intel would give it."""
    if n <= 3:
        return "a few"
    if n < 10:
        return "under 10"
    return f"about {5 * round(n / 5)}"


def few(n: int) -> str:
    """A rough count of something small and countable, like a SAM site's launchers."""
    if n <= 0:
        return "none"
    if n <= 6:
        lo = n - (n + 1) % 2   # 1-2, 3-4, 5-6
        return f"{lo} or {lo + 1}"
    return about(n)


def t_minus(dt: float) -> str:
    """Time to take-off, as BMS shows it: T-0:19:32 (T+ once it's past)."""
    s = int(round(dt))
    sign, s = ("-", s) if s >= 0 else ("+", -s)
    return f"T{sign}{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


KT = 0.514444


def weather_text(w: dict[str, Any] | None) -> str:
    """The mission's weather in a line: clouds, visibility, wind, temperature (the mission bridge's
    ``query mission`` summary). Wind is given without its direction."""
    if not w:
        return ""
    parts = []
    preset = str(w.get("preset") or "")
    base = w.get("base")
    at = f" at {round(float(base) / FT / 500) * 500:,.0f} ft" if isinstance(base, (int, float)) and base > 0 else ""
    if preset.startswith("RainyPreset"):
        parts.append(f"overcast and rain{at}")
    elif preset.startswith("Preset"):
        try:
            n = int(preset.removeprefix("Preset"))
        except ValueError:
            n = 0
        kind = "few clouds" if n <= 2 else "scattered clouds" if n <= 12 else "broken clouds" if n <= 19 \
            else "overcast"
        parts.append(kind + at)
    else:
        parts.append("clear")
    if w.get("fog"):
        parts.append("fog")
    vis = w.get("visibility")
    if isinstance(vis, (int, float)) and vis > 0:
        nm = vis / NM
        parts.append("visibility 10+ nm" if nm >= 10 else f"visibility {nm:.0f} nm")
    wind = w.get("wind_ground")
    if isinstance(wind, dict) and isinstance(wind.get("speed"), (int, float)):
        kt = wind["speed"] / KT
        parts.append("calm" if kt < 2 else f"wind {kt:.0f} kt")
    temp = w.get("temperature")
    if isinstance(temp, (int, float)):
        parts.append(f"{temp:.0f}°C")
    return ", ".join(parts)


def hhmm(t: float) -> str:
    s = int(t)
    return f"{s // 3600 % 24:02d}:{s // 60 % 60:02d}"


# ---------- territory and the front line ----------

class _Nearest:
    """Distance to the nearest of a set of points, searched in buckets (capped at ``cap``)."""

    def __init__(self, pts: Iterable[Pos], cap: float) -> None:
        self.cap = cap
        self.b: dict[tuple[int, int], list[Pos]] = {}
        for p in pts:
            self.b.setdefault((int(p.x // cap), int(p.y // cap)), []).append(p)

    def __call__(self, x: float, y: float) -> float:
        i, j = int(x // self.cap), int(y // self.cap)
        best = self.cap
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for p in self.b.get((i + di, j + dj), ()):
                    d = math.hypot(p.x - x, p.y - y)
                    if d < best:
                        best = d
        return best


@dataclass
class Territory:
    blue: list[list[dict[str, float]]] = field(default_factory=list)   # polygons
    red: list[list[dict[str, float]]] = field(default_factory=list)
    front: list[list[dict[str, float]]] = field(default_factory=list)  # polylines


def _clip(poly: list[tuple[float, float, float]], sign: float) -> list[tuple[float, float]]:
    """The part of a cell polygon (x, y, f at each corner) where sign * f > 0, f linear along edges."""
    out: list[tuple[float, float]] = []
    n = len(poly)
    for k in range(n):
        x0, y0, f0 = poly[k]
        x1, y1, f1 = poly[(k + 1) % n]
        a, b = sign * f0, sign * f1
        if a > 0:
            out.append((x0, y0))
        if (a > 0) != (b > 0):
            t = a / (a - b)
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    return out


def _simplify(pts: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Douglas-Peucker."""
    if len(pts) < 3:
        return pts
    (ax, ay), (bx, by) = pts[0], pts[-1]
    dx, dy = bx - ax, by - ay
    n = math.hypot(dx, dy)
    best, idx = -1.0, 0
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        d = abs(dy * (px - ax) - dx * (py - ay)) / n if n else math.hypot(px - ax, py - ay)
        if d > best:
            best, idx = d, i
    if best <= tol:
        return [pts[0], pts[-1]]
    return _simplify(pts[:idx + 1], tol)[:-1] + _simplify(pts[idx:], tol)


def territory(blue: list[Pos], red: list[Pos], cell: float = CELL_M, margin: float = MARGIN_M) -> Territory:
    """Shade each side's ground (points nearer a side's objective than the other side's) and trace
    the front between them.

    A grid of ``cell``; f = (distance to red) - (distance to blue) at each corner, so blue ground
    is f > 0 and the front is f = 0, found by marching squares (linear along each cell edge).
    Cells wholly one side's are merged into strips; cells the front crosses are cut along it."""
    out = Territory()
    if not blue or not red:
        return out
    pts = blue + red
    x0, x1 = min(p.x for p in pts) - margin, max(p.x for p in pts) + margin
    y0, y1 = min(p.y for p in pts) - margin, max(p.y for p in pts) + margin
    ni, nj = max(1, math.ceil((x1 - x0) / cell)), max(1, math.ceil((y1 - y0) / cell))
    cap = max(x1 - x0, y1 - y0)   # beyond the bucket search, both distances cap here: f = 0 far from both
    near_b, near_r = _Nearest(blue, cell * 12), _Nearest(red, cell * 12)
    big = cell * 12

    def f_at(x: float, y: float) -> float:
        db, dr = near_b(x, y), near_r(x, y)
        if db >= big and dr >= big:   # far from both: fall back to a full search
            db = min(math.hypot(p.x - x, p.y - y) for p in blue)
            dr = min(math.hypot(p.x - x, p.y - y) for p in red)
        elif db >= big:
            db = min(cap, min(math.hypot(p.x - x, p.y - y) for p in blue))
        elif dr >= big:
            dr = min(cap, min(math.hypot(p.x - x, p.y - y) for p in red))
        v = dr - db
        return v if v != 0 else 1e-6

    gx = [x0 + i * cell for i in range(ni + 1)]
    gy = [y0 + j * cell for j in range(nj + 1)]
    f = [[f_at(x, y) for y in gy] for x in gx]

    # The front: marching squares on f, the crossings keyed by the cell edge they sit on.
    def cross(a: tuple[int, int], b: tuple[int, int]) -> tuple[float, float]:
        fa, fb = f[a[0]][a[1]], f[b[0]][b[1]]
        t = fa / (fa - fb)
        return (gx[a[0]] + (gx[b[0]] - gx[a[0]]) * t, gy[a[1]] + (gy[b[1]] - gy[a[1]]) * t)

    point: dict[tuple, tuple[float, float]] = {}
    links: dict[tuple, list[tuple]] = {}
    for i in range(ni):
        for j in range(nj):
            ring = [(i, j), (i, j + 1), (i + 1, j + 1), (i + 1, j)]
            edges = []
            for k in range(4):
                a, b = ring[k], ring[(k + 1) % 4]
                if (f[a[0]][a[1]] > 0) != (f[b[0]][b[1]] > 0):
                    key = (min(a, b), max(a, b))
                    point.setdefault(key, cross(a, b))
                    edges.append(key)
            if len(edges) == 2:
                pairs = [(edges[0], edges[1])]
            elif len(edges) == 4:   # a saddle: split by the cell centre's side
                centre = sum(f[a][b] for a, b in ring) > 0
                first_in = (f[i][j] > 0) == centre
                pairs = [(edges[0], edges[1]), (edges[2], edges[3])] if not first_in else \
                    [(edges[3], edges[0]), (edges[1], edges[2])]
            else:
                pairs = []
            for a, b in pairs:
                links.setdefault(a, []).append(b)
                links.setdefault(b, []).append(a)
    # Chains: open ones end on the grid's edge (an edge crossing shared by one cell only); the
    # rest are closed rings (enclaves).
    seen: set[tuple] = set()
    chains: list[list[tuple[float, float]]] = []
    rings: list[tuple[list[tuple[float, float]], tuple]] = []
    starts = [k for k, v in links.items() if len(v) == 1] + [k for k, v in links.items() if len(v) != 1]
    for start in starts:
        if start in seen:
            continue
        chain, cur, prev = [start], start, None
        seen.add(start)
        while True:
            nxt = [n for n in links[cur] if n != prev and n not in seen]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            seen.add(cur)
            chain.append(cur)
        line = [point[k] for k in chain]
        if len(links[start]) == 1:
            chains.append(_simplify(line, SIMPLIFY_M))
        elif len(line) >= 3:
            rings.append((_simplify(line + [line[0]], SIMPLIFY_M)[:-1], start))
    for line in chains + [r + r[:1] for r, _ in rings]:
        out.front.append([{"x": round(x), "y": round(y)} for x, y in line])

    # Fill: each side's ground as whole polygons, bounded by the front chains and the grid's edge.
    # Walk the edge (x0,y0) -> (x0,Y1) -> (X1,Y1) -> (X1,y0); between two chain ends the edge is
    # one side's; at a chain end, follow the chain to its other end and carry on along the edge.
    X1, Y1 = gx[-1], gy[-1]
    W, H = Y1 - y0, X1 - x0
    eps = cell * 1e-6

    def t_of(x: float, y: float) -> float:
        if abs(x - x0) < eps:
            return y - y0
        if abs(y - Y1) < eps:
            return W + (x - x0)
        if abs(x - X1) < eps:
            return W + H + (Y1 - y)
        return 2 * W + H + (X1 - x)

    edge_corners = [(0.0, (x0, y0), f[0][0]), (W, (x0, Y1), f[0][nj]), (W + H, (X1, Y1), f[ni][nj]),
                    (2 * W + H, (X1, y0), f[ni][0])]
    edge_pts = sorted(  # every grid corner on the edge, with its t and f: an arc's side
        [(t_of(gx[0], gy[j]), (gx[0], gy[j]), f[0][j]) for j in range(nj + 1)]
        + [(t_of(gx[i], gy[nj]), (gx[i], gy[nj]), f[i][nj]) for i in range(1, ni + 1)]
        + [(t_of(gx[ni], gy[j]), (gx[ni], gy[j]), f[ni][j]) for j in range(nj)]
        + [(t_of(gx[i], gy[0]), (gx[i], gy[0]), f[i][0]) for i in range(1, ni)])
    ends = sorted(((t_of(*c[e]), n, e) for n, c in enumerate(chains) for e in (0, -1)))
    dest = {Side.BLUE: out.blue, Side.RED: out.red}

    def between(ta: float, tb: float, pts=edge_pts) -> list[tuple[float, tuple[float, float], float]]:
        """Points on the edge after ta up to before tb (going round, wrapping past t = 0)."""
        if tb > ta:
            return [c for c in pts if ta < c[0] < tb]
        return [c for c in pts if c[0] > ta] + [c for c in pts if c[0] < tb]

    if not ends:
        s = Side.BLUE if f[0][0] > 0 else Side.RED
        dest[s].append([{"x": round(x), "y": round(y)} for _, (x, y), _ in edge_corners])
    done: set[int] = set()
    index = {(n, e): k for k, (_, n, e) in enumerate(ends)}
    for k0 in range(len(ends)):
        if k0 in done:
            continue
        arc = between(ends[k0][0], ends[(k0 + 1) % len(ends)][0])
        s = Side.BLUE if arc and arc[0][2] > 0 else Side.RED
        poly: list[tuple[float, float]] = []
        k = k0
        while k not in done:
            done.add(k)
            nxt = (k + 1) % len(ends)
            t_a, t_b = ends[k][0], ends[nxt][0]
            poly += [pt for _, pt, _ in between(t_a, t_b, edge_corners)]   # the box's corners
            _, n, e = ends[nxt]
            c = chains[n] if e == 0 else chains[n][::-1]
            poly += c
            k = index[(n, -1 if e == 0 else 0)]
        if len(poly) >= 3:
            dest[s].append([{"x": round(x), "y": round(y)} for x, y in poly])
    for ring, key in rings:   # an enclave: the side of the grid corner inside it
        a, b = key
        inside = _inside(gx[a[0]], gy[a[1]], ring)
        fa = f[a[0]][a[1]] if inside else f[b[0]][b[1]]
        dest[Side.BLUE if fa > 0 else Side.RED].append([{"x": round(x), "y": round(y)} for x, y in ring])
    return out


def _dist_to_route(p: Pos, route: list[Pos]) -> float:
    """Distance from ``p`` to the polyline ``route`` (or to its one point)."""
    if len(route) == 1:
        return math.hypot(p.x - route[0].x, p.y - route[0].y)
    best = math.inf
    for a, b in zip(route, route[1:]):
        dx, dy = b.x - a.x, b.y - a.y
        len2 = dx * dx + dy * dy
        t = max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2)) if len2 else 0.0
        best = min(best, math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy)))
    return best


def _bounds(pts: list[Pos]) -> dict[str, Any]:
    """{"fit": [x0, y0, x1, y1]}: the box the client brings into view (empty without points)."""
    if not pts:
        return {}
    return {"fit": [round(min(p.x for p in pts)), round(min(p.y for p in pts)),
                    round(max(p.x for p in pts)), round(max(p.y for p in pts))]}


def _hmm(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 3600}:{s // 60 % 60:02d}"


def _inside(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    """Point in polygon (even-odd)."""
    n, c = len(ring), False
    for k in range(n):
        (xa, ya), (xb, yb) = ring[k], ring[(k + 1) % n]
        if (ya > y) != (yb > y) and x < xa + (xb - xa) * (y - ya) / (yb - ya):
            c = not c
    return c


# ---------- what a side knows ----------

def sam_name(u: GroundUnit) -> str:
    cls = sam_class(u.composition)
    if cls:
        return cls
    for t, n in sorted(u.composition.items(), key=lambda kv: -THREAT_RANGE_M.get(kv[0], 0.0)):
        if n > 0 and t in SAM_LAUNCHERS:
            return SAM_LAUNCHERS[t]        # launchers left, its radar gone
        if n > 0 and t in AD_NAME:
            return AD_NAME[t]
    return "SAM" if u.role is GroundRole.SAM else "AAA"


def flight_sym(f: Flight, aircraft_type: str) -> str:
    if aircraft_type in HELO_TYPES:
        return "helo"
    if f.mission in ("AWACS",):
        return "awacs"
    if f.mission in ("TANKER",):
        return "tanker"
    if f.mission in FIGHTER_MISSIONS:
        return "fighter"
    return "attack"


def flight_kind(f: Flight, aircraft_type: str) -> str:
    return {"helo": "helicopters", "awacs": "AWACS", "tanker": "tanker", "fighter": "fighters",
            "attack": "attack aircraft"}[flight_sym(f, aircraft_type)]


class Picture:
    """The war as ``side`` knows it, as a ``campaignMap`` spec."""

    def __init__(self, state: CampaignState, theater: Theater, side: Side = Side.BLUE, *, magvar: float = 0.0,
                 title: str = "Campaign", news: Iterable[str] = (), side_names: dict[Side, str] | None = None,
                 terrain: Territory | None = None, tod_offset: float = 0.0, weather: str = "",
                 me: str | None = None, pilots: dict[str, str] | None = None) -> None:
        self.st, self.th, self.side, self.magvar = state, theater, side, magvar
        self.enemy = enemy_of(side)
        self.title = title
        self.news = list(news)
        self.names = side_names or {}
        self.terrain = terrain
        self.off = tod_offset      # campaign clock + this = DCS time of day (seconds since the mission date's midnight)
        self.weather = weather
        self.me = me                  # the viewer's ucid, if known: their seat in a flight
        self.pilots = pilots or {}    # ucid -> name, for the seats
        self.objects: list[dict[str, Any]] = []
        self.enemy_ad: list[tuple[GroundUnit, str, float, dict[str, Any]]] = []
        self.flight_buttons: list[dict[str, Any]] = []
        self.packages: dict[str, str] = {}      # selectable flight's group -> its package
        self.joins: dict[str, dict[str, Any]] = {}   # selectable flight's group -> its join button
        self.actions: dict[str, dict[str, Any]] = {}  # ... -> its commit or fly button (seated players)

    # ----- helpers -----

    def name(self, side: Side) -> str:
        return self.names.get(side) or side.name.lower()

    def opos(self, oid: str) -> Pos:
        return self.th.objectives[oid].pos

    def held(self, side: Side) -> list[str]:
        return [oid for oid, o in self.st.objectives.items() if o.owner is side and oid in self.th.objectives]

    def mag(self, hdg_true: float) -> str:
        return f"{(hdg_true - self.magvar) % 360:03.0f}M"

    def target_name(self, target_id: str) -> str:
        if target_id in self.th.objectives:
            return self.th.objectives[target_id].name
        u = self.st.ground_units.get(target_id)
        if u is not None:
            return u.name if u.side is self.side else f"enemy {ROLE_WORD.get(u.role, 'forces')}"
        return ""   # e.g. a CAP's "front"

    def own_target(self, f: Flight, pkg) -> tuple[Pos, str] | None:
        """Where the flight attacks and what (Run 27: the package's target isn't each flight's): a SEAD
        flight's SAM site, a moving target where it is now, else its attack waypoint. None for a flight
        without one (a CAP on its station)."""
        wp = next((w for w in f.waypoints if w.action in ("CAS", "BOMB", "SEAD")
                   or (f.mission == "ESCORT" and w.params.get("target"))), None)
        if wp is None:
            return None
        main = self.target_name(pkg.target_id) if pkg else ""
        if f.mission == "SEAD":
            sites = [u for u in self.st.ground_units.values() if u.side is self.enemy and u.role is GroundRole.SAM
                     and strength(u) > 0 and math.hypot(u.pos.x - wp.pos.x, u.pos.y - wp.pos.y) <= SEAD_FIND_M]
            if sites:
                u = min(sites, key=lambda u: math.hypot(u.pos.x - wp.pos.x, u.pos.y - wp.pos.y))
                return u.pos, f"{sam_name(u)} site" + (f" near {self.near(u.pos)}" if self.near(u.pos) else "")
            return wp.pos, "air defence" + (f" near {self.near(wp.pos)}" if self.near(wp.pos) else "")
        if pkg and pkg.target_id in self.st.ground_units and f.mission != "ESCORT":
            return self.st.ground_units[pkg.target_id].pos, main    # where it is now
        if f.mission == "ESCORT":
            return wp.pos, f"escort to {main}" if main else "escort"
        return wp.pos, main

    def near(self, p: Pos) -> str:
        """The nearest objective's name, within 15 km."""
        best = min(self.th.objectives.values(), key=lambda o: math.hypot(o.pos.x - p.x, o.pos.y - p.y), default=None)
        if best is None or math.hypot(best.pos.x - p.x, best.pos.y - p.y) > 15_000:
            return ""
        return best.name

    def target_pos(self, target_id: str) -> Pos | None:
        if target_id in self.th.objectives:
            return self.th.objectives[target_id].pos
        u = self.st.ground_units.get(target_id)
        return u.pos if u is not None else None

    def order_text(self, u: GroundUnit) -> str:
        """E.g. "Attacking Jabab", "Moving to As Sanamayn", "In reserve"."""
        order = ORDER_WORD.get(u.order, u.order.name.capitalize())
        where = self.target_name(u.order_target) if u.order_target else ""
        if not where:
            return order.removesuffix(" to")
        return f"{order} {where}"

    def in_contact(self, p: Pos, ours: list[Pos]) -> bool:
        return any(math.hypot(p.x - q.x, p.y - q.y) <= CONTACT_M for q in ours)

    # ----- the picture -----

    def build(self, center: Pos | None = None, scale: float | None = None) -> dict[str, Any]:
        st = self.st
        ours_ground = [self.opos(o) for o in self.held(self.side)] + \
            [u.pos for u in st.ground_units.values() if u.side is self.side and strength(u) > 0]
        terr = self.terrain or territory([self.opos(o) for o in self.held(Side.BLUE)],
                                         [self.opos(o) for o in self.held(Side.RED)])
        for s, polys in ((Side.BLUE, terr.blue), (Side.RED, terr.red)):
            for poly in polys:
                self.objects.append({"kind": "area", "points": poly, "color": CLEAR, "fill": TERRITORY_FILL[s]})
        front_info = (f"The front\n{self.name(Side.BLUE)} holds {len(self.held(Side.BLUE))} objectives, "
                      f"{self.name(Side.RED)} {len(self.held(Side.RED))}")
        for line in terr.front:
            self.objects.append({"kind": "line", "points": line, "color": [0.95, 0.95, 0.95, 1], "style": "boundary",
                                 "info": front_info})
        self._airbases()
        self._infrastructure()
        self._air_defence(ours_ground)
        seen = self._formations(ours_ground)
        self._battles()
        flights = self._flights()
        # A site only shown on a route that's on nobody's route isn't sent.
        self.objects = [o for o in self.objects if o.get("groups") != [] or o.get("detail")]
        spec = {"title": self.title, "mode": "map", "panel": self._panel(seen, flights),
                "objects": self.objects, "detail_scale": round(DETAIL_SCALE_M), "flights": self.flight_buttons,
                "packages": self.packages, "joins": self.joins, "actions": self.actions,
                "flights_none": f"none in the next {PANEL_AHEAD_S // 3600} h"}
        c = center or self._centre()
        if c is not None:
            spec["center"] = xy(c)
        spec["scale"] = round(scale or 140 * NM)
        return spec

    def _centre(self) -> Pos | None:
        """The middle of the battles, else of the front, else of our ground."""
        pts = [self.opos(b.at) for b in self.st.battles.values() if b.at in self.th.objectives]
        if not pts:
            front = [p for line in (self.terrain.front if self.terrain else []) for p in line]
            pts = [Pos(p["x"], p["y"]) for p in front]
        if not pts:
            pts = [self.opos(o) for o in self.held(self.side)]
        if not pts:
            return None
        return Pos(sum(p.x for p in pts) / len(pts), sum(p.y for p in pts) / len(pts))

    def _airbases(self) -> None:
        sqs: dict[str, list] = {}
        for sq in self.st.squadrons.values():
            sqs.setdefault(sq.base_id, []).append(sq)
        for oid, os_ in self.st.objectives.items():
            o = self.th.objectives.get(oid)
            if o is None or o.type not in (ObjectiveType.AIRBASE, ObjectiveType.FARP):
                continue
            owner = os_.owner
            lines = [f"{o.name} ({self.name(owner)})"]
            here = sqs.get(oid, [])
            if owner is self.side:
                lines += [f"{sq.name}: {sq.aircraft_ready} {sq.aircraft_type} ready" +
                          (f", {sq.aircraft_damaged} damaged" if sq.aircraft_damaged else "") for sq in here]
            elif here:
                types = sorted({sq.aircraft_type for sq in here if sq.side is owner})
                if types:
                    lines.append("Reported here: " + ", ".join(types))
            func = os_.function(o)
            if func < 0.99:
                lines.append(f"Damaged: working at {func:.0%}")
            self.objects.append({"kind": "icon", "sym": "airbase" if o.type is ObjectiveType.AIRBASE else "farp",
                                 "color": SIDE_COLOR[owner], **xy(o.pos), "label": o.name, "info": "\n".join(lines)})

    def _infrastructure(self) -> None:
        """Depots, factories, power, fuel: ours and the enemy's (with their damage)."""
        for oid, os_ in self.st.objectives.items():
            o = self.th.objectives.get(oid)
            if o is None or os_.owner not in (self.side, self.enemy) or o.type not in INFRA_SYM:
                continue
            func = os_.function(o)
            info = f"{o.name}: {TYPE_WORD[o.type]} ({self.name(os_.owner)})"
            if func < 0.99:
                info += f"\nDamaged: working at {func:.0%}"
            self.objects.append({"kind": "icon", "sym": INFRA_SYM[o.type], "color": SIDE_COLOR[os_.owner],
                                 **xy(o.pos), "info": info})

    def _air_defence(self, ours_ground: list[Pos]) -> None:
        """Air defence sites, in three layers (see the module docstring); keeps the enemy's sites for
        the flights' route threats (``_route_threats``)."""
        for u in self.st.ground_units.values():
            if u.role not in (GroundRole.SAM, GroundRole.AAA) or strength(u) == 0:
                continue
            rng = threat_range(u.composition)
            if rng <= 0:
                continue
            mine = u.side is self.side
            name = sam_name(u)
            lorad = name in LORAD and u.role is GroundRole.SAM
            if lorad:
                layer = {}                                  # always
            elif mine or (u.role is GroundRole.SAM and rng >= LONG_SAM_M) or u.battle \
                    or self.in_contact(u.pos, ours_ground):
                layer = {"detail": True}                    # known: zoomed in
            else:
                layer = {"groups": []}                      # not known: only on a selected flight's route
            col = SIDE_COLOR[u.side]
            guns = u.role is GroundRole.AAA
            word = "guns" if guns else "launchers"
            up = guns or sam_class(u.composition) is not None
            weapons = sum(n for t, n in u.composition.items() if t in THREAT_RANGE_M or t in SAM_LAUNCHERS)
            n, full = strength(u), u.full or strength(u)
            if mine:
                lines = [u.name, f"{name}: {weapons} {word}, {n} of {full} vehicles"]
            else:
                lines = [f"{name} ({self.name(u.side)})", f"{few(weapons)} {word}"]
                if n < 0.75 * full:
                    lines.append("Reported damaged")
            if any(t in SAM_LAUNCHERS for t in u.composition):   # a site with a radar we model
                lines.append("Radar active" if up else "Radar down")
            sym = "aaa" if guns else "sam_long" if rng >= 40_000 else "sam_medium" if rng >= LONG_SAM_M else "sam_short"
            icon = {"kind": "icon", "sym": sym, "color": col, **xy(u.pos), "label": name, "info": "\n".join(lines),
                    **layer}
            # No info on the ring: it would take the clicks meant for what's inside it.
            ring = {"kind": "ring", **xy(u.pos), "r": round(rng), "color": col,
                    "fill": [col[0], col[1], col[2], 0.06], "style": "solid2" if rng >= 40_000 else "dash", **layer}
            if "groups" in layer:
                ring["groups"] = icon["groups"]             # the same list: a flight's group lands on both
            self.objects += [icon, ring]
            if not mine:
                self.enemy_ad.append((u, name, rng, icon))

    def _route_threats(self, gid: str, route: list[Pos]) -> str:
        """Tags the enemy's air defence along ``route`` (and its target) with ``gid``, so selecting the
        flight shows them all. Returns them as a line for the flight's info, e.g. "SA-6 x2, ZSU-23-4"."""
        counts: dict[str, int] = {}
        for u, name, rng, icon in self.enemy_ad:
            if _dist_to_route(u.pos, route) > rng + ROUTE_MARGIN_M:
                continue
            counts[name] = counts.get(name, 0) + 1
            if "groups" in icon:
                icon["groups"].append(gid)
            elif icon.get("detail"):
                icon.setdefault("groups", []).append(gid)
                ring = self.objects[self.objects.index(icon) + 1]
                ring["groups"] = icon["groups"]
        return ", ".join(f"{n} x{k}" if k > 1 else n for n, k in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def _formations(self, ours_ground: list[Pos]) -> int:
        """Ground formations: all of ours, the enemy's in contact. Returns how many enemy ones are seen."""
        seen = 0
        for u in self.st.ground_units.values():
            if u.role in (GroundRole.GARRISON, GroundRole.SAM, GroundRole.AAA) or strength(u) == 0:
                continue
            sym = ROLE_SYM.get(u.role, "ifv")
            heading = 0.0
            if u.route and u.route[0] in self.th.objectives:
                to = self.opos(u.route[0])
                heading = math.degrees(math.atan2(to.y - u.pos.y, to.x - u.pos.x)) % 360
            if u.side is self.side:
                n, full = strength(u), u.full or strength(u)
                lines = [u.name, self.order_text(u),
                         f"{n} of {full} vehicles, morale {u.morale:.0%}, supply {u.supply:.0%}"]
                if u.battle:
                    lines.append(f"In the battle for {self.target_name(u.battle)}")
                # No label: formations stand together and their names would pile up (click for them).
                self.objects.append({"kind": "icon", "sym": sym, "color": SIDE_COLOR[self.side], **xy(u.pos),
                                     "heading": round(heading), "info": "\n".join(lines)})
            elif u.battle or self.in_contact(u.pos, ours_ground):
                seen += 1
                word = ROLE_WORD.get(u.role, "forces")
                lines = [f"Enemy {word}", f"{about(strength(u))} vehicles"]
                if u.battle:
                    lines.append(f"In the battle for {self.target_name(u.battle)}")
                elif u.route:
                    lines.append("On the move")
                self.objects.append({"kind": "icon", "sym": sym, "color": SIDE_COLOR[u.side], **xy(u.pos),
                                     "heading": round(heading), "info": "\n".join(lines)})
        return seen

    def _battles(self) -> None:
        for b in self.st.battles.values():
            if b.at not in self.th.objectives:
                continue
            o = self.th.objectives[b.at]
            ours = b.attacker is self.side
            info = (f"Battle of {o.name}, hour {b.hours}\n{self.name(b.attacker)} attacking"
                    f"\n{'We are attacking' if ours else 'We are defending' if self.st.objectives[b.at].owner is self.side else ''}").rstrip()
            self.objects.append({"kind": "icon", "sym": "target", "color": YELLOW, **xy(o.pos), "info": info})
            self.objects.append({"kind": "text", "x": round(o.pos.x + 2.5 * NM), "y": round(o.pos.y + 2 * NM),
                                 "text": f"Battle of {o.name}", "color": YELLOW, "size": 13, "info": info})

    def _flights(self) -> list[Flight]:
        """Our flights in the air, the enemy's in radar cover, and our flights' plans (drawn while one
        is selected: its route in white with times, its package's other flights in yellow, its target
        and the air defence along the way). Returns our players' coming flights, for the panel."""
        st = self.st
        radars = [self.opos(oid) for oid in self.held(self.side)
                  if self.th.objectives[oid].type is ObjectiveType.AIRBASE]
        awacs = []
        for f in st.flights.values():
            sq = st.squadrons.get(f.squadron_id)
            if sq and sq.side is self.side and f.state in AIRBORNE and f.mission == "AWACS" and f.pos:
                awacs.append(f.pos)
        live = [f for f in sorted(st.flights.values(), key=lambda f: f.takeoff_time)
                if f.squadron_id in st.squadrons and f.state not in DONE and f.aircraft > 0]
        ours = [f for f in live if st.squadrons[f.squadron_id].side is self.side]
        coming = [f for f in ours if f.player_preferred and f.takeoff_time <= st.clock + PANEL_AHEAD_S]
        buttons = coming[:PANEL_FLIGHTS]
        # The flights one can select: the panel's, and ours in the air (their symbols).
        selectable = {f.id for f in buttons} | {f.id for f in ours if f.state in AIRBORNE and f.pos is not None}
        infos: dict[str, str] = {}
        self.packages = {f"f:{f.id}": f.package_id for f in ours if f.id in selectable}
        self.joins = {f"f:{f.id}": j for f in ours if f.id in selectable and (j := self.join_button(f))}
        self.actions = {f"f:{f.id}": a for f in ours if f.id in selectable and (a := self.action_button(f))}
        for f in ours:
            sq = st.squadrons[f.squadron_id]
            pkg = st.packages.get(f.package_id)
            own = self.own_target(f, pkg)
            target, tpos = (own[1], own[0]) if own else (self.target_name(pkg.target_id) if pkg else "", None)
            plan = self._plan(f)
            gid = f"f:{f.id}"
            mates = [m for m in (pkg.flight_ids if pkg else []) if m != f.id and m in selectable]
            if f.id in selectable:
                pts = [p for p, _, _ in plan] + ([f.pos] if f.pos and f.state in AIRBORNE else []) + \
                    ([tpos] if tpos else [])
                threats = self._route_threats(gid, pts)
                infos[f.id] = self._flight_info(f, sq, target) + \
                    f"\nAir defence on the route: {threats or 'none known'}"
                if len(plan) >= 2:   # its route, white, with the time at each waypoint
                    self.objects.append({"kind": "line", "points": [xy(p) for p, _, _ in plan], "color": WHITE,
                                         "style": "solid2", "info": infos[f.id], "select": gid, "groups": [gid]})
                for i, (p, eta, w) in enumerate(plan[1:], 1):
                    word = WP_WORD.get(w.action or "", f"WP{i}")
                    self.objects.append({"kind": "text", **xy(Pos(p.x + 0.8 * NM, p.y + 0.8 * NM)), "color": WHITE,
                                         "size": 12, "groups": [gid],
                                         "text": f"{word} {hhmm(eta + self.off)} +{_hmm(eta - f.takeoff_time)}"})
                if tpos is not None:
                    self.objects.append({"kind": "icon", "sym": "target", "color": WHITE, **xy(tpos),
                                         "label": f"{f.callsign} target", "groups": [gid],
                                         "info": f"{f.callsign}'s target: {target or 'here'}"})
            if mates and len(plan) >= 2:     # supporting the selected flight: yellow
                groups = [f"f:{m}" for m in mates]
                brief = self._flight_info(f, sq, target)
                self.objects.append({"kind": "line", "points": [xy(p) for p, _, _ in plan], "color": YELLOW,
                                     "style": "dash", "info": brief, "groups": groups})
                at = next(((p, eta) for p, eta, w in plan[1:] if w.action in TIMED_ACTIONS), None)
                if at:
                    self.objects.append({"kind": "text", **xy(Pos(at[0].x - 1.5 * NM, at[0].y + 0.8 * NM)),
                                         "color": YELLOW, "size": 12, "groups": list(groups),
                                         "text": f"{f.callsign} {f.mission} {hhmm(at[1] + self.off)}"})
        for f in buttons:
            plan_pts = [p for p, _, _ in self._plan(f)]
            taken = sum(1 for v in f.player_seats.values() if v)
            name, tail = f"{f.callsign}  {f.mission}", f"{taken}/{f.aircraft} seats"
            button = {"select": f"f:{f.id}", "info": infos[f.id], "name": name, "tail": tail, **_bounds(plan_pts)}
            if f.state is FlightState.PLANNED:
                # Counted down by the client between refreshes, on DCS model time (as the campaign clock).
                button["takeoff_in"] = round(f.takeoff_time - st.clock)
                when = t_minus(f.takeoff_time - st.clock)
            else:
                when = f.state.name.lower().replace("_", " ")
            button["label"] = f"{name}  {when}  {tail}"
            self.flight_buttons.append(button)
        for f in live:
            if f.state not in AIRBORNE or f.pos is None:
                continue
            sq = st.squadrons[f.squadron_id]
            hdg = f.hdg if f.hdg is not None else 0.0
            if sq.side is self.side:
                self.objects.append({"kind": "icon", "sym": flight_sym(f, sq.aircraft_type),
                                     "color": YELLOW if f.player_preferred else SIDE_COLOR[self.side], **xy(f.pos),
                                     "heading": round(hdg), "label": f.callsign, "info": infos.get(f.id, ""),
                                     "select": f"f:{f.id}"})
            elif any(math.hypot(f.pos.x - r.x, f.pos.y - r.y) <= RADAR_M for r in radars) or \
                    any(math.hypot(f.pos.x - a.x, f.pos.y - a.y) <= AWACS_M for a in awacs):
                alt = f" at {round((f.alt_m or 0) / FT / 1000)},000 ft" if f.alt_m else ""
                self.objects.append({"kind": "icon", "sym": flight_sym(f, sq.aircraft_type),
                                     "color": SIDE_COLOR[sq.side], **xy(f.pos), "heading": round(hdg),
                                     "info": f"Enemy {flight_kind(f, sq.aircraft_type)}, {about(f.aircraft) if f.aircraft > 3 else f.aircraft}"
                                             f"\nHeading {self.mag(hdg)}{alt}"})
        return coming

    def _plan(self, f: Flight) -> list[tuple[Pos, float, Waypoint | None]]:
        """The flight's plan: its airbase at take-off, then each waypoint with its time (its own time on
        target if it has one, else flown at the leg's speed; an orbit or attack holds it there)."""
        sq = self.st.squadrons[f.squadron_id]
        if sq.base_id not in self.th.objectives:
            return []
        here = self.opos(sq.base_id)
        t = f.takeoff_time
        out: list[tuple[Pos, float, Waypoint | None]] = [(here, t, None)]
        cruise = flight_cruise_ms(self.st, f)
        for w in f.waypoints:
            t += math.hypot(w.pos.x - here.x, w.pos.y - here.y) / (w.speed_ms or cruise)
            if w.tot is not None:
                t = w.tot
            out.append((w.pos, t, w))
            if w.action in TIMED_ACTIONS:
                t += float(w.params.get("duration_s", 0) or 0)
            here = w.pos
        return out

    def _flight_info(self, f: Flight, sq, target: str) -> str:
        lines = [f"{f.callsign}: {f.aircraft}x {sq.aircraft_type}", f"{f.mission}" + (f" at {target}" if target else "")]
        if f.state is FlightState.PLANNED:
            lines.append(f"Takes off {hhmm(f.takeoff_time + self.off)} from "
                         f"{self.th.objectives[sq.base_id].name if sq.base_id in self.th.objectives else sq.base_id}")
        else:
            alt = f", {f.alt_m / FT:,.0f} ft" if f.alt_m is not None else ""
            hdg = f", heading {self.mag(f.hdg)}" if f.hdg is not None else ""
            lines.append(f"{f.state.name.lower().replace('_', ' ')}{alt}{hdg}")
        if f.player_preferred:
            taken = {seat: who for seat, who in f.player_seats.items() if who}
            lines.append(f"Player flight: {len(taken)} of {f.aircraft} seats taken"
                         + (", committed" if f.committed else ""))
            for seat in sorted(taken):
                who = taken[seat]
                lines.append(f"  {seat_name(f, seat)}: " + ("you" if who == self.me else self.pilots.get(who, "a player")))
            if f.committed:
                ai = [seat_name(f, s_) for s_ in range(1, f.aircraft + 1) if s_ not in f.slots]
                if ai:
                    lines.append(f"  AI: {', '.join(ai)}")
        return "\n".join(lines)

    def join_button(self, f: Flight) -> dict[str, Any] | None:
        """The panel's join button for a selected flight: {label, key, ok}; None if players can't fly it."""
        if not f.player_preferred:
            return None
        gid = f"f:{f.id}"
        seat = seat_of(f, self.me) if self.me else None
        if seat is not None:
            return {"label": f"Leave {f.callsign} (you're {f.callsign}-{seat})", "key": f"leave:{gid}", "ok": True}
        if f.state is not FlightState.PLANNED:
            return {"label": f"{f.callsign} has taken off", "key": "", "ok": False}
        free = len(free_seats(f))
        if free <= 0:
            return {"label": f"{f.callsign} is full", "key": "", "ok": False}
        return {"label": f"Join {f.callsign} ({free} of {f.aircraft} seats free)", "key": f"join:{gid}", "ok": True}

    def action_button(self, f: Flight) -> dict[str, Any] | None:
        """The second button, for a player seated in the selected flight: Commit (its lead, once
        everyone's in: builds the session mission) or, once committed, Fly (from FLY_OPEN_S before
        take-off: into the seat's jet). {label, key, ok}; Fly before it opens also has opens_in
        (seconds, counted down by the client) and ready (the label once open)."""
        seat = seat_of(f, self.me) if f.player_preferred else None
        if seat is None:
            return None
        gid = f"f:{f.id}"
        me = seat_name(f, seat)
        if f.state not in (FlightState.PLANNED, FlightState.TAXI):
            return {"label": f"{f.callsign} has taken off", "key": "", "ok": False}
        if not f.committed:
            if f.state is not FlightState.PLANNED:
                return None
            if seat == 1:
                return {"label": f"Commit {f.callsign} (restarts the mission)", "key": f"commit:{gid}", "ok": True}
            return {"label": f"Waiting for {seat_name(f, 1)} to commit", "key": "", "ok": False}
        if seat not in f.slots:
            return {"label": f"{me} has no jet in this mission", "key": "", "ok": False}
        opens = f.takeoff_time - FLY_OPEN_S - self.st.clock
        if opens > 0:
            return {"label": f"Fly {me} from {hhmm(f.takeoff_time - FLY_OPEN_S + self.off)}", "key": f"fly:{gid}",
                    "ok": False, "opens_in": round(opens), "ready": f"Fly {me}"}
        return {"label": f"Fly {me}", "key": f"fly:{gid}", "ok": True}

    def _panel(self, seen: int, coming: list[Flight]) -> list[str]:
        st = self.st
        # The campaign's day; the time of day as DCS has it.
        lines = [f"Day {int(st.clock) // 86400 + 1}  {hhmm(st.clock + self.off)}"]
        if self.weather:
            lines.append(self.weather[:1].upper() + self.weather[1:])
        lines.append("")
        lines += ["FRONT", f"  {self.name(Side.BLUE)} {len(self.held(Side.BLUE))} objectives, "
                           f"{self.name(Side.RED)} {len(self.held(Side.RED))}"]
        battles = sorted(st.battles.values(), key=lambda b: -b.hours)
        for b in battles[:4]:
            if b.at in self.th.objectives:
                lines.append(f"  {self.th.objectives[b.at].name}: {self.name(b.attacker)} attacking, {b.hours} h")
        if len(battles) > 4:
            lines.append(f"  and {len(battles) - 4} more battles")
        if not battles:
            lines.append("  No battles")
        if seen:
            lines.append(f"  {seen} enemy formations in contact")
        # Our players' flights are buttons below this text (spec "flights"); more than fit are counted here.
        if len(coming) > PANEL_FLIGHTS:
            lines += ["", f"UPCOMING FLIGHTS: {len(coming) - PANEL_FLIGHTS} more in the next "
                          f"{PANEL_AHEAD_S // 3600} h (below: the first)"]
        if self.news:
            lines += ["", "NEWS"] + [f"  {n if len(n) <= NEWS_CHARS else n[:NEWS_CHARS - 3] + '...'}"
                                     for n in self.news[-6:][::-1]]
        return lines


# ---------- sending it ----------

HookCall = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass
class Viewer:
    player_id: int
    version: int | None = None
    sent: float = 0.0          # real time of the last map sent
    ucid: str | None = None    # from player_connect or a click: their seat in a flight


class WarMap:
    """Sends the war map to players with the client script (see the module docstring)."""

    def __init__(self, state: CampaignState, theater: Theater, hooks_call: HookCall, *, side: Side = Side.BLUE,
                 magvar: float = 0.0, title: str = "Campaign", side_names: dict[Side, str] | None = None,
                 refresh_s: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.st, self.th, self.hooks_call = state, theater, hooks_call
        self.side, self.magvar, self.title = side, magvar, title
        self.side_names = side_names or {}
        self.refresh_s = refresh_s
        self.clock = clock
        self.viewers: dict[int, Viewer] = {}
        self.news: deque[str] = deque(maxlen=NEWS_KEPT)
        self._terrain: tuple[tuple, Territory] | None = None
        self._sending = False
        self.last_bytes = 0
        self.last_build_ms = 0.0
        self.tod_offset = 0.0     # campaign clock -> DCS time of day (set_mission)
        self.weather = ""
        self.ucids: dict[int, str] = {}     # player id -> ucid (player_connect, clicks)
        self.pilots: dict[str, str] = {}    # ucid -> name
        # Commit and Fly (campaign/session.py); set by the server. Without it they answer "not here".
        self.session: Any = None

    def set_mission(self, info: dict[str, Any], clock_now: float) -> None:
        """The loaded mission's time and weather (the mission bridge's ``query mission``), on each
        mission connect: the map gives times of day as DCS has them, and the weather."""
        abs_t = info.get("abs")
        if isinstance(abs_t, (int, float)):
            self.tod_offset = float(abs_t) - clock_now
        self.weather = weather_text(info.get("weather") if isinstance(info.get("weather"), dict) else None)
        log.info("MAP mission time of day %s, weather: %s", hhmm(clock_now + self.tod_offset), self.weather or "?")

    def add_news(self, text: str) -> None:
        self.news.append(f"{hhmm(self.st.clock + self.tod_offset)} {text[:1].upper()}{text[1:]}")

    def terrain(self) -> Territory:
        """Recomputed only when an objective changes hands."""
        key = tuple(sorted((oid, o.owner.name) for oid, o in self.st.objectives.items()))
        if self._terrain is None or self._terrain[0] != key:
            pos = self.th.objectives
            self._terrain = (key, territory(
                [pos[o].pos for o, s in self.st.objectives.items() if s.owner is Side.BLUE and o in pos],
                [pos[o].pos for o, s in self.st.objectives.items() if s.owner is Side.RED and o in pos]))
        return self._terrain[1]

    def spec(self, *, show: bool, keep_view: bool, player_id: int | None = None) -> dict[str, Any]:
        t0 = time.perf_counter()
        spec = Picture(self.st, self.th, self.side, magvar=self.magvar, title=self.title, news=self.news,
                       side_names=self.side_names, terrain=self.terrain(), tod_offset=self.tod_offset,
                       weather=self.weather, me=self.ucids.get(player_id) if player_id is not None else None,
                       pilots=self.pilots).build()
        spec["show"] = show
        spec["keep_view"] = keep_view
        self.last_build_ms = (time.perf_counter() - t0) * 1000
        return spec

    async def send(self, player_id: int, *, show: bool, keep_view: bool) -> None:
        spec = self.spec(show=show, keep_view=keep_view, player_id=player_id)
        self.last_bytes = len(json.dumps(spec, separators=(",", ":")))
        await self.hooks_call("client_rpc", {"player_id": player_id, "method": "campaignMap", "args": [spec]})
        v = self.viewers.setdefault(player_id, Viewer(player_id))
        v.sent = self.clock()
        log.info("MAP sent to player %s (%s, %d objects, %.0f kB, built in %.0f ms)", player_id,
                 "shown" if show else "refresh", len(spec["objects"]), self.last_bytes / 1000, self.last_build_ms)

    async def reply(self, player_id: int, text: str) -> None:
        await self.hooks_call("chat_to", {"player_id": player_id, "text": "> " + text})

    # ----- events from the Hooks (``player`` messages) -----

    def on_player(self, data: dict[str, Any], background: Callable[[Awaitable[Any]], None]) -> bool:
        """Handles what's for the war map; True if it was."""
        kind = data.get("kind")
        pid = data.get("player_id")
        if not isinstance(pid, int):
            return False
        if kind == "client_hello":
            self.viewers[pid] = Viewer(pid, version=data.get("version"))
            log.info("MAP player %s has the client script (version %s)", pid, data.get("version"))
            background(self._safe(self._hello(pid)))
            return True
        if kind == "client_web" and str(data.get("stage", "")).startswith("map"):
            log.info("MAP player %s: %s", pid, str(data.get("detail") or "").replace("\n", " | "))
            return True
        if kind == "player_connect" and data.get("ucid"):
            self.ucids[pid] = str(data["ucid"])
            if data.get("name"):
                self.pilots[str(data["ucid"])] = str(data["name"])
            return False
        if kind == "player_disconnect":
            self.viewers.pop(pid, None)
            return False
        if kind == "client_click" and str(data.get("key", "")).startswith(("join:", "leave:", "commit:", "fly:")):
            if data.get("ucid"):
                self.ucids[pid] = str(data["ucid"])
                if data.get("name"):
                    self.pilots[str(data["ucid"])] = str(data["name"])
            what, _, gid = str(data["key"]).partition(":")
            background(self._safe(self._join(pid, what, gid.removeprefix("f:"))))
            return True
        if kind == "chat_command":
            words = str(data.get("text") or "").lower().split()
            if words[:1] == ["map"]:
                background(self._safe(self._command(pid, words[1:])))
                return True
        return False

    async def _hello(self, pid: int) -> None:
        await self.send(pid, show=False, keep_view=False)
        await self.reply(pid, "War map ready: Ctrl+Shift+F8 shows or hides it (or type -map)")

    async def _command(self, pid: int, rest: list[str]) -> None:
        known = pid in self.viewers   # said hello since the server started
        await self.send(pid, show=True, keep_view=False)
        if not known:   # maybe the server restarted since; maybe no client script: say what it needs
            await self.reply(pid, "War map sent: Ctrl+Shift+F8 shows or hides it. Nothing showing? It needs the "
                                  "campaign client script; F10 > Other > Campaign > Status works without it")

    def join(self, player_id: int, what: str, flight_id: str) -> str:
        """A player takes a seat in a player flight ("join") or gives it up ("leave"). Returns the
        reply. The seat is theirs (Flight.player_seats); its lead's Commit and their own Fly get
        them into the jet (campaign/session.py). Once committed, only its Client seats can be joined."""
        ucid = self.ucids.get(player_id)
        f = self.st.flights.get(flight_id)
        sq = self.st.squadrons.get(f.squadron_id) if f else None
        if ucid is None:
            return "Can't tell who you are yet: try again in a moment"
        if f is None or sq is None or sq.side is not self.side or not f.player_preferred:
            return "That flight isn't one players can fly"
        if what == "leave":
            seat = seat_of(f, ucid)
            if seat is None:
                return f"You aren't in {f.callsign}"
            if f.spawn is SpawnState.SPAWNED and f.committed:
                return f"{f.callsign} is in DCS: leave your jet instead"
            f.player_seats[seat] = None
            log.info("MAP player %s (%s) leaves %s-%d", player_id, self.pilots.get(ucid, ucid), f.callsign, seat)
            return f"You've left {f.callsign}"
        if seat_of(f, ucid) is not None:
            return f"You're already {f.callsign}-{seat_of(f, ucid)}"
        if f.state is not FlightState.PLANNED:
            return f"{f.callsign} has taken off"
        free = free_seats(f)
        if not free:
            return f"{f.callsign} is full" + (" (committed: its other seats are AI)" if f.committed else "")
        left = []
        for other in self.st.flights.values():   # one flight at a time
            s_ = seat_of(other, ucid)
            if s_ is not None:
                other.player_seats[s_] = None
                left.append(other.callsign)
        f.player_seats[free[0]] = ucid
        log.info("MAP player %s (%s) joins %s-%d%s", player_id, self.pilots.get(ucid, ucid), f.callsign, free[0],
                 f" (left {', '.join(left)})" if left else "")
        if f.committed:
            nxt = f"Fly opens at {hhmm(f.takeoff_time - FLY_OPEN_S + self.tod_offset)}"
        elif free[0] == 1:
            nxt = "You lead it: press Commit once everyone's in (it restarts the mission with your jets)"
        else:
            nxt = f"{seat_name(f, 1)} commits once everyone's in"
        return (f"You're {seat_name(f, free[0])}: {f.mission}, take-off {hhmm(f.takeoff_time + self.tod_offset)} "
                f"({t_minus(f.takeoff_time - self.st.clock)})" + (f". You've left {', '.join(left)}" if left else "") +
                f". {nxt}")

    async def _join(self, player_id: int, what: str, flight_id: str) -> None:
        if what in ("commit", "fly"):
            if self.session is None:
                text = "Getting into the jet isn't available on this server"
            elif what == "commit":
                text = self.session.commit(player_id, self.ucids.get(player_id), flight_id)
            else:
                text = await self.session.fly(player_id, self.ucids.get(player_id), flight_id)
        else:
            text = self.join(player_id, what, flight_id)
            if self.session is not None:
                await self.session.seats_changed()
        await self.reply(player_id, text)
        # The buttons and seats as they are now; not opened over the cockpit after Fly.
        await self.send(player_id, show=what != "fly", keep_view=True)

    async def _safe(self, coro: Awaitable[Any]) -> None:
        try:
            await coro
        except Exception as e:  # noqa: BLE001 - a map that fails must not stop the server
            log.warning("war map: %s", e)

    async def tick(self) -> None:
        """Refresh each viewer's map every ``refresh_s`` (hidden maps too: the hotkey shows the latest)."""
        if self._sending:
            return
        now = self.clock()
        due = [v for v in self.viewers.values() if now - v.sent >= self.refresh_s]
        if not due:
            return
        self._sending = True
        try:
            for v in due:
                await self._safe(self.send(v.player_id, show=False, keep_view=True))
                v.sent = now   # also after a failure: try again next period, not every tick
        finally:
            self._sending = False
