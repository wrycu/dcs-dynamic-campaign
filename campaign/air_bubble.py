"""The flight bubble: which AI flights exist in DCS, and moving the rest along their routes.

Rules:
- An airborne flight is spawned (air start, where it is) once a player is within ``air_spawn``,
  and taken back out of DCS once every player is beyond ``air_despawn``, it has been in DCS for
  ``air_min_dwell`` and it hasn't been in a fight for ``PIN_S``.
- A flight due to take off (``takeoff_time`` reached on the campaign clock) starts in DCS on the
  ramp or runway if a player is within ``air_spawn`` of its base; otherwise it takes off without
  DCS. Flights with an AIR start, or no base, begin at their first waypoint.
- Flights that aren't in DCS fly their route at ``CRUISE_MS``, orbit for the ORBIT waypoint's
  duration, and are home when they reach LAND or EXIT.
- Flights on the ground in DCS (taxiing, landed) are left to ``AirSpawner``: it recovers them.
- At most ``max_flights`` campaign flights are in DCS; the closest to a player spawn first.

Time is DCS model time from telemetry, so time acceleration speeds the whole war up and a paused
mission stops it. The campaign clock (``CampaignState.clock``) advances by the same amount.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .air import AirSpawner
from .bubble import BubbleConfig, Observer, nearest, observer_from_telemetry
from .flight_spawn import CRUISE_MS, ORBIT_DEFAULT_S, Takeoff
from .model import CampaignState, Flight, FlightState, Pos, SpawnState, Squadron, StartMode
from .protocol import PREFIX_FLIGHT, as_list, parse_dcs_name

log = logging.getLogger(__name__)

MAX_STEP_S = 120.0   # largest model-time jump applied at once (a stalled or paused mission)
PIN_S = 300.0        # a flight stays in DCS this long after it kills, is hit or loses an aircraft
FAIL_BACKOFF_S = 60.0
AIRBORNE = (FlightState.ENROUTE, FlightState.ON_STATION, FlightState.EGRESS)


def fly(f: Flight, t0: float, t1: float, speed: float = CRUISE_MS) -> str | None:
    """Move an aggregated flight along its route from campaign time ``t0`` to ``t1``.

    Returns "LAND" or "EXIT" when the flight reaches such a waypoint (it's home), else None.
    """
    t = t0
    while f.next_wp < len(f.waypoints):
        wp = f.waypoints[f.next_wp]
        if f.pos is None:
            f.pos, f.alt_m = wp.pos, wp.alt_m
        if f.hold_until is not None:  # orbiting at wp
            if f.hold_until > t1:
                return None
            t = max(t, f.hold_until)
            f.hold_until = None
            f.next_wp += 1
            continue
        dx, dy = wp.pos.x - f.pos.x, wp.pos.y - f.pos.y
        dist = math.hypot(dx, dy)
        if dist > 0:
            f.hdg = math.degrees(math.atan2(dy, dx)) % 360
        f.speed_ms = speed
        reach = speed * (t1 - t)
        if reach < dist:
            k = reach / dist
            f.pos = Pos(f.pos.x + dx * k, f.pos.y + dy * k)
            f.alt_m = (f.alt_m if f.alt_m is not None else wp.alt_m) * (1 - k) + wp.alt_m * k
            return None
        t += dist / speed
        f.pos, f.alt_m = wp.pos, wp.alt_m
        if wp.action == "ORBIT":
            f.hold_until = t + float(wp.params.get("duration_s", ORBIT_DEFAULT_S))
            continue
        f.next_wp += 1
        if wp.action in ("LAND", "EXIT"):
            return wp.action
    return "EXIT"  # ran out of route: treat as gone home


@dataclass
class AirPlan:
    launch_dcs: list[str] = field(default_factory=list)     # due flights to start on the ramp/runway
    launch_virtual: list[str] = field(default_factory=list)  # due flights to take off without DCS
    spawn: list[str] = field(default_factory=list)           # airborne flights to put into DCS
    aggregate: list[str] = field(default_factory=list)       # airborne flights to take out of DCS


class AirBubble:
    def __init__(self, state: CampaignState, air: AirSpawner, cfg: BubbleConfig | None = None, *,
                 base_pos: Callable[[Squadron], Pos | None],
                 takeoff_for: Callable[[Flight], Awaitable[Takeoff]]) -> None:
        """``base_pos`` gives a squadron's airbase position (None: off-map or unknown).
        ``takeoff_for`` gets the DCS takeoff details (airbase id, parking) for a ramp/runway start."""
        self.state = state
        self.air = air
        self.cfg = cfg or BubbleConfig()
        self.base_pos = base_pos
        self.takeoff_for = takeoff_for
        self.observers: list[Observer] = []
        self.last_t: float | None = None
        self.spawned_at: dict[str, float] = {}    # flight id -> campaign clock it entered DCS
        self.pinned_until: dict[str, float] = {}
        self.backoff_until: dict[str, float] = {}
        self.busy: set[str] = set()

    # ---------- save / load (all times are on the campaign clock, which is saved) ----------

    def export(self) -> dict[str, Any]:
        return {"spawned_at": self.spawned_at, "pinned_until": self.pinned_until}

    def restore(self, data: dict[str, Any]) -> None:
        self.spawned_at = {k: float(v) for k, v in (data.get("spawned_at") or {}).items()}
        self.pinned_until = {k: float(v) for k, v in (data.get("pinned_until") or {}).items()}

    # ---------- inputs ----------

    def on_telemetry(self, t: float | None, data: dict[str, Any]) -> None:
        """Players from telemetry, and the campaign clock from its DCS model time ``t``."""
        self.observers = [o for o in (observer_from_telemetry(p, self.cfg.lookahead_s)
                                      for p in as_list(data.get("players")) if isinstance(p, dict)) if o]
        if t is not None:
            self.advance_to(float(t))

    def on_new_run(self) -> None:
        """The mission restarted: its model time starts again from 0."""
        self.last_t = None
        self.busy.clear()

    def advance_to(self, t: float) -> None:
        if self.last_t is None or t < self.last_t:
            self.last_t = t
            return
        self.advance(min(t - self.last_t, MAX_STEP_S))
        self.last_t = t

    def advance(self, dt: float) -> None:
        """Run the war for ``dt`` seconds: the campaign clock, and every flight not in DCS."""
        t0 = self.state.clock
        self.state.clock = t1 = t0 + dt
        for f in list(self.state.flights.values()):
            if f.spawn is SpawnState.AGGREGATED and f.state in AIRBORNE and f.aircraft > 0:
                arrived = fly(f, t0, t1)
                if arrived:
                    self.air.arrive_aggregated(f, arrived)

    def on_event(self, data: dict[str, Any]) -> None:
        names = []
        if data.get("kind") == "unit_killed":
            names.append((data.get("unit") or {}).get("name"))
        elif data.get("kind") in ("kill", "hit"):
            names += [(data.get(k) or {}).get("name") for k in ("killer", "victim", "target", "shooter")]
        for name in names:
            parsed = parse_dcs_name(name)
            if parsed and parsed.prefix == PREFIX_FLIGHT:
                self.pinned_until[parsed.model_id] = self.state.clock + PIN_S

    # ---------- planning ----------

    def plan(self) -> AirPlan:
        now = self.state.clock
        cfg = self.cfg
        plan = AirPlan()
        in_dcs = sum(1 for f in self.state.flights.values() if f.spawn is SpawnState.SPAWNED)
        wanted: list[tuple[float, str]] = []
        for f in self.state.flights.values():
            if f.id in self.busy or self.backoff_until.get(f.id, -math.inf) > now or f.aircraft <= 0:
                continue
            if f.spawn is SpawnState.AGGREGATED and f.state is FlightState.PLANNED and f.takeoff_time <= now:
                base = self.base_pos(self.state.squadrons[f.squadron_id])
                if f.start is not StartMode.AIR and base is not None and nearest(base, self.observers) <= cfg.air_spawn:
                    wanted.append((nearest(base, self.observers), f.id))
                else:
                    plan.launch_virtual.append(f.id)
            elif f.spawn is SpawnState.AGGREGATED and f.state in AIRBORNE and f.pos is not None:
                d = nearest(f.pos, self.observers)
                if d <= cfg.air_spawn:
                    wanted.append((d, f.id))
            elif f.spawn is SpawnState.SPAWNED and f.state in AIRBORNE and f.pos is not None:
                live = self.air.live.get(f.id)
                if live is None or not live.took_off:
                    continue
                if (nearest(f.pos, self.observers) > cfg.air_despawn
                        and now - self.spawned_at.get(f.id, -math.inf) >= cfg.air_min_dwell
                        and self.pinned_until.get(f.id, -math.inf) <= now):
                    plan.aggregate.append(f.id)
        room = max(0, cfg.max_flights - in_dcs + len(plan.aggregate))
        for _, fid in sorted(wanted)[:room]:
            f = self.state.flights[fid]
            (plan.launch_dcs if f.state is FlightState.PLANNED else plan.spawn).append(fid)
        # Due flights that can't get into DCS (cap reached) still take off, without DCS.
        for _, fid in sorted(wanted)[room:]:
            if self.state.flights[fid].state is FlightState.PLANNED:
                plan.launch_virtual.append(fid)
        return plan

    async def tick(self) -> AirPlan:
        plan = self.plan()
        for fid in plan.launch_virtual:
            f = self.state.flights[fid]
            try:
                self.air.launch_aggregated(f, self.base_pos(self.state.squadrons[f.squadron_id]))
            except ValueError as e:
                log.warning("%s can't launch: %s", fid, e)
                self.backoff_until[fid] = self.state.clock + FAIL_BACKOFF_S
        for fid in plan.aggregate:
            await self._run(fid, self._aggregate)
        for fid in plan.launch_dcs + plan.spawn:
            await self._run(fid, self._spawn)
        return plan

    async def _run(self, fid: str, op: Callable[[Flight], Awaitable[None]]) -> None:
        f = self.state.flights[fid]
        self.busy.add(fid)
        try:
            await op(f)
        except Exception as e:  # noqa: BLE001 - back off and try again later
            log.warning("%s: %s failed: %s", fid, op.__name__.strip("_"), e)
            self.backoff_until[fid] = self.state.clock + FAIL_BACKOFF_S
        finally:
            self.busy.discard(fid)

    async def _spawn(self, f: Flight) -> None:
        takeoff = None
        if f.state is FlightState.PLANNED and f.start is not StartMode.AIR:
            takeoff = await self.takeoff_for(f)
        await self.air.spawn(f, takeoff=takeoff)
        self.spawned_at[f.id] = self.state.clock

    async def _aggregate(self, f: Flight) -> None:
        await self.air.aggregate(f)
        self.spawned_at.pop(f.id, None)
