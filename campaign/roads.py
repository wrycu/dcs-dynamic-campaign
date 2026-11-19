"""Polylines along roads: length, a point and heading at a distance, and where a position is
along one. Road paths come from DCS (``query road_path``, i.e. ``land.findPathOnRoads``).

Positions are model ``Pos`` (x north, y east, metres); headings are radians clockwise from north.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .model import Pos


@dataclass(frozen=True)
class Projection:
    along: float    # distance along the path to the closest point (m)
    offset: float   # distance from the path (m)
    pos: Pos        # the closest point on the path


class Path:
    def __init__(self, points: list[Pos]) -> None:
        if not points:
            raise ValueError("a path needs at least one point")
        self.points = points
        self.cum = [0.0]
        for a, b in zip(points, points[1:]):
            self.cum.append(self.cum[-1] + math.dist((a.x, a.y), (b.x, b.y)))

    @property
    def length(self) -> float:
        return self.cum[-1]

    def _segment(self, s: float) -> int:
        """Index i of the segment points[i] -> points[i+1] containing distance s."""
        s = min(max(s, 0.0), self.length)
        lo, hi = 0, len(self.points) - 2
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.cum[mid] <= s:
                lo = mid
            else:
                hi = mid - 1
        return max(lo, 0)

    def point_at(self, s: float) -> Pos:
        if len(self.points) == 1:
            return self.points[0]
        s = min(max(s, 0.0), self.length)
        i = self._segment(s)
        a, b = self.points[i], self.points[i + 1]
        seg = self.cum[i + 1] - self.cum[i]
        k = 0.0 if seg == 0 else (s - self.cum[i]) / seg
        return Pos(a.x + (b.x - a.x) * k, a.y + (b.y - a.y) * k)

    def heading_at(self, s: float) -> float:
        """Direction of travel at distance s; looks past zero-length segments."""
        if len(self.points) == 1:
            return 0.0
        i = self._segment(s)
        for j in list(range(i, len(self.points) - 1)) + list(range(i - 1, -1, -1)):
            a, b = self.points[j], self.points[j + 1]
            if (a.x, a.y) != (b.x, b.y):
                return math.atan2(b.y - a.y, b.x - a.x) % (2 * math.pi)
        return 0.0

    def project(self, p: Pos) -> Projection:
        best: Projection | None = None
        if len(self.points) == 1:
            q = self.points[0]
            return Projection(0.0, math.dist((p.x, p.y), (q.x, q.y)), q)
        for i, (a, b) in enumerate(zip(self.points, self.points[1:])):
            dx, dy = b.x - a.x, b.y - a.y
            seg2 = dx * dx + dy * dy
            k = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / seg2))
            q = Pos(a.x + dx * k, a.y + dy * k)
            d = math.dist((p.x, p.y), (q.x, q.y))
            if best is None or d < best.offset:
                best = Projection(self.cum[i] + k * math.sqrt(seg2), d, q)
        assert best is not None
        return best

    def after(self, s: float) -> list[Pos]:
        """The path from distance s to the end: the point at s, then the remaining vertices."""
        i = self._segment(s)
        return [self.point_at(s)] + self.points[i + 1:]

    def reversed(self) -> Path:
        return Path(list(reversed(self.points)))


def simplify(points: list[Pos], tolerance_m: float) -> list[Pos]:
    """Drop points that lie within tolerance_m of the line through their neighbours
    (Douglas-Peucker); keeps the first and last points."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        a, b = points[i], points[j]
        dx, dy = b.x - a.x, b.y - a.y
        seg2 = dx * dx + dy * dy
        worst, k_worst = -1.0, -1
        for k in range(i + 1, j):
            p = points[k]
            if seg2 == 0:
                d = math.dist((p.x, p.y), (a.x, a.y))
            else:
                d = abs(dx * (a.y - p.y) - dy * (a.x - p.x)) / math.sqrt(seg2)
            if d > worst:
                worst, k_worst = d, k
        if worst > tolerance_m:
            keep[k_worst] = True
            stack += [(i, k_worst), (k_worst, j)]
    return [p for p, k in zip(points, keep) if k]
