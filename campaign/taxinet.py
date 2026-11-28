"""An airbase's taxiway network, read from DCS's own files, and routes on it (proof P22, Run 30).

DCS keeps each airbase's taxiways and runways as a road network the aircraft AI taxis on:
``Mods/terrains/<theatre>/AirfieldsTaxiways/<Airbase_Name>.rn5`` in the DCS install. Nothing in
the Lua APIs plans a route on it for a vehicle, so the engine reads the file itself and gives the
ride's Humvee waypoints along the taxiways ("Off Road" waypoints, close together), instead of a
straight line across the ground.

The format isn't documented; what this reads (decoded 2026-11-27 from Khalkhalah.rn5, DCS 2.9.32):
after a header and a list of segment type names (``taxiway_15m``, ``taxiway_24m``, ``taxiway_34m``,
``runway_45m``, then stand and junction names), each edge is a record of 32-bit ints
``[?, edge id, type, node a, ?, node b, ?, 0, n]`` followed by ``n`` points of three float64s
``(x, height, z)`` in DCS world coordinates (x north, z east), then more data this doesn't use.
Type 3 is the runway. The polylines stop short of their junctions, so the network is joined by
the node ids, and each node sits where its edges' nearest ends are. Records are found by
scanning for that shape, which is robust to the parts not understood.
"""
from __future__ import annotations

import heapq
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .model import Pos
from .roads import simplify

RUNWAY = 3                 # the edge type of runway segments
RUNWAY_PENALTY = 20.0      # a metre of runway counts as this many (vehicles keep off it)
WORLD_M = 1.0e6            # coordinates beyond this aren't points
HEIGHT_M = 1.0e4
MAX_POINTS = 1000


@dataclass
class Edge:
    id: int
    type: int
    a: int
    b: int
    points: list[Pos]
    length: float = 0.0

    def __post_init__(self) -> None:
        self.length = sum(math.dist((p.x, p.y), (q.x, q.y)) for p, q in zip(self.points, self.points[1:]))


@dataclass
class TaxiNet:
    edges: list[Edge]
    nodes: dict[int, Pos] = field(default_factory=dict)
    _adj: dict[int, list[Edge]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for e in self.edges:
            self._adj.setdefault(e.a, []).append(e)
            self._adj.setdefault(e.b, []).append(e)
        if not self.nodes:
            self.nodes = _node_positions(self.edges)

    # ----- geometry -----

    def oriented(self, e: Edge, start_node: int) -> list[Pos]:
        """The edge's points running away from ``start_node``."""
        pts = e.points
        n = self.nodes[start_node]
        if _d(pts[-1], n) < _d(pts[0], n):
            return list(reversed(pts))
        return list(pts)

    def nearest(self, p: Pos, keep_off_runway: bool = True) -> tuple[Edge, int, Pos, float]:
        """The nearest point on the network to ``p``: (edge, segment index, the point, distance)."""
        best: tuple[Edge, int, Pos, float] | None = None
        for e in self.edges:
            if keep_off_runway and e.type == RUNWAY:
                continue
            for i, (a, b) in enumerate(zip(e.points, e.points[1:])):
                q = _project(p, a, b)
                d = _d(p, q)
                if best is None or d < best[3]:
                    best = (e, i, q, d)
        if best is None:
            if keep_off_runway:
                return self.nearest(p, keep_off_runway=False)
            raise ValueError("empty taxiway network")
        return best

    # ----- routes -----

    def route(self, start: Pos, goal: Pos, tolerance_m: float = 2.0) -> list[Pos]:
        """A route from ``start`` to ``goal`` along the taxiways: straight onto the network at its
        nearest point, along it (keeping off the runway where it can), and straight off it to the
        goal. Thinned to ``tolerance_m``."""
        e0, i0, p0, _ = self.nearest(start)
        e1, i1, p1, _ = self.nearest(goal)
        if e0 is e1:   # the same edge: along it between the two points
            pts = e0.points
            if i0 <= i1:
                mid = pts[i0 + 1:i1 + 1]
            else:
                mid = list(reversed(pts[i1 + 1:i0 + 1]))
            if i0 == i1:
                mid = []
            return simplify([start, p0, *mid, p1, goal], tolerance_m)
        # Leaving the start's edge towards either of its nodes, arriving on the goal's edge from either.
        out = {e0.a: self._piece(e0, i0, p0, e0.a), e0.b: self._piece(e0, i0, p0, e0.b)}
        into = {e1.a: self._piece(e1, i1, p1, e1.a), e1.b: self._piece(e1, i1, p1, e1.b)}
        dist: dict[int, float] = {}
        prev: dict[int, tuple[int, Edge] | None] = {}
        heap: list[tuple[float, int]] = []
        for n, piece in out.items():
            c = _length(piece) * (RUNWAY_PENALTY if e0.type == RUNWAY else 1.0)
            if c < dist.get(n, math.inf):
                dist[n], prev[n] = c, None
                heapq.heappush(heap, (c, n))
        done: set[int] = set()
        while heap:
            c, n = heapq.heappop(heap)
            if n in done:
                continue
            done.add(n)
            for e in self._adj.get(n, []):
                if e is e0 or e is e1:
                    continue
                m = e.b if e.a == n else e.a
                nc = c + e.length * (RUNWAY_PENALTY if e.type == RUNWAY else 1.0)
                if nc < dist.get(m, math.inf):
                    dist[m], prev[m] = nc, (n, e)
                    heapq.heappush(heap, (nc, m))
        best_n, best_c = None, math.inf
        for n, piece in into.items():
            c = dist.get(n, math.inf) + _length(piece) * (RUNWAY_PENALTY if e1.type == RUNWAY else 1.0)
            if c < best_c:
                best_n, best_c = n, c
        if best_n is None:
            raise ValueError("no route on the taxiways")
        chain: list[Pos] = []
        n = best_n
        legs: list[list[Pos]] = []
        while prev.get(n) is not None:
            m, e = prev[n]
            legs.append(self.oriented(e, m))
            n = m
        first = list(reversed(out[n]))     # p0 -> node n
        for leg in reversed(legs):
            chain.extend(leg)
        last = into[best_n]                # node -> p1
        pts = [start, *first, *chain, *last, goal]
        return simplify(pts, tolerance_m)

    def _piece(self, e: Edge, i: int, p: Pos, node: int) -> list[Pos]:
        """The part of edge ``e`` from point ``p`` (on segment ``i``) to its end at ``node``,
        listed from the node to p."""
        pts = e.points
        toward_end = _d(pts[-1], self.nodes[node]) < _d(pts[0], self.nodes[node])
        if toward_end:
            return list(reversed([p, *pts[i + 1:]]))
        return [*pts[:i + 1], p]


def route_length(points: list[Pos]) -> float:
    return _length(points)


# ---------- reading the file ----------

def parse(data: bytes) -> TaxiNet:
    """The network in an .rn5 file's bytes (see the module docstring)."""
    edges: dict[int, Edge] = {}
    o = 40
    size = len(data)
    while o < size - 24:
        n = struct.unpack_from("<i", data, o - 4)[0]
        if 2 <= n <= MAX_POINTS and o + 24 * n <= size and o >= 36:
            pts = [struct.unpack_from("<ddd", data, o + 24 * i) for i in range(n)]
            if all(_is_point(x, y, z) for x, y, z in pts):
                hdr = struct.unpack_from("<9i", data, o - 36)
                _, eid, etype, a, _, b, _, zero, _ = hdr
                if 0 <= eid < 100_000 and 0 <= etype < 64 and a >= 0 and b >= 0 and zero == 0 and eid not in edges:
                    edges[eid] = Edge(eid, etype, a, b, [Pos(x, z) for x, _, z in pts])
                    o += 24 * n
                    continue
        o += 1
    if not edges:
        raise ValueError("no taxiway edges found")
    return TaxiNet([edges[k] for k in sorted(edges)])


def taxiways_file(dcs_dir: Path, theatre: str, airbase: str) -> Path:
    return dcs_dir / "Mods" / "terrains" / theatre / "AirfieldsTaxiways" / f"{airbase.replace(' ', '_')}.rn5"


def load(dcs_dir: Path, theatre: str, airbase: str) -> TaxiNet | None:
    """The airbase's network, or None if DCS has no file for it."""
    path = taxiways_file(dcs_dir, theatre, airbase)
    if not path.is_file():
        return None
    return parse(path.read_bytes())


# ---------- helpers ----------

def _is_point(x: float, y: float, z: float) -> bool:
    return (abs(x) < WORLD_M and abs(z) < WORLD_M and abs(y) < HEIGHT_M and (abs(x) > 1 or abs(z) > 1)
            and math.isfinite(x) and math.isfinite(z))


def _node_positions(edges: list[Edge]) -> dict[int, Pos]:
    """Each node where its edges' ends meet: start from the mean of all their ends, then keep, for
    each edge, the end nearer the node, a few times over."""
    ends: dict[int, list[tuple[Pos, Pos]]] = {}
    for e in edges:
        pair = (e.points[0], e.points[-1])
        ends.setdefault(e.a, []).append(pair)
        ends.setdefault(e.b, []).append(pair)
    nodes: dict[int, Pos] = {}
    for n, pairs in ends.items():
        pts = [p for pair in pairs for p in pair]
        c = Pos(sum(p.x for p in pts) / len(pts), sum(p.y for p in pts) / len(pts))
        for _ in range(5):
            near = [min(pair, key=lambda p: _d(p, c)) for pair in pairs]
            c = Pos(sum(p.x for p in near) / len(near), sum(p.y for p in near) / len(near))
        nodes[n] = c
    # A node at the end of one edge only: its end is ambiguous from the mean; take the end of that
    # edge farther from the edge's other node.
    for e in edges:
        for n, other in ((e.a, e.b), (e.b, e.a)):
            if len(ends[n]) == 1 and other in nodes and len(ends[other]) > 1:
                first, last = e.points[0], e.points[-1]
                nodes[n] = first if _d(first, nodes[other]) > _d(last, nodes[other]) else last
    return nodes


def _d(a: Pos, b: Pos) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _length(points: list[Pos]) -> float:
    return sum(_d(p, q) for p, q in zip(points, points[1:]))


def _project(p: Pos, a: Pos, b: Pos) -> Pos:
    dx, dy = b.x - a.x, b.y - a.y
    seg2 = dx * dx + dy * dy
    if seg2 == 0:
        return a
    t = max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / seg2))
    return Pos(a.x + dx * t, a.y + dy * t)
