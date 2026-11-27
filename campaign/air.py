"""Keep DCS in step with campaign flights: spawn, track, recover, aggregate.

The air spawner owns a ``Flight``'s DCS projection:
- ``spawn`` puts it in DCS (a ramp, runway or air start). Its aircraft leave the squadron the
  first time it launches.
- events move it through TAXI -> ENROUTE -> LANDED, and losses reduce ``Flight.aircraft``
  (once per DCS unit name per spawn);
- once every surviving aircraft has landed and shut down, it is despawned and the survivors go
  back to the squadron ("recovered");
- ``aggregate`` takes an airborne flight out of DCS, keeping position, altitude, heading, speed and
  fuel, so a later ``spawn`` (air start) resumes it where it was;
- reaching an ``EXIT`` waypoint counts as getting home (bases off the map);
- watchdogs abort a ramp flight that never takes off (taxi deadlocks) and recover a landed
  flight that never shuts down.

Talks to DCS through an injected ``call(op, data)`` coroutine, like the ground spawner.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .catalog import flight_cruise_ms
from .flight_spawn import ATTACK_RADIUS_M, ORBIT_DEFAULT_S, Takeoff, build_flight_spawn, flight_number_hint
from .model import CampaignState, Flight, FlightState, Pos, Side, SpawnState, StartMode
from .protocol import PREFIX_FLIGHT, Hello, Op, as_list, group_name, parse_dcs_name

log = logging.getLogger(__name__)

Call = Callable[[str, dict[str, Any]], Awaitable[Any]]
Notify = Callable[[Flight, str], None]


@dataclass
class AirConfig:
    taxi_timeout_s: float = 900.0      # ramp/runway flight that hasn't taken off by then is aborted
    shutdown_timeout_s: float = 600.0  # landed flight that hasn't shut down by then is recovered anyway
    wp_reached_m: float = 9_000.0      # a waypoint counts as passed within this distance
    missing_ticks: int = 2             # telemetry reports without the group before it counts as gone


@dataclass
class _Live:
    """Per-spawn bookkeeping for a flight that is in DCS."""
    names: list[str]
    spawned_at: float
    started_at: float | None          # when it was told to start (None while held uncontrolled)
    dead: set[str]
    landed: set[str]
    shutdown: set[str]
    landed_at: float | None = None
    took_off: bool = False
    missing: int = 0
    busy: bool = False
    number: int = 0                   # callsign/board-number flight number


class AirSpawner:
    def __init__(self, state: CampaignState, call: Call, cfg: AirConfig | None = None,
                 clock: Callable[[], float] = time.monotonic, notify: Notify | None = None) -> None:
        self.state = state
        self.call = call
        self.cfg = cfg or AirConfig()
        self.clock = clock
        self.notify = notify
        self.live: dict[str, _Live] = {}
        self.orphans: set[str] = set()

    # ---------- save / load ----------

    def export(self) -> dict[str, Any]:
        """Bookkeeping for flights in DCS, for the save file. Times are stored as ages."""
        now = self.clock()
        age = lambda t: None if t is None else now - t  # noqa: E731
        return {fid: {"names": live.names, "age": age(live.spawned_at), "started_age": age(live.started_at),
                      "landed_age": age(live.landed_at), "dead": sorted(live.dead),
                      "landed": sorted(live.landed), "shutdown": sorted(live.shutdown),
                      "took_off": live.took_off, "number": live.number}
                for fid, live in self.live.items()}

    def restore(self, data: dict[str, Any]) -> None:
        """Undo ``export`` after a load, before the mission reconnects."""
        now = self.clock()
        at = lambda a: None if a is None else now - float(a)  # noqa: E731
        self.live = {}
        for fid, d in data.items():
            f = self.state.flights.get(fid)
            if f is None or f.spawn is not SpawnState.SPAWNED:
                continue
            spawned = at(d.get("age"))
            self.live[fid] = _Live(names=list(d["names"]), spawned_at=now if spawned is None else spawned,
                                   started_at=at(d.get("started_age")), dead=set(d.get("dead", ())),
                                   landed=set(d.get("landed", ())), shutdown=set(d.get("shutdown", ())),
                                   landed_at=at(d.get("landed_age")), took_off=bool(d.get("took_off")),
                                   number=int(d.get("number", 0)))
        # A flight saved as spawned without bookkeeping (shouldn't happen) is adopted with what we know.
        for f in self.state.flights.values():
            if f.spawn is SpawnState.SPAWNED and f.id not in self.live:
                names = [f"{f.dcs_group}#{i}" for i in range(1, f.aircraft + 1)]
                self.live[f.id] = _Live(names=names, spawned_at=now, started_at=now, dead=set(), landed=set(),
                                        shutdown=set(), took_off=f.state is not FlightState.TAXI)

    # ---------- commands ----------

    async def spawn(self, flight: Flight, *, takeoff: Takeoff | None = None, uncontrolled: bool = False) -> None:
        if flight.spawn is SpawnState.SPAWNED:
            raise ValueError(f"{flight.id} is already spawned")
        sq = self.state.squadrons[flight.squadron_id]
        first_launch = flight.state is FlightState.PLANNED
        if first_launch and sq.aircraft_ready < flight.aircraft:
            raise ValueError(f"{sq.name} has {sq.aircraft_ready} aircraft ready, {flight.id} needs {flight.aircraft}")
        # Its own number (given when planned: its callsign); older saves' flights get a free one.
        number = flight.number or self._free_number(flight, sq.side)
        orbit_left = None
        if flight.hold_until is not None and flight.start is StartMode.AIR:
            orbit_left = max(0.0, flight.hold_until - self.state.clock)
        spec = build_flight_spawn(flight, sq.aircraft_type, sq.side, takeoff=takeoff, uncontrolled=uncontrolled,
                                  number=number, orbit_left_s=orbit_left,
                                  speed_ms=flight_cruise_ms(self.state, flight))
        await self.call(Op.SPAWN_GROUP, spec.data)
        now = self.clock()
        if first_launch:
            sq.aircraft_ready -= flight.aircraft
        flight.spawn = SpawnState.SPAWNED
        flight.dcs_group = spec.data["group"]["name"]
        airborne = flight.start is StartMode.AIR
        flight.state = FlightState.ENROUTE if airborne else FlightState.TAXI
        self.live[flight.id] = _Live(names=spec.unit_names, spawned_at=now,
                                     started_at=None if uncontrolled else now,
                                     dead=set(), landed=set(), shutdown=set(), took_off=airborne,
                                     number=number)
        self._notify(flight, "held on the ramp" if uncontrolled else "spawned")

    async def start(self, flight: Flight) -> None:
        """Start an uncontrolled (held) flight: it starts up, taxis and flies its route."""
        live = self._live(flight)
        await self.call(Op.SET_COMMAND, {"group": flight.dcs_group, "command": {"id": "Start", "params": {}}})
        live.started_at = self.clock()
        self._notify(flight, "starting up")

    async def aggregate(self, flight: Flight) -> None:
        """Take an airborne flight out of DCS, keeping where it was and how much fuel it had."""
        live = self._live(flight)
        res = await self._despawn(flight, live) or {}
        if res.get("gone"):
            self._gone(flight)
            return
        flight.aircraft = len(as_list(res.get("alive"))) or flight.aircraft
        pos = res.get("pos")
        if isinstance(pos, dict):
            flight.pos = Pos(float(pos["x"]), float(pos["y"]))
        for attr, key in (("alt_m", "alt"), ("hdg", "hdg"), ("speed_ms", "spd"), ("fuel", "fuel")):
            if isinstance(res.get(key), (int, float)):
                setattr(flight, attr, float(res[key]))
        flight.start = StartMode.AIR
        self._detach(flight)
        self._notify(flight, "aggregated in the air")

    def launch_aggregated(self, flight: Flight, pos: Pos | None) -> None:
        """Take off without DCS: the flight is airborne at ``pos`` (its base) or its first waypoint."""
        if flight.state is not FlightState.PLANNED or flight.spawn is SpawnState.SPAWNED:
            raise ValueError(f"{flight.id} is not waiting to take off")
        sq = self.state.squadrons[flight.squadron_id]
        if sq.aircraft_ready < flight.aircraft:
            raise ValueError(f"{sq.name} has {sq.aircraft_ready} aircraft ready, {flight.id} needs {flight.aircraft}")
        sq.aircraft_ready -= flight.aircraft
        flight.start = StartMode.AIR
        flight.state = FlightState.ENROUTE
        flight.pos = pos or flight.waypoints[flight.next_wp].pos
        flight.alt_m = flight.waypoints[flight.next_wp].alt_m
        self._notify(flight, "took off (not in DCS)")

    def arrive_aggregated(self, flight: Flight, action: str) -> None:
        """A flight that isn't in DCS reached its LAND or EXIT waypoint: its aircraft are home."""
        self.state.squadrons[flight.squadron_id].aircraft_ready += flight.aircraft
        flight.state = FlightState.LANDED
        self._notify(flight, f"{'landed' if action == 'LAND' else 'left the map'} (not in DCS): "
                             f"{flight.aircraft} aircraft back to the squadron")

    async def recover(self, flight: Flight, why: str = "recovered") -> None:
        """Despawn a flight that is home (landed, or left the map) and return its aircraft."""
        live = self._live(flight)
        res = await self._despawn(flight, live) or {}
        survivors = 0 if res.get("gone") else len(as_list(res.get("alive")))
        self.state.squadrons[flight.squadron_id].aircraft_ready += survivors
        flight.aircraft = survivors
        flight.state = FlightState.LANDED if survivors else FlightState.LOST
        self._detach(flight)
        self._notify(flight, f"{why}: {survivors} aircraft back to the squadron")

    # ---------- inputs ----------

    def on_hello(self, hello: Hello, new_run: bool) -> None:
        live_groups = set(hello.spawned)
        for f in self.state.flights.values():
            if f.spawn is not SpawnState.SPAWNED:
                continue
            if new_run or f.dcs_group not in live_groups:
                self._vanished(f)
        known = {f.dcs_group for f in self.state.flights.values() if f.spawn is SpawnState.SPAWNED}
        for name in live_groups - known:
            parsed = parse_dcs_name(name)
            if parsed and parsed.prefix == PREFIX_FLIGHT:
                self.orphans.add(name)
        for live in self.live.values():
            live.busy = False

    def on_telemetry(self, data: dict[str, Any]) -> list[Flight]:
        """Update spawned flights from telemetry. Returns flights that reached an EXIT waypoint."""
        seen: set[str] = set()
        exits: list[Flight] = []
        for g in as_list(data.get("groups")):
            if not isinstance(g, dict):
                continue
            parsed = parse_dcs_name(g.get("id"))
            if not parsed or parsed.prefix != PREFIX_FLIGHT:
                continue
            f = self.state.flights.get(parsed.model_id)
            if f is None or f.spawn is not SpawnState.SPAWNED:
                continue
            seen.add(f.id)
            pos = g.get("pos")
            if isinstance(pos, dict):
                f.pos = Pos(float(pos["x"]), float(pos["y"]))
            for attr, key in (("alt_m", "alt"), ("hdg", "hdg"), ("speed_ms", "spd"), ("fuel", "fuel")):
                if isinstance(g.get(key), (int, float)):
                    setattr(f, attr, float(g[key]))
            if f.pos is not None and g.get("in_air", True):
                self._track_route(f, exits)
        for fid, live in list(self.live.items()):  # _vanished() removes entries
            f = self.state.flights[fid]
            if fid in seen or live.busy:
                live.missing = 0
                continue
            live.missing += 1
            if live.missing >= self.cfg.missing_ticks:
                log.info("%s vanished from DCS telemetry", fid)
                self._vanished(f)
        return exits

    def _track_route(self, f: Flight, exits: list[Flight]) -> None:
        """Advance ``next_wp`` from a telemetry position.

        Near any upcoming waypoint, everything before it is passed (telemetry is sparse, so a
        waypoint can be skipped between reports). An ORBIT waypoint stays the next one until its
        time is up, measured on the campaign clock from when the flight first got there, so a
        flight taken out of DCS mid-orbit comes back for the rest of it. An attack waypoint (CAS,
        SEAD, BOMB) is never skipped: it is passed only by reaching it. A LAND waypoint is never
        passed by flying near it, only by landing: a flight overhead or in the pattern hasn't
        landed, and taken out of DCS there it must still fly to the field and land.
        """
        now = self.state.clock
        for j in range(f.next_wp, len(f.waypoints)):
            wp = f.waypoints[j]
            if math.dist((f.pos.x, f.pos.y), (wp.pos.x, wp.pos.y)) > self.cfg.wp_reached_m:
                if wp.action in ATTACK_RADIUS_M:
                    break   # never skip an attack: a later point near the route doesn't mean it's done
                continue
            if wp.action == "LAND":
                f.next_wp, f.hold_until = j, None
            elif wp.action == "ORBIT":
                if f.next_wp != j or f.hold_until is None:
                    f.next_wp, f.hold_until = j, now + float(wp.params.get("duration_s", ORBIT_DEFAULT_S))
                elif now >= f.hold_until:
                    f.next_wp, f.hold_until = j + 1, None
            else:
                f.next_wp, f.hold_until = j + 1, None
                if wp.action == "EXIT":
                    exits.append(f)
            return
        if f.hold_until is not None and now >= f.hold_until:
            # Orbit over and the flight has left the area for its next leg.
            f.next_wp, f.hold_until = f.next_wp + 1, None

    def on_event(self, data: dict[str, Any]) -> None:
        kind = data.get("kind")
        unit = data.get("unit") or {}
        name = unit.get("name")
        parsed = parse_dcs_name(name)
        if not parsed or parsed.prefix != PREFIX_FLIGHT or parsed.unit_index is None:
            return
        f = self.state.flights.get(parsed.model_id)
        live = self.live.get(parsed.model_id)
        if f is None or live is None:
            return
        if kind == "takeoff":
            if not live.took_off:
                live.took_off = True
                f.state = FlightState.ENROUTE
                self._notify(f, f"took off from {data.get('place') or 'the airbase'}")
        elif kind == "land":
            live.landed.add(name)
            if self._all(live, live.landed) and f.state is not FlightState.LANDED:
                f.state = FlightState.LANDED
                live.landed_at = self.clock()
                self._notify(f, f"landed at {data.get('place') or 'an airbase'}")
        elif kind == "engine_shutdown":
            live.shutdown.add(name)
        elif kind == "unit_killed":
            if name in live.dead:
                return
            live.dead.add(name)
            f.aircraft = max(0, f.aircraft - 1)
            self._notify(f, f"lost {unit.get('type') or 'an aircraft'}: {f.aircraft} left")
            if f.aircraft == 0:
                f.state = FlightState.LOST
                self._detach(f)  # DCS removes a group whose last unit dies
                self._notify(f, "destroyed")

    # ---------- loop ----------

    def due(self) -> list[tuple[Flight, str]]:
        """Flights whose watchdog or landing calls for action now: (flight, "recover" | "abort")."""
        now = self.clock()
        out: list[tuple[Flight, str]] = []
        for fid, live in self.live.items():
            f = self.state.flights[fid]
            if live.busy:
                continue
            if f.state is FlightState.LANDED and live.landed_at is not None:
                if self._all(live, live.shutdown) or now - live.landed_at >= self.cfg.shutdown_timeout_s:
                    out.append((f, "recover"))
            elif not live.took_off and live.started_at is not None \
                    and now - live.started_at >= self.cfg.taxi_timeout_s:
                out.append((f, "abort"))
        return out

    async def tick(self, exits: list[Flight] | None = None) -> None:
        for name in list(self.orphans):
            try:
                await self.call(Op.DESPAWN_GROUP, {"group": name})
                self.orphans.discard(name)
            except Exception as e:  # noqa: BLE001
                log.warning("removing orphan %s failed: %s", name, e)
        for f in exits or []:
            if f.id in self.live:
                await self._try(self.recover(f, "left the map"), f)
        for f, what in self.due():
            if what == "recover":
                await self._try(self.recover(f), f)
            else:
                log.warning("%s never took off; aborting (taxi deadlock?)", f.id)
                await self._try(self.recover(f, "aborted, never took off"), f)

    # ---------- internals ----------

    def _live(self, flight: Flight) -> _Live:
        live = self.live.get(flight.id)
        if live is None or flight.spawn is not SpawnState.SPAWNED:
            raise ValueError(f"{flight.id} is not spawned")
        return live

    def _all(self, live: _Live, names: set[str]) -> bool:
        alive = [n for n in live.names if n not in live.dead]
        return bool(alive) and all(n in names for n in alive)

    async def _despawn(self, flight: Flight, live: _Live) -> dict | None:
        live.busy = True
        try:
            return await self.call(Op.DESPAWN_GROUP, {"group": flight.dcs_group})
        finally:
            live.busy = False

    def _free_number(self, flight: Flight, side: Side) -> int:
        """A flight number (callsign/board number) no other spawned flight on this side is using."""
        used = {live.number for fid, live in self.live.items()
                if self.state.squadrons[self.state.flights[fid].squadron_id].side is side}
        n = flight_number_hint(flight.id)
        for k in range(89):
            cand = (n - 1 + k) % 89 + 1
            if cand not in used:
                return cand
        return n

    async def _try(self, coro, flight: Flight) -> None:
        try:
            await coro
        except Exception as e:  # noqa: BLE001 - retried on the next tick
            log.warning("%s: %s", flight.id, e)

    def _vanished(self, f: Flight) -> None:
        """The group is gone from DCS without us despawning it (mission restart, or removed)."""
        live = self.live.get(f.id)
        if f.state in (FlightState.PLANNED, FlightState.TAXI) or (live and not live.took_off):
            # Never got airborne: the aircraft are still at the base.
            self.state.squadrons[f.squadron_id].aircraft_ready += f.aircraft
            f.state = FlightState.PLANNED
        elif f.state is FlightState.LANDED:
            self.state.squadrons[f.squadron_id].aircraft_ready += f.aircraft
        else:
            f.start = StartMode.AIR  # resume in the air where it was last seen
        early = live is not None and not live.took_off and self.clock() - live.spawned_at < 60
        self._detach(f)
        if early:
            sq = self.state.squadrons[f.squadron_id]
            self._notify(f, f"removed by DCS right after spawning; check the airbase warehouse has "
                            f"{f.aircraft} {sq.aircraft_type}")
        else:
            self._notify(f, "no longer in DCS")

    def _gone(self, f: Flight) -> None:
        f.aircraft = 0
        f.state = FlightState.LOST
        self._detach(f)

    def _detach(self, f: Flight) -> None:
        f.spawn = SpawnState.AGGREGATED
        f.dcs_group = None
        self.live.pop(f.id, None)

    def _notify(self, f: Flight, what: str) -> None:
        log.info("%s %s", f.id, what)
        if self.notify:
            try:
                self.notify(f, what)
            except Exception:  # noqa: BLE001
                log.exception("notify failed for %s", f.id)
