"""Ghost players for testing with one person: fixed points the bubbles treat as extra players.

    --ghost 090/100      magnetic bearing / range in nm from Khalkhalah
    --ghost Damascus     on an airbase (looked up in DCS when the mission connects)

Ghosts are added to the ``players`` list of telemetry before it reaches the bubbles, so ground
formations and AI flights near a ghost spawn as if a player were there. They are drawn on the F10
map as "GHOST n". Status displays and the like still see only the real players.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from campaign.bubble import NM
from campaign.model import Pos
from tools.build_test_missions import AIRBASE_NAME, AIRBASE_XY

GHOST_DRAW_ID = 3900
GHOST_COLOR = [1, 0.85, 0.2, 1]


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--ghost", action="append", default=[], metavar="BRG/NM|AIRBASE",
                    help=f"a ghost player at magnetic bearing/range (nm) from {AIRBASE_NAME}, e.g. 090/100, "
                         "or on an airbase by name; repeat for more")


@dataclass
class Ghost:
    label: str
    spec: str
    pos: Pos | None = None


def parse(spec: str, magvar: float, ref: Pos = Pos(*AIRBASE_XY)) -> Ghost:
    """``BRG/NM`` (magnetic, from ``ref``) gives a position now; anything else is an airbase name."""
    brg, sep, rng = spec.partition("/")
    if sep:
        try:
            b, r = float(brg), float(rng)
        except ValueError:
            raise ValueError(f"bad ghost {spec!r}: expected BRG/NM, e.g. 090/100") from None
        true = math.radians(b + magvar)  # magnetic = true - magvar
        return Ghost(spec, spec, Pos(ref.x + r * NM * math.cos(true), ref.y + r * NM * math.sin(true)))
    return Ghost(spec, spec)


class Ghosts:
    def __init__(self, specs: list[str], magvar: float) -> None:
        self.ghosts = [parse(s, magvar) for s in specs]

    def __bool__(self) -> bool:
        return bool(self.ghosts)

    def inject(self, data: dict[str, Any]) -> dict[str, Any]:
        """Telemetry with the placed ghosts appended to ``players``."""
        extra = [{"name": f"GHOST {i}", "ghost": True, "pos": {"x": g.pos.x, "y": g.pos.y}}
                 for i, g in enumerate(self.ghosts, start=1) if g.pos is not None]
        if not extra:
            return data
        players = data.get("players")
        return {**data, "players": (list(players) if isinstance(players, list) else []) + extra}

    async def setup(self, call: Callable[[str, dict], Awaitable[Any]]) -> list[str]:
        """Place ghosts given as airbase names and draw all of them. Returns lines to print."""
        lines = []
        for i, g in enumerate(self.ghosts, start=1):
            if g.pos is None:
                ab = await call("query", {"what": "airbase", "airbase": g.spec})
                if not ab or not ab.get("exists"):
                    lines.append(f"GHOST {i}: airbase {g.spec!r} not found; ignored")
                    continue
                g.pos = Pos(ab["pos"]["x"], ab["pos"]["y"])
            await call("draw", {"id": GHOST_DRAW_ID + i, "audience": "all", "shape": "text",
                                "pos": {"x": g.pos.x, "y": g.pos.y}, "font_size": 14,
                                "text": f"GHOST {i} ({g.label})", "color": GHOST_COLOR, "read_only": True})
            lines.append(f"GHOST {i} ({g.label}) placed")
        return lines
