"""Ghost players for testing with one person: fixed points the bubbles treat as extra players.

    python3 -m campaign.server --ghost 090/100 --ghost Damascus

- ``BRG/NM``: magnetic bearing and range in nm from the scenario's home airbase.
- anything else: an airbase, by its DCS name.

Ghosts are placed when the mission connects (airbases have to be known), and added to the
``players`` of telemetry before it reaches the bubbles, so ground formations and AI flights near
a ghost spawn as if a player were there. They are drawn on the F10 map as "GHOST n". Status and
the like still see only the real players.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .bubble import NM
from .model import Pos

GHOST_DRAW_ID = 3900
GHOST_COLOR = [1, 0.85, 0.2, 1]


@dataclass
class Ghost:
    spec: str
    brg_nm: tuple[float, float] | None = None   # (magnetic bearing, nm) from home, or None for an airbase
    pos: Pos | None = None


def parse(spec: str) -> Ghost:
    brg, sep, rng = spec.partition("/")
    if not sep:
        return Ghost(spec)
    try:
        return Ghost(spec, (float(brg), float(rng)))
    except ValueError:
        raise ValueError(f"bad ghost {spec!r}: expected BRG/NM (e.g. 090/100) or an airbase name") from None


def offset(ref: Pos, brg_mag: float, nm: float, magvar: float) -> Pos:
    true = math.radians(brg_mag + magvar)   # magnetic = true - magvar (east)
    return Pos(ref.x + nm * NM * math.cos(true), ref.y + nm * NM * math.sin(true))


class Ghosts:
    def __init__(self, specs: list[str]) -> None:
        self.ghosts = [parse(s) for s in specs]

    def __bool__(self) -> bool:
        return bool(self.ghosts)

    def place(self, airbase_pos: Callable[[str], Pos | None], home: str, magvar: float) -> list[str]:
        """Work out each ghost's position. Returns lines to log."""
        lines = []
        for i, g in enumerate(self.ghosts, start=1):
            if g.brg_nm is not None:
                ref = airbase_pos(home) if home else None
                g.pos = offset(ref, *g.brg_nm, magvar) if ref else None
                where = f"from {home}" if ref else f"(no home airbase {home!r} in this mission)"
            else:
                g.pos = airbase_pos(g.spec)
                where = "" if g.pos else "(no such airbase in this mission)"
            lines.append(f"GHOST {i} ({g.spec}) {where}: {'placed' if g.pos else 'ignored'}".replace(" :", ":"))
        return lines

    async def draw(self, call: Callable[[str, dict], Awaitable[Any]]) -> None:
        for i, g in enumerate(self.ghosts, start=1):
            if g.pos is not None:
                await call("draw", {"id": GHOST_DRAW_ID + i, "audience": "all", "shape": "text",
                                    "pos": {"x": g.pos.x, "y": g.pos.y}, "font_size": 14,
                                    "text": f"GHOST {i} ({g.spec})", "color": GHOST_COLOR, "read_only": True})

    def inject(self, data: dict[str, Any]) -> dict[str, Any]:
        """Telemetry with the placed ghosts appended to ``players``."""
        extra = [{"name": f"GHOST {i}", "ghost": True, "pos": {"x": g.pos.x, "y": g.pos.y}}
                 for i, g in enumerate(self.ghosts, start=1) if g.pos is not None]
        if not extra:
            return data
        players = data.get("players")
        return {**data, "players": (list(players) if isinstance(players, list) else []) + extra}
