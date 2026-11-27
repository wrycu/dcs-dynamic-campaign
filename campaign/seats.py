"""Players' seats in flights, and the times around getting into the jet (campaign/session.py).

Shared by the war map (its buttons) and the session (commit, fly, the deadline).
"""
from __future__ import annotations

from .model import Flight

FLY_OPEN_S = 20 * 60   # Fly opens this long before take-off (asked 2026-11-27)
LATE_S = 10 * 60       # nobody in the jets this long after take-off: the AI flies it


def seat_of(f: Flight, ucid: str | None) -> int | None:
    """The player's seat in the flight (1-based), or None."""
    if ucid is None:
        return None
    return next((seat for seat, who in sorted(f.player_seats.items()) if who == ucid), None)


def seat_name(f: Flight, seat: int) -> str:
    """The seat's callsign, as its unit is named in the session mission: "Uzi 4-1"."""
    return f"{f.callsign}-{seat}"


def group_label(f: Flight) -> str:
    """The flight's Client group in the session mission: "Uzi 4 SEAD"."""
    return f"{f.callsign} {f.mission}"


def free_seats(f: Flight) -> list[int]:
    """Seats a player can still join: any empty one, or once committed only its Client seats (the
    others are AI in the mission)."""
    seats = sorted(f.slots) if f.committed else range(1, f.aircraft + 1)
    return [s for s in seats if not f.player_seats.get(s)]
