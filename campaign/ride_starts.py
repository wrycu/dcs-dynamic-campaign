"""Where rides to the jets start, mapped by hand (Run 31: the tower is rarely the right place).

Each airbase can have any number of starts (a big base has several ramps); a ride starts at the
one nearest its flight's jets. They're placed in DCS on the F10 map: a mark with the text
``ride start`` saves its spot for the nearest airbase; ``ride start delete`` removes the saved
start nearest it. Changing a saved mark's text to something else removes it too. Kept in
``data/ride_starts.json`` (by theatre, then DCS airbase name), with the scenario data.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

from .model import Pos

PATH = Path(__file__).resolve().parent.parent / "data" / "ride_starts.json"
ADD_WORDS = "ride start"
DELETE_WORDS = ("ride start delete", "delete ride start")
BASE_M = 6000.0       # a mark this close to an airbase is for it
DELETE_M = 150.0      # a delete mark removes the saved start this close to it


def _d(a: Pos, b: Pos) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def mark_intent(text: Any) -> str | None:
    """"add", "delete" or None for a mark's text."""
    t = " ".join(str(text or "").lower().replace("-", " ").split())
    if any(t.startswith(w) for w in DELETE_WORDS):
        return "delete"
    if t.startswith(ADD_WORDS):
        return "add"
    return None


class RideStarts:
    def __init__(self, path: Path = PATH) -> None:
        self.path = path
        self.data: dict[str, dict[str, list[dict[str, Any]]]] = {}
        if path.is_file():
            self.data = json.loads(path.read_text())

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")

    def at(self, theatre: str, airbase: str) -> list[Pos]:
        return [Pos(float(s["x"]), float(s["y"])) for s in self.data.get(theatre, {}).get(airbase, [])]

    def all(self, theatre: str) -> list[tuple[str, Pos]]:
        return [(ab, Pos(float(s["x"]), float(s["y"]))) for ab, starts in sorted(self.data.get(theatre, {}).items())
                for s in starts]

    def nearest(self, theatre: str, airbase: str, to: Pos) -> Pos | None:
        starts = self.at(theatre, airbase)
        return min(starts, key=lambda p: _d(p, to)) if starts else None

    def add(self, theatre: str, airbase: str, p: Pos) -> int:
        """Saves a start; returns how many the airbase has now."""
        starts = self.data.setdefault(theatre, {}).setdefault(airbase, [])
        starts.append({"x": round(p.x, 1), "y": round(p.y, 1), "added": dt.date.today().isoformat()})
        self.save()
        return len(starts)

    def remove_near(self, theatre: str, airbase: str, p: Pos, within: float = DELETE_M) -> bool:
        starts = self.data.get(theatre, {}).get(airbase, [])
        near = [s for s in starts if _d(Pos(float(s["x"]), float(s["y"])), p) <= within]
        if not near:
            return False
        starts.remove(min(near, key=lambda s: _d(Pos(float(s["x"]), float(s["y"])), p)))
        if not starts:
            del self.data[theatre][airbase]
        self.save()
        return True


def nearest_airbase(airbases: dict[str, dict[str, Any]], p: Pos, within: float = BASE_M) -> str | None:
    """The DCS airbase (the mission bridge's ``airbases``: name -> {pos}) nearest ``p``, if close."""
    best = None
    for name, ab in airbases.items():
        pos = ab.get("pos") if isinstance(ab, dict) else None
        if not isinstance(pos, dict) or ab.get("category") not in (None, 0, "airdrome"):
            continue
        d = _d(Pos(float(pos["x"]), float(pos["y"])), p)
        if d <= within and (best is None or d < best[0]):
            best = (d, name)
    return best[1] if best else None
