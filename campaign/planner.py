"""The planner (ATO, backlog item 2, docs/planner.md): each side's flights, planned from the war.

- **Rolling plan.** Each side plans its flights an hour at a time, ``HORIZON_S`` ahead: on the first
  call, the next three hours; then each hour, the hour that has come into the horizon.
- **Size.** Each squadron flies ``SORTIES_PER_DAY`` sorties per aircraft per day for its side's air
  posture, scaled by its base's runway and (for a side that fights by attrition) by how well its
  trade with the enemy is going (``AirWar.attrition_ok``).
- **What.** Missions are scored and the best flown while there are aircraft:
  - offensive (blue here): CAP over the front, CAS over battles and ahead of its own attacks,
    interdiction of enemy columns moving up and of formations massed at the front, strikes on
    enemy airbases, and SEAD against air defences near the front;
  - defensive (red here): CAP over its main bases and its capital, CAS and a forward CAP for its
    own counterattacks, and attacks on exposed enemy formations on its ground (no escort near).
    Its intercepts are launched as they happen (campaign/air_war.py), from fighters kept back.
- **Packages.** A package is a target and a time on target: the main flight, SEAD (offensive side)
  if a SAM site covers its target or route, and an escort if enemy fighters can reach it.
- **Routes:** climb, an ingress point ``IP_M`` short of the target, the target with its action, an
  egress point off to the side, and home (``LAND``).
"""
from __future__ import annotations

import logging
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from . import combat
from .air_war import AirWar, seg_dist
from .bubble import strength
from .catalog import cruise_ms
from .ground_war import MANEUVER, GroundWar
from .model import (CampaignState, Flight, FlightState, GroundRole, GroundUnit, Package, Pos, Side, SpawnState,
                    Squadron, StartMode, Theater, Waypoint)
from .payloads import loadouts

log = logging.getLogger(__name__)

NM = 1852.0
FT = 0.3048
SLOT_S = 3600
HORIZON_S = 3 * 3600
LEAD_S = 900                 # nothing is planned to take off sooner than this
SPACING_S = 900              # takeoffs in a slot are this far apart (then they wrap round)
IP_M = 30_000
EGRESS_M = 15_000
ALERT = 2                    # a defensive side keeps this many fighters per CAP squadron for intercepts
BORROW = 2                   # aircraft a package may take from the next hour's share
SEAD_PER_SLOT = 2            # SEAD missions of their own (not with a package) per hour
ESCORT_REACH_M = 250_000
RISK_WEIGHT = 8.0            # a task's score is divided by 1 + this x its SAM risk
MAX_RISK = 0.15              # tasks riskier than this (summed shot-down chances) aren't flown     # enemy fighters based this close to a target can reach it
SORTIES_PER_DAY = {"offensive": 2.5, "defensive": 2.0, "cautious": 1.2}
ALT_M = {"CAP": 26_000 * FT, "ESCORT": 24_000 * FT, "CAS": 16_000 * FT, "BAI": 16_000 * FT,
         "STRIKE": 22_000 * FT, "SEAD": 20_000 * FT}
ROLE = {"CAP": "CAP", "ESCORT": "CAP", "CAS": "CAS", "BAI": "CAS", "STRIKE": "STRIKE", "SEAD": "SEAD"}
ON_STATION_S = {"CAP": 3600}

# Loadout names (data/payloads.json) are scored by these words; the best name is used.
AA_WORDS = ("AIM-120", "AIM-7", "AIM-9", "R-27", "R-24", "R-73", "R-60", "R-3", "Fuel", "FUEL")
AG_WORDS = ("AGM-65", "Mk-82", "MK-82", "MK82", "Mk82", "GBU-12", "GBU-38", "CBU", "B-8", "FAB-250", "FAB-100",
            "S-8", "S-24", "BetAB", "Hard targets", "CLUSTERS")
STRIKE_WORDS = ("GBU-10", "GBU-31", "Mk-84", "MK-84", "FAB-500", "FAB-1500", "BetAB", "KAB", "BOMBS", "GBU-12")
SEAD_WORDS = ("AGM-88", "HARM", "Kh-58", "Kh-25MP")


def pick_loadout(aircraft_type: str, mission: str) -> str:
    words = {"CAP": AA_WORDS, "ESCORT": AA_WORDS, "INTERCEPT": AA_WORDS, "CAS": AG_WORDS, "BAI": AG_WORDS,
             "STRIKE": STRIKE_WORDS, "SEAD": SEAD_WORDS}.get(mission, AG_WORDS)
    best, best_score = "", -math.inf
    for name in loadouts(aircraft_type):
        score = sum(1 for w in words if w in name)
        if mission in ("CAP", "ESCORT", "INTERCEPT"):
            score -= 2 * sum(1 for w in AG_WORDS + STRIKE_WORDS if w in name)
        if score > best_score:
            best, best_score = name, score
    return best


@dataclass
class Task:
    """A mission worth flying, before it has aircraft."""
    score: float
    mission: str          # CAP, CAS, BAI, STRIKE, SEAD
    target: Pos
    target_id: str
    n: int = 2
    sead: bool = False    # may need SEAD (offensive side)
    escort: bool = False
    params: dict = field(default_factory=dict)
    lead_s: float = 0.0   # want the time on target at least this long after takeoff slot start


def _d(a: Pos, b: Pos) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _enemy(side: Side) -> Side:
    return Side.RED if side is Side.BLUE else Side.BLUE


def _mean(ps: list[Pos]) -> Pos:
    return Pos(sum(p.x for p in ps) / len(ps), sum(p.y for p in ps) / len(ps))


class Planner:
    """A ``server.System``: plans both sides' flights into the campaign state."""
    name = "planner"

    def __init__(self, state: CampaignState, theater: Theater, postures: dict[Side, str], gw: GroundWar, air: AirWar,
                 *, seed: int = 1) -> None:
        self.state = state
        self.objs = theater.objectives
        self.postures = postures
        self.gw = gw
        self.air = air
        self.seed = seed
        self.planned_until: dict[Side, float] = {}
        self.carry: dict[Side, float] = defaultdict(float)
        self.counter = 0

    # ---------- System ----------

    def export(self) -> dict[str, Any]:
        return {"planned_until": {s.name: t for s, t in self.planned_until.items()},
                "carry": {s.name: c for s, c in self.carry.items()}, "counter": self.counter}

    def restore(self, data: dict[str, Any]) -> None:
        self.planned_until = {Side[k]: float(v) for k, v in (data.get("planned_until") or {}).items()}
        self.carry = defaultdict(float, {Side[k]: float(v) for k, v in (data.get("carry") or {}).items()})
        self.counter = int(data.get("counter", 0))

    def on_hello(self, hello, new_run: bool) -> None:
        pass

    def on_event(self, data: dict[str, Any]) -> None:
        pass

    async def tick(self) -> None:
        pass

    def advance(self, t0: float, t1: float) -> None:
        for side in self.postures:
            until = self.planned_until.get(side)
            if until is None:
                until = self.planned_until[side] = t1 + LEAD_S
            while until < t1 + HORIZON_S:
                self.plan_slot(side, until, until + SLOT_S)
                until = self.planned_until[side] = until + SLOT_S

    # ---------- aircraft ----------

    def base_pos(self, sq: Squadron) -> Pos:
        return self.objs[sq.base_id].pos

    def runway(self, sq: Squadron) -> float:
        os_ = self.state.objectives.get(sq.base_id)
        return os_.function(self.objs[sq.base_id]) if os_ and sq.base_id in self.objs else 1.0

    def reserved(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for f in self.state.flights.values():
            if f.state is FlightState.PLANNED:
                out[f.squadron_id] += f.aircraft
        return out

    def size(self, sq: Squadron) -> int:
        out = sum(f.aircraft for f in self.state.flights.values() if f.squadron_id == sq.id
                  and f.state not in (FlightState.PLANNED, FlightState.LANDED, FlightState.LOST))
        return sq.aircraft_ready + out

    def available(self, sq: Squadron, reserved: dict[str, int]) -> int:
        keep = ALERT if self.postures.get(sq.side) == "defensive" and "CAP" in sq.roles else 0
        return max(0, sq.aircraft_ready - reserved[sq.id] - keep)

    def squadron_for(self, side: Side, mission: str, target: Pos, n: int, reserved: dict[str, int]) -> Squadron | None:
        role = ROLE[mission]

        def fit(sq: Squadron) -> float:
            t = combat.atype(sq.aircraft_type)
            return {"CAP": t.a2a, "ESCORT": t.a2a, "SEAD": t.sead}.get(mission, t.cas_kills)

        options = [sq for sq in self.state.squadrons.values() if sq.side is side and role in sq.roles
                   and self.available(sq, reserved) >= n and self.runway(sq) > 0.2
                   and (mission != "SEAD" or combat.atype(sq.aircraft_type).sead > 0)]
        if not options:
            return None
        return max(options, key=lambda sq: (round(fit(sq), 1), -_d(self.base_pos(sq), target), sq.id))

    # ---------- the plan ----------

    def plan_slot(self, side: Side, s0: float, s1: float) -> None:
        posture = self.postures[side]
        rate = SORTIES_PER_DAY.get(posture, 2.0)
        squadrons = [sq for sq in self.state.squadrons.values() if sq.side is side]
        if not squadrons:
            return
        factor = 1.0 if posture == "offensive" else self.air.attrition_ok(side)
        budget = self.carry[side] + factor * sum(self.size(sq) * rate / 24 * self.runway(sq) for sq in squadrons)
        tasks = self.offensive_tasks(side) if posture == "offensive" else self.defensive_tasks(side)
        sites = [(u, cls) for u, cls in self.air.sam_sites(_enemy(side))]
        for t in tasks:
            r = self.risk(side, t, sites)
            t.score = -1 if r > MAX_RISK else t.score / (1 + RISK_WEIGHT * r)
        tasks = [t for t in tasks if t.score > 0]
        reserved = self.reserved()
        flown: list[str] = []
        k = 0
        for task in sorted(tasks, key=lambda t: (-t.score, t.mission, t.target_id)):
            if budget < 2:
                break
            takeoff = s0 + (k * SPACING_S) % SLOT_S
            used = self._package(side, task, takeoff, reserved, budget)
            if used:
                budget -= used
                k += 1
                flown.append(f"{task.mission} {self._name(task)}")
        self.carry[side] = max(-BORROW, min(budget, 4.0))
        if flown:
            log.info("PLAN %s T+%dh: %s", side.name.lower(), int(s0 // 3600), ", ".join(flown))

    def _name(self, task: Task) -> str:
        o = self.objs.get(task.target_id)
        if o is not None:
            return o.name
        u = self.state.ground_units.get(task.target_id)
        return u.name if u else task.target_id

    # ---------- what's worth doing ----------

    def enemy_fighters(self, side: Side) -> list[Squadron]:
        return [sq for sq in self.state.squadrons.values() if sq.side is _enemy(side) and "CAP" in sq.roles
                and self.size(sq) > 0]

    def front_line(self) -> Pos | None:
        pts = [self.objs[o].pos for a, b in self.gw.front_links() for o in (a, b)]
        return _mean(pts) if pts else None

    def battle_targets(self, side: Side, at: str) -> list[GroundUnit]:
        """The enemy formations fighting a battle at ``at``, from ``side``'s point of view."""
        b = self.state.battles[at]
        enemy = _enemy(side)
        if b.attacker is enemy:
            return [u for u in self.gw.units(enemy) if u.battle == at and u.role is not GroundRole.ARTILLERY]
        return [u for u in self.gw.at(at, enemy) if u.role in MANEUVER or u.role is GroundRole.GARRISON]

    def offensive_tasks(self, side: Side) -> list[Task]:
        enemy = _enemy(side)
        tasks: list[Task] = []
        front = self.front_line()
        if front is not None and self.enemy_fighters(side):
            tasks.append(Task(9, "CAP", self.clear_of_sams(side, front), "front",
                              params={"duration_s": ON_STATION_S["CAP"]}))
        for at in sorted(self.state.battles):
            us = self.battle_targets(side, at)
            if us:
                power = sum(self.gw.fighter(u).power() for u in us)
                tasks.append(Task(10 + min(5, power / 100), "CAS", _mean([u.pos for u in us]), at, sead=True))
        # Ahead of its own attacks: the objective they're driving to.
        for oid in sorted({u.route[-1] for u in self.gw.maneuver(side) if u.route and u.order.name == "ATTACK"
                           and self.gw.owner(u.route[-1]) is enemy} - set(self.state.battles)):
            if self.gw.at(oid, enemy):
                tasks.append(Task(7, "CAS", self.objs[oid].pos, oid, sead=True))
        # Interdiction: enemy columns moving up, then formations massed at the front.
        front_objs = set(self.gw.front(enemy))   # ours, next to theirs
        for u in self.gw.maneuver(enemy):
            if u.route and (u.route[-1] in self.gw.front(side) or u.route[-1] in front_objs or u.route[-1] in
                            self.state.battles):
                tasks.append(Task(6 + min(4, self.gw.fighter(u).power() / 100), "BAI", u.pos, u.id, sead=True,
                                  params={"moving": True}))
            elif not u.route and u.location_id in self.gw.front(side) and u.battle is None:
                tasks.append(Task(5.5 + min(3, self.gw.fighter(u).power() / 100), "BAI", u.pos, u.id, sead=True))
        # Their airbases with aircraft: worth more the more aircraft there are.
        by_base: dict[str, int] = defaultdict(int)
        for sq in self.state.squadrons.values():
            if sq.side is enemy and sq.aircraft_ready > 0 and self.runway(sq) > 0.4:
                by_base[sq.base_id] += sq.aircraft_ready
        for b, n in by_base.items():
            tasks.append(Task(3 + min(3, n / 4), "STRIKE", self.objs[b].pos, b, n=2, sead=True,
                              escort=bool(self.enemy_fighters(side))))
        # Air defences covering the front: the most dangerous few.
        if front is not None:
            sites = [(u, cls) for u, cls in self.air.sam_sites(enemy)
                     if _d(u.pos, front) <= combat.SAM_TYPES[cls].range_m + 20_000]
            for u, cls in sorted(sites, key=lambda p: (-combat.SAM_TYPES[p[1]].pk, p[0].id))[:SEAD_PER_SLOT]:
                tasks.append(Task(4 + combat.SAM_TYPES[cls].pk * 20, "SEAD", u.pos, u.id, params={"radius_m": 15_000}))
        return self._dedupe(tasks)

    def defensive_tasks(self, side: Side) -> list[Task]:
        enemy = _enemy(side)
        tasks: list[Task] = []
        # The core: CAP over what matters most, the side's main base and what the enemy is driving at.
        goal = self.gw.sides.get(enemy).goal if self.gw.sides.get(enemy) else None
        bases = sorted({sq.base_id for sq in self.state.squadrons.values() if sq.side is side},
                       key=lambda b: (-sum(self.size(sq) for sq in self.state.squadrons.values() if sq.base_id == b), b))
        cover = [self.objs[goal].pos] if goal in self.objs and self.gw.owner(goal) is side else []
        cover += [self.objs[b].pos for b in bases[:1]]
        if self.enemy_air(side):
            for i, p in enumerate(cover[:2]):
                tasks.append(Task(9 - i, "CAP", p, f"cap{i}", params={"duration_s": ON_STATION_S["CAP"]}))
        own = self.gw.sides[side].countries if side in self.gw.sides else frozenset()
        for at in sorted(self.state.battles):
            b = self.state.battles[at]
            us = self.battle_targets(side, at)
            if not us:
                continue
            where = _mean([u.pos for u in us])
            if b.attacker is side:
                # Its own counterattack: the main surge.
                tasks.append(Task(9, "CAS", where, at, escort=True))
                tasks.append(Task(8, "CAP", self.objs[at].pos, f"fwd_{at}", params={"duration_s": 2400}))
            elif not own or self.objs[at].country in own:
                exposed = not self.covered(enemy, where)
                tasks.append(Task(6 if exposed else 3, "CAS", where, at, escort=not exposed))
        for u in self.gw.maneuver(enemy):
            if u.route and self.air.owner_near(u.pos) is side:
                exposed = not self.covered(enemy, u.pos)
                tasks.append(Task(5 if exposed else 2, "BAI", u.pos, u.id, escort=not exposed, params={"moving": True}))
        return self._dedupe(tasks)

    def risk(self, side: Side, task: Task, sites: list) -> float:
        """Summed SAM shot-down chances on the way to a task's target and there, from the side's
        nearest base. A SEAD flight with the package (or the task's own target site) counts less."""
        bases = [self.base_pos(sq) for sq in self.state.squadrons.values() if sq.side is side]
        if not bases or task.mission == "CAP":
            return 0.0
        base = min(bases, key=lambda b: _d(b, task.target))
        cfg = combat.SamConfig()
        total = 0.0
        for u, cls in sites:
            st = combat.SAM_TYPES[cls]
            if u.id == task.target_id:
                total += st.pk * cfg.sead_factor
            elif _d(u.pos, task.target) <= st.range_m:
                total += st.pk * (cfg.sead_factor if task.sead else 1.0)
            elif seg_dist(u.pos, base, task.target) <= st.range_m:
                total += st.pk * cfg.transit_factor
        return total

    def clear_of_sams(self, side: Side, pos: Pos) -> Pos:
        """Move a CAP station back towards its own side until no known enemy SAM covers it."""
        home = [self.base_pos(sq) for sq in self.state.squadrons.values() if sq.side is side]
        if not home:
            return pos
        to = min(home, key=lambda b: _d(b, pos))
        d = _d(to, pos) or 1.0
        sites = [(u.pos, combat.SAM_TYPES[cls].range_m + 5_000) for u, cls in self.air.sam_sites(_enemy(side))]
        p = pos
        for k in range(0, int(d), 5_000):
            p = Pos(pos.x + (to.x - pos.x) * k / d, pos.y + (to.y - pos.y) * k / d)
            if all(_d(p, s) > r for s, r in sites):
                return p
        return p

    def enemy_air(self, side: Side) -> bool:
        return any(self.size(sq) > 0 for sq in self.state.squadrons.values() if sq.side is _enemy(side))

    def covered(self, side: Side, pos: Pos) -> bool:
        """Is there a ``side`` CAP planned or flying within reach of pos?"""
        for f in self.state.flights.values():
            if f.mission == "CAP" and f.state not in (FlightState.LANDED, FlightState.LOST) \
                    and self.state.squadrons[f.squadron_id].side is side:
                station = next((w.pos for w in f.waypoints if w.action == "ORBIT"), None)
                if station and _d(station, pos) <= 60_000:
                    return True
        return False

    def _dedupe(self, tasks: list[Task]) -> list[Task]:
        """At most one task per target and mission; skip targets already planned for in the horizon."""
        planned = {(p.target_id, self.state.flights[p.flight_ids[0]].mission) for p in self.state.packages.values()
                   if p.flight_ids and p.flight_ids[0] in self.state.flights
                   and self.state.flights[p.flight_ids[0]].state is FlightState.PLANNED}
        out, seen = [], set()
        for t in sorted(tasks, key=lambda t: -t.score):
            key = (t.target_id, t.mission)
            if key in seen:
                continue
            seen.add(key)
            # Already planned: still worth a second flight only for the very top (CAS over battles, CAP).
            if key in planned and t.mission not in ("CAP",):
                continue
            out.append(t)
        return out

    # ---------- building a package ----------

    def _package(self, side: Side, task: Task, takeoff: float, reserved: dict[str, int], budget: float) -> int:
        main_sq = self.squadron_for(side, task.mission, task.target, task.n, reserved)
        if main_sq is None:
            return 0
        enemy = _enemy(side)
        base = self.base_pos(main_sq)
        # SEAD: SAM sites covering the target or the way there.
        sead_site = None
        if task.sead and task.mission != "SEAD":
            sites = [(u, cls) for u, cls in self.air.sam_sites(enemy)
                     if seg_dist(u.pos, base, task.target) <= combat.SAM_TYPES[cls].range_m]
            if sites:
                sead_site = min(sites, key=lambda p: (_d(p[0].pos, task.target), p[0].id))[0]
        escort = task.escort
        # Short of aircraft this hour: the escort goes first, then SEAD (and then maybe the package).
        # A package may borrow up to BORROW aircraft from the next hour.
        if escort and task.n + (2 if sead_site else 0) + 2 > budget + BORROW:
            escort = False
        sead_short = sead_site is not None and task.n + 2 > budget + BORROW
        reserved_try = dict(reserved)
        reserved_try[main_sq.id] += task.n
        sead_sq = self.squadron_for(side, "SEAD", sead_site.pos, 2, reserved_try) if sead_site else None
        if sead_sq:
            reserved_try[sead_sq.id] += 2
        esc_sq = self.squadron_for(side, "ESCORT", task.target, 2, reserved_try) if escort else None
        if esc_sq:
            reserved_try[esc_sq.id] += 2
        if sead_site is not None and (sead_sq is None or sead_short):
            sead_sq = None
            cls = combat.sam_class(sead_site.composition) or ""
            if combat.SAM_TYPES.get(cls, combat.SamType(0, 0)).pk >= 0.04:
                return 0   # too dangerous without SEAD
        self.counter += 1
        pid = f"pkg_{side.name.lower()}_{self.counter}"
        pkg = Package(pid, side, task.target_id, 0.0, [])
        self.state.packages[pid] = pkg
        flights = [self._flight(pkg, main_sq, task.mission, task.target, task.n, task.params)]
        if sead_sq:
            flights.append(self._flight(pkg, sead_sq, "SEAD", sead_site.pos, 2, {"radius_m": 15_000}))
        if esc_sq:
            flights.append(self._flight(pkg, esc_sq, "ESCORT", task.target, 2, {}))
        # Times: the slowest flight sets the package's speed; SEAD arrives 2 minutes early.
        speed = min(cruise_ms(self.state.squadrons[f.squadron_id].aircraft_type) for f in flights)
        tot = takeoff + self._to_target(flights[0], speed)
        for f in flights:
            early = 120 if f.mission == "SEAD" else 0
            f.takeoff_time = tot - early - self._to_target(f, speed)
        shift = max(0.0, self.state.clock + LEAD_S / 3 - min(f.takeoff_time for f in flights))
        if task.params.get("moving"):
            self._aim_moving(flights[0], task, tot)
        for f in flights:
            f.takeoff_time += shift
            reserved[f.squadron_id] += f.aircraft
        pkg.tot = tot + shift
        return sum(f.aircraft for f in flights)

    def _aim_moving(self, f: Flight, task: Task, tot: float) -> None:
        u = self.state.ground_units.get(task.target_id)
        if u is None:
            return
        p = self.gw.predict(u, tot - self.state.clock)
        for w in f.waypoints:
            if w.action in ("CAS", "BOMB"):
                w.pos = p

    def _to_target(self, f: Flight, speed: float) -> float:
        sq = self.state.squadrons[f.squadron_id]
        here, t = self.base_pos(sq), 0.0
        for w in f.waypoints:
            t += _d(here, w.pos) / speed
            here = w.pos
            if w.action in ("CAS", "BOMB", "SEAD", "ORBIT") or (f.mission == "ESCORT" and w.params.get("target")):
                return t
        return t

    def _flight(self, pkg: Package, sq: Squadron, mission: str, target: Pos, n: int, params: dict) -> Flight:
        base = self.base_pos(sq)
        alt = ALT_M.get(mission, 20_000 * FT)
        dx, dy = target.x - base.x, target.y - base.y
        d = math.hypot(dx, dy) or 1.0
        ux, uy = dx / d, dy / d
        climb = Pos(base.x + ux * min(20_000, d * 0.3), base.y + uy * min(20_000, d * 0.3))
        land = Waypoint(base, 0, action="LAND", params={"base": sq.base_id})
        if mission == "CAP":
            wps = [Waypoint(climb, alt), Waypoint(target, alt, action="ORBIT",
                                                   params={"duration_s": params.get("duration_s", 3600)}), land]
        else:
            back = min(IP_M, d * 0.5)
            ip = Pos(target.x - ux * back, target.y - uy * back)
            side_sign = 1 if random.Random(f"{self.seed}:{pkg.id}").random() < 0.5 else -1
            egress = Pos(ip.x - uy * EGRESS_M * side_sign, ip.y + ux * EGRESS_M * side_sign)
            action = {"CAS": "CAS", "BAI": "CAS", "STRIKE": "BOMB", "SEAD": "SEAD"}.get(mission)
            p = {k: v for k, v in params.items() if k != "moving"}
            if mission == "ESCORT":
                p["target"] = True
            wps = [Waypoint(climb, alt), Waypoint(ip, alt), Waypoint(target, alt, action=action, params=p),
                   Waypoint(egress, alt), land]
        self.counter += 1
        side = sq.side
        fid = f"{side.name.lower()}_{mission.lower()}_{self.counter}"
        f = Flight(fid, pkg.id, sq.id, f"{mission} {self.counter}", mission, n, pick_loadout(sq.aircraft_type, mission),
                   wps, takeoff_time=0.0, start=StartMode.RAMP_HOT, player_preferred=sq.player_flyable)
        self.state.flights[fid] = f
        pkg.flight_ids.append(fid)
        return f

    # ---------- reporting ----------

    def status_lines(self) -> list[str]:
        planned = defaultdict(int)
        for f in self.state.flights.values():
            if f.state is FlightState.PLANNED:
                planned[self.state.squadrons[f.squadron_id].side] += 1
        return [f"Planned flights: blue {planned[Side.BLUE]}, red {planned[Side.RED]}"]
