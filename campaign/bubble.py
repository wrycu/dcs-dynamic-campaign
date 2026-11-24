"""Bubble planning: decide which aggregated formations must exist in DCS right now.

Pure functions, no I/O. The spawner (campaign/spawner.py) feeds them the campaign state plus
what it knows about players and in-flight commands, and executes the resulting plan.

Rules:
- A formation spawns when any observer comes within its spawn distance, and despawns only once
  every observer is beyond its (larger) despawn distance; the gap stops flicker at the edge.
- Air defences use at least their engagement range plus a margin for both, so a SAM site is
  already up and searching before a player can enter its envelope.
- A spawned formation stays for at least ``min_dwell`` seconds, and while it is "pinned"
  (recently in combat), whatever the distances.
- The number of live DCS vehicles is capped. Formations closest to an observer, relative to
  their radius, win; once over the cap the least relevant spawned ones are despawned.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from .catalog import threat_range
from .flight_spawn import ATTACK_RADIUS_M
from .model import Flight, FlightState, GroundUnit, Pos, Side, SpawnState

NM = 1852.0  # metres per nautical mile


@dataclass(frozen=True)
class Observer:
    """Something the war must be real around: a player aircraft, for now."""
    pos: Pos
    ahead: Pos | None = None  # where it will be after the look-ahead time, if moving


@dataclass
class BubbleConfig:
    """Bubble tuning. Distances are metres here; operators set them in nm (campaign/settings.py)."""
    ground_spawn: float = 20 * NM          # visual/targeting-pod range plus slack
    ground_despawn: float = 25 * NM
    threat_spawn_margin: float = 10 * NM   # added to an air defence's engagement range
    threat_despawn_margin: float = 15 * NM
    min_dwell: float = 90.0           # seconds a fresh spawn is kept regardless of distance
    max_units: int = 300              # live campaign ground vehicles in DCS
    max_spawns_per_tick: int = 4      # spread spawns out to avoid frame hitches
    lookahead_s: float = 60.0         # project observers along their velocity
    # AI flights (campaign/air_bubble.py). Fighters see and shoot far, so these are much larger.
    air_spawn: float = 60 * NM
    air_despawn: float = 80 * NM
    air_min_dwell: float = 120.0      # seconds a flight spawned in DCS stays there at least
    max_flights: int = 12             # campaign AI flights in DCS at once

    def __post_init__(self) -> None:
        if self.ground_spawn <= 0 or self.threat_spawn_margin < 0:
            raise ValueError("spawn distances must be positive")
        if self.ground_despawn < self.ground_spawn:
            raise ValueError("ground despawn distance must be at least the spawn distance")
        if self.threat_despawn_margin < self.threat_spawn_margin:
            raise ValueError("air-defence despawn margin must be at least the spawn margin")
        if self.air_spawn <= 0:
            raise ValueError("air spawn distance must be positive")
        if self.air_despawn < self.air_spawn:
            raise ValueError("air despawn distance must be at least the air spawn distance")


@dataclass
class SpawnPlan:
    spawn: list[str] = field(default_factory=list)
    despawn: list[str] = field(default_factory=list)


def strength(unit: GroundUnit) -> int:
    return sum(n for n in unit.composition.values() if n > 0)


def spawn_radius(unit: GroundUnit, cfg: BubbleConfig) -> float:
    threat = threat_range(unit.composition)
    return max(cfg.ground_spawn, threat + cfg.threat_spawn_margin) if threat else cfg.ground_spawn


def despawn_radius(unit: GroundUnit, cfg: BubbleConfig) -> float:
    threat = threat_range(unit.composition)
    return max(cfg.ground_despawn, threat + cfg.threat_despawn_margin) if threat else cfg.ground_despawn


def observer_from_telemetry(player: dict, lookahead_s: float) -> Observer | None:
    """Build an observer from one ``telemetry.players`` entry (hdg in degrees, spd in m/s)."""
    pos = player.get("pos")
    if not isinstance(pos, dict) or "x" not in pos or "y" not in pos:
        return None
    here = Pos(float(pos["x"]), float(pos["y"]))
    hdg, spd = player.get("hdg"), player.get("spd")
    if not isinstance(hdg, (int, float)) or not isinstance(spd, (int, float)) or spd < 1:
        return Observer(here)
    dist = spd * lookahead_s
    rad = math.radians(hdg)
    # DCS x is north, our y (DCS z) is east; heading is clockwise from north.
    return Observer(here, Pos(here.x + dist * math.cos(rad), here.y + dist * math.sin(rad)))


def _dist(a: Pos, b: Pos) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def nearest(pos: Pos, observers: Iterable[Observer]) -> float:
    best = math.inf
    for o in observers:
        best = min(best, _dist(pos, o.pos))
        if o.ahead is not None:
            best = min(best, _dist(pos, o.ahead))
    return best


def attack_targets(flights: Iterable[Flight], units: Iterable[GroundUnit], side_of: dict[str, Side],
                   cfg: BubbleConfig) -> tuple[set[str], set[str]]:
    """Formations that AI flights in DCS are on their way to attack, or attacking.

    Without this, a target could leave DCS (no player near it) while the flight attacking it is
    in DCS, and the flight would find nothing and go home. Returns ``(keep, spawn)``:
    keep: every enemy formation in the zone of an attack waypoint the flight hasn't left
          behind (the next one, or the one it just passed, since it attacks after reaching it);
    spawn: those of them the flight is within the formation's spawn distance of, i.e. what a
           player there would get.
    side_of: flight id -> its side.
    """
    units = list(units)
    keep: set[str] = set()
    spawn: set[str] = set()
    for f in flights:
        if f.spawn is not SpawnState.SPAWNED or f.state in (FlightState.LANDED, FlightState.LOST):
            continue
        side = side_of.get(f.id)
        for wp in f.waypoints[max(f.next_wp - 1, 0):]:
            radius = wp.params.get("radius_m", ATTACK_RADIUS_M.get(wp.action or ""))
            if radius is None:
                continue
            for u in units:
                if u.side is side or strength(u) == 0 or _dist(u.pos, wp.pos) > radius:
                    continue
                keep.add(u.id)
                if f.pos is not None and _dist(u.pos, f.pos) <= spawn_radius(u, cfg):
                    spawn.add(u.id)
    return keep, spawn


def plan_ground(
    units: Iterable[GroundUnit],
    observers: list[Observer],
    now: float,
    cfg: BubbleConfig,
    *,
    spawned_at: dict[str, float] | None = None,
    busy: set[str] | frozenset[str] = frozenset(),
    pinned: set[str] | frozenset[str] = frozenset(),
    blocked: set[str] | frozenset[str] = frozenset(),
    forced: set[str] | frozenset[str] = frozenset(),
) -> SpawnPlan:
    """Return which formations to spawn and despawn.

    spawned_at: unit id -> time it was spawned (for min_dwell)
    busy:       units with a spawn/despawn command in flight; left alone this tick
    pinned:     units that must not be despawned (recently in combat)
    blocked:    units that may not be spawned yet (backing off after a failure)
    forced:     units to spawn (and keep) with no player near, e.g. an AI flight's target
    """
    spawned_at = spawned_at or {}
    keep: list[tuple[float, GroundUnit]] = []     # (relevance, unit); lower = more relevant
    want: list[tuple[float, GroundUnit]] = []
    plan = SpawnPlan()
    used = 0

    for u in units:
        size = strength(u)
        if u.id in busy:
            used += size  # an in-flight spawn or despawn still holds its budget
            continue
        if size == 0:
            if u.spawn is SpawnState.SPAWNED:
                plan.despawn.append(u.id)
            continue
        dist = nearest(u.pos, observers)
        rel = 0.0 if u.id in forced else dist / spawn_radius(u, cfg)
        if u.spawn is SpawnState.SPAWNED:
            young = now - spawned_at.get(u.id, -math.inf) < cfg.min_dwell
            if dist <= despawn_radius(u, cfg) or young or u.id in pinned or u.id in forced:
                keep.append((rel, u))
            else:
                plan.despawn.append(u.id)
        elif rel <= 1.0 and u.id not in blocked:
            want.append((rel, u))

    keep.sort(key=lambda p: p[0])
    for rel, u in keep:
        used += strength(u)
    # Over the cap (e.g. after many spawns or a lowered cap): shed the least relevant first,
    # but never something pinned or still within its dwell time.
    for rel, u in reversed(keep):
        if used <= cfg.max_units:
            break
        if u.id in pinned or u.id in forced or now - spawned_at.get(u.id, -math.inf) < cfg.min_dwell:
            continue
        plan.despawn.append(u.id)
        used -= strength(u)

    want.sort(key=lambda p: p[0])
    for rel, u in want:
        if len(plan.spawn) >= cfg.max_spawns_per_tick:
            break
        size = strength(u)
        if used + size > cfg.max_units:
            continue  # a smaller formation further down may still fit
        plan.spawn.append(u.id)
        used += size
    return plan
