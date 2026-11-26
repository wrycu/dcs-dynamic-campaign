"""The air war for flights that aren't in DCS (docs/planner.md, section 4).

A flight in DCS fights in DCS. A flight that isn't is moved along its route by the air bubble
(``air_bubble.fly``); this resolves what happens to it, with the P7 fight model (campaign/combat.py):

- **On the way:** each leg it flies through an enemy SAM site's range is a chance of being shot
  down (``sam_losses``), lower once a SEAD flight of its package has reached its target.
- **At its target**, by waypoint action:
  - ``CAS``: the biggest enemy formation within the zone; its own air defence fires first
    (``shorad_losses``), then the flight kills vehicles (``cas_kills``).
  - ``SEAD``: the nearest enemy SAM site in the zone; a hit takes out one of its radars, and the
    package's other flights are safer for ``SUPPRESS_S``.
  - ``BOMB`` on an airbase: runway damage (fewer sorties until repaired) and parked aircraft.
- **Air to air:** a fighter flight (CAP, escort, intercept, sweep) within ``ENGAGE_M`` of an enemy
  flight fights it (``air_battle``). An escort or CAP near the flight that's jumped takes the fight
  instead. A strike flight that's jumped jettisons and goes home; fighters that lose half go home.
- **Red's intercepts:** a defensive side scrambles fighters at enemy flights over or near its
  ground that it can catch weak: no fighters with them (docs/planner.md, section 2).

Losses come off ``Flight.aircraft`` (the squadron already gave them up at launch) and off ground
formations' compositions. Each roll uses the campaign's seed with the flight and waypoint, so a
saved war goes on the same way.
"""
from __future__ import annotations

import logging
import math
import random
from collections import defaultdict
from typing import Any, Callable

from . import combat
from .bubble import strength
from .combat import AirSide
from .model import (CampaignState, Flight, FlightState, GroundRole, GroundUnit, ObjectiveType, Pos, Side,
                    SpawnState, StartMode, Theater, Waypoint)

log = logging.getLogger(__name__)

NM = 1852.0
ENGAGE_M = 25_000           # fighters find and fight enemy flights this close
COOLDOWN_S = 900            # a flight fights at most once in this long
SUPPRESS_S = 1800           # a package is safer from SAMs this long after its SEAD flight attacks
CAS_COVER_TOWN = 2.0        # formations standing in a town are harder to find and kill
RUNWAY_REPAIR_H = 0.04      # runway damage repaired per hour (about a day from closed)
STRIKE_RUNWAY = 0.15        # runway damage per striking aircraft
STRIKE_PARKED = 0.015       # chance per striking aircraft to destroy each parked aircraft (most are sheltered)
CANCEL_LATE_S = 1200        # a planned flight that couldn't take off by then is cancelled
KEEP_DONE_S = 6 * 3600      # finished flights stay in the state this long (for status and stats)
SCRAMBLE_REACH_M = 150_000  # an intercept is launched only at flights this close to a fighter base
OWN_SKY_M = 40_000          # "over our ground": this close to an objective we hold
SCRAMBLE_STEP_S = 300

AIRBORNE = (FlightState.ENROUTE, FlightState.ON_STATION, FlightState.EGRESS)
FIGHTER_MISSIONS = {"CAP", "ESCORT", "INTERCEPT", "SWEEP"}
ATTACK_ACTIONS = {"CAS", "SEAD", "BOMB"}
EVADE_CHANCE = 0.6          # a defensive side's fighters jumped by stronger ones get away this often

Notify = Callable[[str], None]


def seg_dist(p: Pos, a: Pos, b: Pos) -> float:
    dx, dy = b.x - a.x, b.y - a.y
    L = dx * dx + dy * dy
    k = 0.0 if L == 0 else max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / L))
    return math.hypot(p.x - (a.x + dx * k), p.y - (a.y + dy * k))


def _d(a: Pos, b: Pos) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _enemy(side: Side) -> Side:
    return Side.RED if side is Side.BLUE else Side.BLUE


class AirWar:
    """A ``server.System`` (and part of the offline runner, campaign/war.py)."""
    name = "air_war"

    def __init__(self, state: CampaignState, theater: Theater, postures: dict[Side, str], *, seed: int = 1,
                 notify: Notify | None = None, base_pos: Callable[[str], Pos | None] | None = None) -> None:
        """``postures``: each side's air posture ("offensive", "defensive" or "cautious")."""
        self.state = state
        self.objs = theater.objectives
        self.postures = postures
        self.seed = seed
        self.notify = notify or (lambda text: None)
        self.base_pos = base_pos or (lambda oid: self.objs[oid].pos if oid in self.objs else None)
        self.last_pos: dict[str, Pos] = {}          # flight id -> where its current leg started
        self.exposed: dict[str, set[str]] = defaultdict(set)   # flight id -> SAM sites already faced
        self.suppressed: dict[str, float] = {}      # package id -> safer from SAMs until
        self.cooldown: dict[str, float] = {}        # flight id -> can't fight again before
        self.hunted: set[str] = set()               # enemy flights an intercept is already after
        self.losses: list[tuple[float, Side, str]] = []   # (time, side, cause) per aircraft
        self.killed: dict[Side, int] = {Side.BLUE: 0, Side.RED: 0}   # vehicles killed by each side's air
        self.done_at: dict[str, float] = {}         # finished flight id -> when it was first seen finished
        self.next_scramble = 0.0
        self.counter = 0

    # ---------- System ----------

    def export(self) -> dict[str, Any]:
        return {"last_pos": {k: [p.x, p.y] for k, p in self.last_pos.items()},
                "exposed": {k: sorted(v) for k, v in self.exposed.items()},
                "suppressed": self.suppressed, "cooldown": self.cooldown, "hunted": sorted(self.hunted),
                "losses": [[t, s.name, c] for t, s, c in self.losses],
                "killed": {s.name: n for s, n in self.killed.items()},
                "done_at": self.done_at, "next_scramble": self.next_scramble, "counter": self.counter}

    def restore(self, data: dict[str, Any]) -> None:
        self.last_pos = {k: Pos(*v) for k, v in (data.get("last_pos") or {}).items()}
        self.exposed = defaultdict(set, {k: set(v) for k, v in (data.get("exposed") or {}).items()})
        self.suppressed = {k: float(v) for k, v in (data.get("suppressed") or {}).items()}
        self.cooldown = {k: float(v) for k, v in (data.get("cooldown") or {}).items()}
        self.hunted = set(data.get("hunted") or [])
        self.losses = [(float(t), Side[s], c) for t, s, c in data.get("losses") or []]
        self.killed.update({Side[k]: int(v) for k, v in (data.get("killed") or {}).items()})
        self.done_at = {k: float(v) for k, v in (data.get("done_at") or {}).items()}
        self.next_scramble = float(data.get("next_scramble", 0.0))
        self.counter = int(data.get("counter", 0))

    def on_hello(self, hello, new_run: bool) -> None:
        pass

    def on_event(self, data: dict[str, Any]) -> None:
        pass

    async def tick(self) -> None:
        pass

    def advance(self, t0: float, t1: float) -> None:
        if math.floor(t1 / 3600) > math.floor(t0 / 3600):
            self._hour()
        self._engagements(t1)
        if t1 >= self.next_scramble:
            self.next_scramble = t1 + SCRAMBLE_STEP_S
            for side, posture in self.postures.items():
                if posture in ("defensive", "offensive"):
                    self._scramble(side, t1)
        self._tidy(t1)

    # ---------- helpers ----------

    def types(self, f: Flight) -> list[str]:
        return [self.state.squadrons[f.squadron_id].aircraft_type] * max(0, f.aircraft)

    def side_of(self, f: Flight) -> Side:
        return self.state.squadrons[f.squadron_id].side

    def owner_near(self, pos: Pos, radius: float = OWN_SKY_M) -> Side | None:
        """Who holds the nearest objective, if one is within ``radius``."""
        best, who = radius, None
        for oid, os_ in self.state.objectives.items():
            o = self.objs.get(oid)
            if o is None or o.area_of is not None:
                continue
            d = _d(o.pos, pos)
            if d < best:
                best, who = d, os_.owner
        return who

    def awacs(self, side: Side, pos: Pos) -> bool:
        """Blue has AWACS (a superpower, D2); a defender has GCI over its own ground."""
        return side is Side.BLUE or self.owner_near(pos) is side

    def sam_sites(self, side: Side) -> list[tuple[GroundUnit, str]]:
        """A side's SAM sites that can fire, with their class."""
        out = []
        for u in self.state.ground_units.values():
            if u.side is side and u.role is GroundRole.SAM and strength(u) > 0:
                cls = combat.sam_class(u.composition)
                if cls:
                    out.append((u, cls))
        return out

    def _lose(self, f: Flight, idx: list[int], cause: str) -> None:
        if not idx:
            return
        side = self.side_of(f)
        n = min(len(idx), f.aircraft)
        f.aircraft -= n
        for _ in range(n):
            self.losses.append((self.state.clock, side, cause))
        if f.aircraft <= 0:
            f.state = FlightState.LOST
        log.info("AIR %s %s lost %d to %s (%d left)", f.callsign, f.mission, n, cause, f.aircraft)

    def _go_home(self, f: Flight) -> None:
        """Abort: fly straight to the last waypoint (home)."""
        if f.waypoints:
            f.next_wp = len(f.waypoints) - 1
            f.hold_until = None
            f.state = FlightState.EGRESS

    def rng(self, *parts: Any) -> random.Random:
        return random.Random(":".join(str(p) for p in (self.seed,) + parts))

    # ---------- at a waypoint ----------

    def on_waypoint(self, f: Flight, idx: int) -> None:
        """A flight not in DCS reached waypoint ``idx`` (called by the air bubble)."""
        if f.aircraft <= 0 or idx >= len(f.waypoints):
            return
        wp = f.waypoints[idx]
        side = self.side_of(f)
        start = self.last_pos.get(f.id) or self.base_pos(self.state.squadrons[f.squadron_id].base_id) or wp.pos
        self.last_pos[f.id] = wp.pos
        rng = self.rng(f.id, idx)
        aborted = f.state is FlightState.EGRESS and idx < len(f.waypoints) - 1
        # SAM sites on the leg just flown (each one once per flight).
        exposures = []
        for u, cls in self.sam_sites(_enemy(side)):
            rng_m = combat.SAM_TYPES[cls].range_m
            if u.id in self.exposed[f.id] or seg_dist(u.pos, start, wp.pos) > rng_m:
                continue
            self.exposed[f.id].add(u.id)
            at_target = wp.action in ATTACK_ACTIONS and not aborted and _d(u.pos, wp.pos) <= rng_m
            exposures.append((cls, at_target))
        if exposures:
            sead = self.suppressed.get(f.package_id, -math.inf) > self.state.clock or wp.action == "SEAD"
            self._lose(f, combat.sam_losses(self.types(f), exposures, sead, rng), "SAM")
        if f.aircraft <= 0 or aborted:
            return
        if wp.action == "CAS":
            self._cas(f, wp, rng)
        elif wp.action == "SEAD":
            self._sead(f, wp, rng)
        elif wp.action == "BOMB":
            self._bomb(f, wp, rng)

    def _targets(self, side: Side, wp: Waypoint, radius: float) -> list[GroundUnit]:
        return [u for u in self.state.ground_units.values() if u.side is not side and u.side is not Side.NEUTRAL
                and strength(u) > 0 and u.spawn is SpawnState.AGGREGATED and _d(u.pos, wp.pos) <= radius]

    def _hit(self, f: Flight, u: GroundUnit, rng: random.Random, cover: float = 1.0) -> int:
        """The flight attacks formation u: its air defence first, then the bombs."""
        self._lose(f, combat.shorad_losses(self.types(f), u.composition, rng), "SHORAD")
        if f.aircraft <= 0:
            return 0
        killed = combat.cas_kills(self.types(f), u.composition, rng, cover)
        for t, k in killed.items():
            u.composition[t] = u.composition.get(t, 0) - k
            if u.composition[t] <= 0:
                del u.composition[t]
        n = sum(killed.values())
        u.morale = max(0.0, u.morale - 0.03 * n)
        self.killed[self.side_of(f)] += n
        if n:
            log.info("AIR %s %s kills %d of %s (%d left)", f.callsign, f.mission, n, u.name, strength(u))
        return n

    def _cas(self, f: Flight, wp: Waypoint, rng: random.Random) -> None:
        from .flight_spawn import ATTACK_RADIUS_M
        side = self.side_of(f)
        radius = float(wp.params.get("radius_m", ATTACK_RADIUS_M["CAS"]))
        targets = [u for u in self._targets(side, wp, radius) if u.role is not GroundRole.SAM]
        if not targets:
            log.info("AIR %s %s found nothing to attack", f.callsign, f.mission)
            return
        u = max(targets, key=lambda u: (strength(u), u.id))
        town = u.location_id in self.objs and self.objs[u.location_id].type is ObjectiveType.CITY and not u.route
        self._hit(f, u, rng, CAS_COVER_TOWN if town else 1.0)

    def _sead(self, f: Flight, wp: Waypoint, rng: random.Random) -> None:
        from .flight_spawn import ATTACK_RADIUS_M
        side = self.side_of(f)
        radius = float(wp.params.get("radius_m", ATTACK_RADIUS_M["SEAD"]))
        sites = [(u, cls) for u, cls in self.sam_sites(_enemy(side)) if _d(u.pos, wp.pos) <= radius]
        self.suppressed[f.package_id] = self.state.clock + SUPPRESS_S
        if not sites:
            return
        u, cls = min(sites, key=lambda p: (_d(p[0].pos, wp.pos), p[0].id))
        if combat.sead_kills(self.types(f), rng):
            radar = next((r for r in combat.SAM_RADARS[cls] if u.composition.get(r, 0) > 0), None)
            if radar:
                u.composition[radar] -= 1
                if u.composition[radar] <= 0:
                    del u.composition[radar]
                log.info("AIR %s SEAD knocks out a %s at %s", f.callsign, radar, u.name)
                self.notify(f"{f.callsign} knocks out the radar of {u.name}")

    def _bomb(self, f: Flight, wp: Waypoint, rng: random.Random) -> None:
        side = self.side_of(f)
        ab = next((oid for oid, o in self.objs.items() if o.type is ObjectiveType.AIRBASE and _d(o.pos, wp.pos) < 3_000
                   and self.state.objectives.get(oid) and self.state.objectives[oid].owner is _enemy(side)), None)
        if ab is None:
            # Not an airbase: bomb whatever formation is at the point.
            targets = self._targets(side, wp, 3_000)
            if targets:
                self._hit(f, max(targets, key=lambda u: (strength(u), u.id)), rng)
            return
        for u in self._targets(side, wp, 4_000):     # the base's air defence
            if u.role in (GroundRole.AAA, GroundRole.SAM, GroundRole.GARRISON):
                self._lose(f, combat.shorad_losses(self.types(f), u.composition, rng), "SHORAD")
        if f.aircraft <= 0:
            return
        os_ = self.state.objectives[ab]
        for feat in self.objs[ab].features:
            os_.feature_damage[feat.id] = min(1.0, os_.feature_damage.get(feat.id, 0.0) + STRIKE_RUNWAY * f.aircraft)
        parked = 0
        for sq in self.state.squadrons.values():
            if sq.base_id == ab and sq.side is not side and sq.aircraft_ready > 0:
                k = combat.binomial(rng, sq.aircraft_ready, STRIKE_PARKED * f.aircraft)
                sq.aircraft_ready -= k
                parked += k
                for _ in range(k):
                    self.losses.append((self.state.clock, sq.side, "on the ground"))
        log.info("AIR %s strikes %s: runway %.0f%% down, %d aircraft destroyed on the ground", f.callsign,
                 self.objs[ab].name, 100 * (1 - os_.function(self.objs[ab])), parked)
        self.notify(f"{f.callsign} strikes {self.objs[ab].name}"
                    + (f": {parked} aircraft destroyed on the ground" if parked else ""))

    # ---------- air to air ----------

    def _airborne(self) -> list[Flight]:
        return [f for f in self.state.flights.values() if f.spawn is SpawnState.AGGREGATED and f.state in AIRBORNE
                and f.aircraft > 0 and f.pos is not None]

    def _is_fighter(self, f: Flight) -> bool:
        return f.mission in FIGHTER_MISSIONS and atype_q(self.types(f)) >= combat.AirConfig().fighter_quality

    def penalty(self, f: Flight) -> float:
        """Carrying bombs until it has attacked or jettisoned."""
        if f.mission in FIGHTER_MISSIONS or f.state is FlightState.EGRESS:
            return 0.0
        done = any(w.action in ATTACK_ACTIONS for w in f.waypoints[:f.next_wp])
        return 0.0 if done else combat.LOADED_PENALTY

    def quality(self, f: Flight) -> float:
        """Air-to-air quality as a fight would see it: type, bombs, AWACS or GCI."""
        side = self.side_of(f)
        return atype_q(self.types(f)) - self.penalty(f) + (combat.AirConfig().awacs_bonus
                                                           if self.awacs(side, f.pos) else 0.0)

    def picks_fights(self, side: Side) -> bool:
        """A side fighting by attrition only takes fights it expects to win."""
        return self.postures.get(side) in ("defensive", "cautious")

    def _engagements(self, now: float) -> None:
        flights = sorted(self._airborne(), key=lambda f: f.id)
        for hunter in flights:
            if not self._is_fighter(hunter) or hunter.aircraft <= 0 or self.cooldown.get(hunter.id, -1) > now:
                continue
            side = self.side_of(hunter)
            near = [f for f in flights if f.aircraft > 0 and self.side_of(f) is _enemy(side)
                    and self.cooldown.get(f.id, -1) <= now and _d(f.pos, hunter.pos) <= ENGAGE_M]
            if self.picks_fights(side):
                near = [f for f in near if self.quality(hunter) >= self.quality(f)]
            if not near:
                continue
            prey = min(near, key=lambda f: (_d(f.pos, hunter.pos), f.id))
            # Its fighters (escort or CAP) close by take the fight instead.
            guard = [g for g in flights if g.aircraft > 0 and self.side_of(g) is self.side_of(prey) and g is not prey
                     and self._is_fighter(g) and self.cooldown.get(g.id, -1) <= now
                     and _d(g.pos, prey.pos) <= ENGAGE_M] if not self._is_fighter(prey) else []
            if guard:
                prey = min(guard, key=lambda g: (_d(g.pos, prey.pos), g.id))
                if self.picks_fights(side) and self.quality(hunter) < self.quality(prey):
                    continue   # the escort is too strong: leave it
            # A side fighting by attrition runs from a stronger hunter, most of the time.
            ps = self.side_of(prey)
            if self._is_fighter(prey) and self.picks_fights(ps) and self.quality(hunter) > self.quality(prey) \
                    and self.rng("evade", hunter.id, prey.id, int(now)).random() < EVADE_CHANCE:
                self.cooldown[prey.id] = self.cooldown[hunter.id] = now + COOLDOWN_S
                self._go_home(prey)
                log.info("AIR %s %s runs from %s %s", prey.callsign, prey.mission, hunter.callsign, hunter.mission)
                continue
            self._fight(hunter, prey, now)

    def _fight(self, a: Flight, b: Flight, now: float) -> None:
        sa, sb = self.side_of(a), self.side_of(b)
        rng = self.rng("air", a.id, b.id, int(now))
        la, lb = combat.air_battle(AirSide(self.types(a), self.awacs(sa, a.pos), self.penalty(a)),
                                   AirSide(self.types(b), self.awacs(sb, b.pos), self.penalty(b)), rng)
        na, nb = a.aircraft, b.aircraft
        self._lose(a, la, "air to air")
        self._lose(b, lb, "air to air")
        self.cooldown[a.id] = self.cooldown[b.id] = now + COOLDOWN_S
        log.info("AIR fight: %s %s (%d) vs %s %s (%d): lose %d and %d", a.callsign, a.mission, na, b.callsign,
                 b.mission, nb, len(la), len(lb))
        if la or lb:
            self.notify(f"air fight near {self._where(a.pos)}: {a.callsign} loses {len(la)}, {b.callsign} {len(lb)}")
        for f, n0 in ((a, na), (b, nb)):
            if f.aircraft <= 0:
                continue
            if not self._is_fighter(f) or f.aircraft * 2 <= n0:
                self._go_home(f)   # strikers jettison and run; beaten fighters go home

    def _where(self, pos: Pos) -> str:
        oid = min((o for o in self.objs.values() if o.area_of is None and o.id in self.state.objectives),
                  key=lambda o: _d(o.pos, pos), default=None)
        return oid.name if oid else "the front"

    # ---------- intercepts (defensive and offensive air postures) ----------

    def attrition_ok(self, side: Side, hours: float = 24) -> float:
        """How freely a side may spend aircraft: 1 while it trades well, down to 0.3 while it doesn't."""
        since = self.state.clock - hours * 3600
        own = sum(1 for t, s, _ in self.losses if s is side and t >= since)
        their = sum(1 for t, s, _ in self.losses if s is _enemy(side) and t >= since)
        return max(0.3, min(1.0, (their + 2) / (own + 2)))

    def _scramble(self, side: Side, now: float) -> None:
        """Launch fighters at enemy flights near our ground with no fighters to protect them."""
        enemy = _enemy(side)
        flights = self._airborne()
        guards = [f for f in flights if self.side_of(f) is enemy and self._is_fighter(f)]
        for prey in sorted(flights, key=lambda f: f.id):
            if self.side_of(prey) is not enemy or prey.id in self.hunted or self._is_fighter(prey):
                continue
            if self.owner_near(prey.pos, 2 * OWN_SKY_M) is not side:
                continue    # not over (or near) our ground
            if any(_d(g.pos, prey.pos) <= 2 * ENGAGE_M for g in guards):
                continue    # covered: not worth it
            if self.rng("scramble", prey.id).random() > self.attrition_ok(side):
                self.hunted.add(prey.id)
                continue    # losing too many: let this one go
            sq = self._interceptors(side, prey.pos)
            if sq is None:
                return
            # Only where it expects to win: its type with GCI against the prey as it is now.
            ours = combat.atype(sq.aircraft_type).a2a + combat.AirConfig().awacs_bonus
            if ours < self.quality(prey):
                continue
            target = prey.waypoints[min(prey.next_wp, len(prey.waypoints) - 1)].pos
            base = self.base_pos(sq.base_id) or target
            self.counter += 1
            fid = f"{side.name.lower()}_int_{self.counter}"
            f = Flight(fid, f"pkg_{fid}", sq.id, f"{side.name.title()} INT {self.counter}", "INTERCEPT", 2, "",
                       [Waypoint(target, 7_000, action="ORBIT", params={"duration_s": 600}),
                        Waypoint(base, 500, action="LAND", params={"base": sq.base_id})],
                       takeoff_time=now, start=StartMode.RUNWAY)
            self.state.flights[fid] = f
            self.hunted.add(prey.id)
            log.info("AIR %s scrambles %s from %s at %s %s", side.name.lower(), fid, sq.base_id, prey.callsign,
                     prey.mission)

    def _interceptors(self, side: Side, pos: Pos):
        reserved = defaultdict(int)
        for f in self.state.flights.values():
            if f.state is FlightState.PLANNED:
                reserved[f.squadron_id] += f.aircraft
        best = None
        for sq in self.state.squadrons.values():
            if sq.side is not side or "CAP" not in sq.roles or sq.aircraft_ready - reserved[sq.id] < 2:
                continue
            bp = self.base_pos(sq.base_id)
            if bp is None or _d(bp, pos) > SCRAMBLE_REACH_M:
                continue
            q = combat.atype(sq.aircraft_type).a2a
            key = (-q, _d(bp, pos), sq.id)
            if best is None or key < best[0]:
                best = (key, sq)
        return best[1] if best else None

    # ---------- upkeep ----------

    def _hour(self) -> None:
        for oid, os_ in self.state.objectives.items():
            for k in list(os_.feature_damage):
                os_.feature_damage[k] = max(0.0, os_.feature_damage[k] - RUNWAY_REPAIR_H)
                if os_.feature_damage[k] == 0:
                    del os_.feature_damage[k]
        cutoff = self.state.clock - 48 * 3600
        self.losses = [x for x in self.losses if x[0] >= cutoff]

    def _tidy(self, now: float) -> None:
        st = self.state
        for f in list(st.flights.values()):
            if f.state is FlightState.PLANNED and f.spawn is SpawnState.AGGREGATED and now > f.takeoff_time + CANCEL_LATE_S:
                log.info("AIR %s %s cancelled: never took off", f.callsign, f.mission)
                self._drop(f)
            elif f.state in (FlightState.LANDED, FlightState.LOST) and f.spawn is SpawnState.AGGREGATED:
                done = self.done_at.setdefault(f.id, now)
                if now - done > KEEP_DONE_S:
                    self._drop(f)
        for k in [k for k, t in self.cooldown.items() if t < now - 3600]:
            del self.cooldown[k]
        for k in [k for k, t in self.suppressed.items() if t < now]:
            del self.suppressed[k]

    def _drop(self, f: Flight) -> None:
        st = self.state
        st.flights.pop(f.id, None)
        self.last_pos.pop(f.id, None)
        self.exposed.pop(f.id, None)
        self.hunted.discard(f.id)
        self.done_at.pop(f.id, None)
        p = st.packages.get(f.package_id)
        if p is not None:
            p.flight_ids = [i for i in p.flight_ids if i != f.id]
            if not p.flight_ids:
                del st.packages[p.id]

    # ---------- reporting ----------

    def status_lines(self) -> list[str]:
        day = self.state.clock - 24 * 3600
        lost = {s: sum(1 for t, x, _ in self.losses if x is s and t >= day) for s in (Side.BLUE, Side.RED)}
        up = defaultdict(int)
        for f in self._airborne():
            up[self.side_of(f)] += 1
        return [f"Air: {up[Side.BLUE]} blue and {up[Side.RED]} red flights airborne out of DCS; "
                f"aircraft lost in 24 h: blue {lost[Side.BLUE]}, red {lost[Side.RED]}"]


def atype_q(types: list[str]) -> float:
    return max((combat.atype(t).a2a for t in types), default=0.0)
