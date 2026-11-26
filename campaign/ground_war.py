"""The ground war (item 8): formations march along links, fight for objectives, and take them.

Built from the P7 war sim (tools/war_sim.py), on the campaign state:

- **Movement.** A formation with a ``route`` marches along the links' road routes at
  ``MARCH_MS``. Off-map its position moves along the route; in DCS (spawned) DCS drives it, the
  engine sends it the road leg it is on, and its progress is read back from its position.
- **Defence.** Each town and airbase has a garrison (a ``GARRISON`` formation that never moves and
  spawns in DCS like any other). Formations standing at a threatened objective deploy on its edge
  (``edge_m``: by town size), facing the road the enemy is coming on.
- **Battles.** A formation that reaches an enemy objective stops ``STANDOFF_M`` short and attacks
  whoever holds it: formations there and the local garrison. Fights are hourly
  (``combat.ground_hour``). In DCS, formations take only DCS losses; abstract fire hits only the
  rest (formations not in DCS, garrisons). A side breaks below 60% strength or 35% morale; an
  attack that hasn't won in 12 hours is called off.
- **Captures.** An objective changes hands when its defenders are gone; infrastructure in its
  area (``ObjectiveDef.area_of``) goes with it, air defences left there are overrun, and
  squadrons based there move back.
- **Commanders** set orders every ``COMMAND_S``. Attack posture (blue in the first scenario):
  attack front objectives where it has 2:1 (terrain included), gather for the best one it can't
  yet, and move everyone else up. Defend posture: retake its own ground where it has 2:1, and send
  the nearest reserves (no long detours) to threatened objectives until the defence matches the
  enemy next to it.

Formations with order HOLD never move, and air defence (SAM, AAA) never does either. Neutral
objectives are never entered. Each hour's dice come from a seed and the hour, so a saved war
goes on the same way whatever ran before.

``python3 -m campaign.ground_war <scenario> --hours 72`` runs a war with no DCS.
"""
from __future__ import annotations

import heapq
import logging
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from . import combat
from .bubble import strength
from .combat import Fighter
from .model import (Battle, CampaignState, GroundOrder, GroundRole, GroundUnit, Link, ObjectiveDef, ObjectiveType,
                    Pos, Side, SpawnState, Theater)

log = logging.getLogger(__name__)

NM = 1852.0
MARCH_MS = 25_000 / 3600     # road march, halts included; also the speed ordered in DCS
STANDOFF_M = 3_000           # attackers stop this far short of the objective they attack
DEFEND_SPREAD_M = 400        # and this far apart, across the road
REINFORCE_MAX_M = 60_000     # reserves are sent at most this far by road (about 2.5 h)...
REINFORCE_DETOUR = 1.6       # ...and only by a road at most this times the straight line (+10 km)
COMMAND_S = 3 * 3600         # commanders give orders this often
HOUR_S = 3600
ATTACK_ODDS = 2.0            # attack only with this much power over the defence (terrain included)
REST_S = 6 * 3600            # a formation regroups this long after an attack
DISBAND_STRENGTH = 0.2       # below this a formation is destroyed as a unit
FRESH = (0.7, 0.6, 0.4)      # strength, morale, supply a formation needs to be sent to attack
SUPPLY_REACH_M = 150_000     # resupply fades to a fifth this far from the nearest supply source
OFF_ROAD_FACTOR = 1.4        # a link without a DCS road route: this times the straight line
MANEUVER = {GroundRole.ARMOR, GroundRole.MECH, GroundRole.INFANTRY, GroundRole.ARTILLERY}
FRONTLINE = {GroundRole.ARMOR, GroundRole.MECH, GroundRole.INFANTRY}   # take a defensive post
STATIC_ROLES = {GroundRole.SAM, GroundRole.AAA}

# Local troops holding towns and airbases (DCS types). Infantry is a mix: riflemen, anti-tank
# (RPG) and machine guns. DCS has no red machine-gunner nor blue anti-tank soldier, so those go to
# riflemen. MANPADS are left out for now (backlog item 8: how to mix them in).
@dataclass(frozen=True)
class GarrisonTypes:
    rifle: str
    rifle2: str | None     # a second rifleman type, for variety (red: paratroopers)
    at: str | None         # anti-tank
    mg: str | None         # machine gun
    manpads: str           # not in garrisons yet
    apc: str | None
    gun: str | None        # AA gun emplacement


GARRISON_TYPES = {
    Side.RED: GarrisonTypes("Infantry AK", "Paratrooper AKS-74", "Soldier RPG", None, "SA-18 Igla manpad",
                            "BTR-80", "ZU-23 Emplacement"),
    Side.BLUE: GarrisonTypes("Soldier M4", None, None, "Soldier M249", "Soldier stinger",
                             "M1043 HMMWV Armament", None),
}


def infantry(side: Side, n: int) -> dict[str, int]:
    """n infantry of a side: a fifth anti-tank, a tenth machine guns, the rest riflemen."""
    t = GARRISON_TYPES.get(side, GARRISON_TYPES[Side.RED])
    at = n // 5 if t.at else 0
    mg = n // 10 if t.mg else 0
    rifles = n - at - mg
    rifle2 = rifles // 4 if t.rifle2 else 0
    comp = {t.rifle: rifles - rifle2, t.rifle2: rifle2, t.at: at, t.mg: mg}
    return {k: v for k, v in comp.items() if k and v > 0}

Notify = Callable[[str], None]
Call = Callable[[str, dict[str, Any]], Awaitable[Any]]


def garrison_for(o: ObjectiveDef, side: Side) -> dict[str, int]:
    """What holds a town or airbase with no formation in it: more in bigger towns."""
    if side not in GARRISON_TYPES or o.type not in (ObjectiveType.CITY, ObjectiveType.AIRBASE):
        return {}
    t = GARRISON_TYPES[side]
    if o.type is ObjectiveType.AIRBASE:
        n_inf, vehicles = 40, {t.apc: 4, t.gun: 6}
    else:
        n_inf, vehicles = 10 + 5 * o.priority, {t.apc: o.priority, t.gun: 1 + o.priority // 2}
    comp = infantry(side, n_inf)
    comp.update({k: v for k, v in vehicles.items() if k and v > 0})
    return comp


def occupiers(side: Side) -> dict[str, int]:
    """What the side that takes an objective leaves behind to hold it."""
    t = GARRISON_TYPES.get(side, GARRISON_TYPES[Side.RED])
    comp = infantry(side, 12)
    if t.gun:
        comp[t.gun] = 1
    return comp


def _mix_old_garrison(u: GroundUnit) -> None:
    """A garrison saved with all riflemen, or with MANPADS: the same number of infantry, mixed."""
    t = GARRISON_TYPES.get(u.side)
    if t is None:
        return
    kinds = [k for k in (t.rifle, t.rifle2, t.at, t.mg, t.manpads) if k]
    old = {k: u.composition.get(k, 0) for k in kinds}
    if not old[t.manpads] and any(old[k] for k in kinds if k != t.rifle):
        return   # already mixed, no MANPADS
    n = sum(old.values())
    if not n:
        return
    for k in kinds:
        u.composition.pop(k, None)
    for k, v in infantry(u.side, n).items():
        u.composition[k] = u.composition.get(k, 0) + v
    u.sited, u.spots = False, []


# How far an objective's edge is from its centre. The map layer has no town outlines; a town's
# priority comes from its buildings within 1.5 km (campaign/theater_build.py: town_priority).
TOWN_EDGE_M = {1: 500.0, 2: 750.0, 3: 1_000.0}
AIRBASE_EDGE_M = 1_500.0


def edge_m(o: ObjectiveDef) -> float:
    if o.type is ObjectiveType.AIRBASE:
        return AIRBASE_EDGE_M
    if o.type is ObjectiveType.CITY:
        return TOWN_EDGE_M.get(o.priority, TOWN_EDGE_M[3] if o.priority > 3 else TOWN_EDGE_M[1])
    return 500.0


def garrison_unit(state: CampaignState, o: ObjectiveDef, side: Side, comp: dict[str, int]) -> GroundUnit:
    """Add a garrison formation holding o (a fresh id: the old one may still be in DCS)."""
    n = 1
    while f"gar_{o.id}_{n}" in state.ground_units:
        n += 1
    u = GroundUnit(f"gar_{o.id}_{n}", side, f"{o.name} garrison", GroundRole.GARRISON, dict(comp), o.pos, o.id,
                   GroundOrder.HOLD, full=sum(comp.values()), ring_m=edge_m(o))
    state.ground_units[u.id] = u
    return u


@dataclass
class SideOrders:
    posture: str = "defend"      # "attack" | "defend"
    goal: str | None = None      # objective an attacker drives towards
    name: str = ""
    countries: frozenset[str] = frozenset()   # a defender only attacks to retake its own
    air: str = ""                # air posture: "offensive" | "defensive" | "cautious" (campaign/planner.py)

    def air_posture(self) -> str:
        return self.air or ("offensive" if self.posture == "attack" else "defensive")


class Graph:
    """The objectives in play (no bridges or infrastructure) and the links between them."""

    def __init__(self, theater: Theater, in_play: set[str]) -> None:
        self.objs = theater.objectives
        self.links: dict[tuple[str, str], Link] = {}
        self.adj: dict[str, dict[str, float]] = defaultdict(dict)
        for ln in theater.links:
            if ln.a not in in_play or ln.b not in in_play or ln.a == ln.b:
                continue
            if not self.node(ln.a) or not self.node(ln.b):
                continue
            a, b = self.objs[ln.a].pos, self.objs[ln.b].pos
            length = ln.length_m or OFF_ROAD_FACTOR * math.hypot(a.x - b.x, a.y - b.y)
            if ln.b in self.adj[ln.a] and self.adj[ln.a][ln.b] <= length:
                continue
            self.links[(ln.a, ln.b)] = ln
            self.links.pop((ln.b, ln.a), None)
            self.adj[ln.a][ln.b] = self.adj[ln.b][ln.a] = length

    def node(self, oid: str) -> bool:
        o = self.objs.get(oid)
        return o is not None and o.area_of is None and o.type is not ObjectiveType.BRIDGE

    def length(self, a: str, b: str) -> float:
        return self.adj[a][b]

    def polyline(self, a: str, b: str) -> list[Pos]:
        """From a to b, along the DCS road route if there is one."""
        ln = self.links.get((a, b))
        pts = list(ln.route) if ln else list(reversed(self.links[(b, a)].route))
        return [self.objs[a].pos] + pts + [self.objs[b].pos]

    def point_along(self, a: str, b: str, m: float) -> Pos:
        pts = self.polyline(a, b)
        total = sum(_d(p, q) for p, q in zip(pts, pts[1:]))
        want = max(0.0, min(1.0, m / self.length(a, b))) * total
        for p, q in zip(pts, pts[1:]):
            seg = _d(p, q)
            if want <= seg and seg > 0:
                k = want / seg
                return Pos(p.x + (q.x - p.x) * k, p.y + (q.y - p.y) * k)
            want -= seg
        return pts[-1]

    def progress(self, a: str, b: str, pos: Pos) -> float:
        """How far along the leg from a to b (in the leg's metres) the nearest point to pos is."""
        pts = self.polyline(a, b)
        total = sum(_d(p, q) for p, q in zip(pts, pts[1:])) or 1.0
        best, best_d, run = 0.0, math.inf, 0.0
        for p, q in zip(pts, pts[1:]):
            seg = _d(p, q)
            k = 0.0 if seg == 0 else max(0.0, min(1.0, ((pos.x - p.x) * (q.x - p.x) + (pos.y - p.y) * (q.y - p.y))
                                                     / (seg * seg)))
            c = Pos(p.x + (q.x - p.x) * k, p.y + (q.y - p.y) * k)
            if _d(c, pos) < best_d:
                best, best_d = run + k * seg, _d(c, pos)
            run += seg
        return best / total * self.length(a, b)


def _d(a: Pos, b: Pos) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


class GroundWar:
    """A ``server.System``: runs the ground war on the campaign state."""
    name = "ground_war"

    def __init__(self, state: CampaignState, theater: Theater, sides: dict[Side, SideOrders], *,
                 seed: int = 1, notify: Notify | None = None, call: Call | None = None,
                 in_play: set[str] | None = None) -> None:
        self.state = state
        self.g = Graph(theater, in_play if in_play is not None else set(state.objectives))
        self.objs = theater.objectives
        self.sides = sides
        self.seed = seed
        self.notify = notify or (lambda text: None)
        self.call = call
        self.next_command = 0.0
        self.routed: dict[str, tuple] = {}   # unit id -> the DCS route last sent to its group
        for u in state.ground_units.values():
            if not u.full:
                u.full = strength(u)
            if u.role is GroundRole.GARRISON and not u.ring_m and u.location_id in self.objs:
                # Saved before garrisons stood on the edge: site them again on the next spawn.
                u.ring_m = edge_m(self.objs[u.location_id])
                u.sited, u.spots = False, []
            if u.role is GroundRole.GARRISON and u.spawn is SpawnState.AGGREGATED:
                _mix_old_garrison(u)

    # ---------- System ----------

    def export(self) -> dict[str, Any]:
        return {"next_command": self.next_command}

    def restore(self, data: dict[str, Any]) -> None:
        self.next_command = float(data.get("next_command", 0.0))

    def on_hello(self, hello, new_run: bool) -> None:
        self.routed.clear()

    def on_event(self, data: dict[str, Any]) -> None:
        pass

    def advance(self, t0: float, t1: float) -> None:
        t = t0
        while t < t1:
            step = min(t1 - t, 300.0)
            self._move(step)
            self._deploy()
            self._undefended()
            t += step
            if math.floor(t / HOUR_S) > math.floor((t - step) / HOUR_S):
                self._hour(int(t // HOUR_S))
            if t >= self.next_command:
                self.next_command = t + COMMAND_S
                self.command()

    async def tick(self) -> None:
        if self.call is None:
            return
        for u in self.state.ground_units.values():
            if u.spawn is not SpawnState.SPAWNED or not u.dcs_group:
                self.routed.pop(u.id, None)
                continue
            r = self._dcs_route(u)
            if r is None:
                continue
            want, action = r
            # Keyed by where it's going, not where it is, so a moving group isn't re-sent every tick.
            key = (u.dcs_group, round(want[-1].x), round(want[-1].y))
            if self.routed.get(u.id) == key:
                continue
            self.routed[u.id] = key
            try:
                await self.call("set_route", {"group": u.dcs_group, "points": [
                    {"x": round(p.x, 1), "y": round(p.y, 1), "action": action, "type": "Turning Point",
                     "speed": MARCH_MS, "speed_locked": True, "ETA": 0, "ETA_locked": False} for p in want]})
            except Exception as e:  # noqa: BLE001 - tried again on the next tick
                log.warning("route for %s failed: %s", u.id, e)
                self.routed.pop(u.id, None)

    def _dcs_route(self, u: GroundUnit) -> tuple[list[Pos], str] | None:
        """Where a spawned formation should drive, and how (None: nowhere)."""
        if u.role not in MANEUVER:
            return None
        if u.route and u.leg_from:
            nxt = u.route[0]
            end = self._leg_end(u, nxt)
            return [u.pos, self.g.point_along(u.leg_from, nxt, end)], "On Road"
        if u.battle and u.side is not self.owner(u.battle) and u.leg_from:
            # Attacking: into the objective, across country.
            return [u.pos, self.objs[u.battle].pos], "Off Road"
        if u.post is not None and u.location_id and _d(u.pos, u.post) > 200:
            return [u.pos, u.post], "Off Road"
        return None

    # ---------- helpers ----------

    def owner(self, oid: str) -> Side:
        os_ = self.state.objectives.get(oid)
        return os_.owner if os_ else Side.NEUTRAL

    def oname(self, oid: str) -> str:
        return self.objs[oid].name

    def side_name(self, side: Side) -> str:
        o = self.sides.get(side)
        return o.name if o and o.name else side.name.lower()

    def units(self, side: Side | None = None) -> list[GroundUnit]:
        return [u for u in self.state.ground_units.values() if strength(u) > 0 and (side is None or u.side is side)]

    def maneuver(self, side: Side | None = None) -> list[GroundUnit]:
        return [u for u in self.units(side) if u.role in MANEUVER and u.order is not GroundOrder.HOLD]

    def at(self, oid: str, side: Side | None = None) -> list[GroundUnit]:
        """Formations standing at an objective (not moving, not attacking it)."""
        return [u for u in self.units(side) if u.location_id == oid and not u.route]

    def front(self, side: Side) -> list[str]:
        """Enemy objectives next to ours."""
        enemy = _enemy(side)
        return sorted({n for o in self.g.adj if self.owner(o) is side for n in self.g.adj[o] if self.owner(n) is enemy})

    def front_links(self) -> list[tuple[str, str]]:
        return sorted((a, b) for (a, b) in self.g.links
                      if {self.owner(a), self.owner(b)} == {Side.BLUE, Side.RED})

    def dist(self, a: str, b: str) -> float:
        return _d(self.objs[a].pos, self.objs[b].pos)

    def path(self, a: str, b: str, side: Side) -> list[str] | None:
        """Shortest road path from a to b through objectives the side holds (b may be the enemy's)."""
        best, prev, q = {a: 0.0}, {}, [(0.0, a)]
        while q:
            d, n = heapq.heappop(q)
            if n == b:
                out = [b]
                while out[-1] != a:
                    out.append(prev[out[-1]])
                return out[::-1]
            if d > best[n]:
                continue
            for m, w in self.g.adj[n].items():
                if (self.owner(m) is not side and m != b) or d + w >= best.get(m, math.inf):
                    continue
                best[m], prev[m] = d + w, n
                heapq.heappush(q, (d + w, m))
        return None

    def path_m(self, p: list[str]) -> float:
        return sum(self.g.length(a, b) for a, b in zip(p, p[1:]))

    def fighter(self, u: GroundUnit) -> Fighter:
        return Fighter(u.id, u.composition, u.full or strength(u), u.supply, u.morale)

    def garrison(self, oid: str) -> GroundUnit | None:
        """The local troops holding an objective, if any are left."""
        owner = self.owner(oid)
        return next((u for u in self.units(owner) if u.role is GroundRole.GARRISON and u.location_id == oid), None)

    def terrain(self, oid: str) -> str:
        return {ObjectiveType.CITY: "town", ObjectiveType.AIRBASE: "airbase"}.get(self.objs[oid].type, "open")

    def defence(self, oid: str) -> float:
        """Combat power holding an objective, with its terrain bonus."""
        power = sum(self.fighter(u).power() for u in self.at(oid, self.owner(oid))
                    if u.role in MANEUVER and u.role is not GroundRole.ARTILLERY)
        g = self.garrison(oid)
        power += self.fighter(g).power() if g else 0.0
        return power * self.cover(oid)

    def cover(self, oid: str) -> float:
        fire, cover = combat.POSTURE[self.terrain(oid)]
        return fire * cover

    def idle(self, side: Side, fresh: bool = True) -> list[GroundUnit]:
        now = self.state.clock
        smin, mmin, supmin = FRESH
        return [u for u in self.maneuver(side) if not u.route and u.battle is None and u.location_id
                and (not fresh or (strength(u) / (u.full or 1) >= smin and u.morale >= mmin
                                   and u.supply >= supmin and now >= u.rest_until))]

    def _place(self, u: GroundUnit, pos: Pos) -> None:
        if pos != u.pos:
            u.pos = pos
            u.sited, u.spots = False, []

    def _settle(self, u: GroundUnit, oid: str) -> None:
        """Stand at an objective, spread out from the others there."""
        others = [v for v in self.at(oid) if v.id != u.id and v.role in MANEUVER]
        u.location_id, u.leg_from, u.leg_m, u.route, u.post = oid, None, 0.0, [], None
        k = len(others) + 1
        c = self.objs[oid].pos
        a, r = math.radians(k * 137.5 + 45), 1_500 * math.sqrt(k)
        self._place(u, Pos(c.x + r * math.cos(a), c.y + r * math.sin(a)))

    def predict(self, u: GroundUnit, dt: float) -> Pos:
        """Where a formation will be in dt seconds if it keeps marching (it is where it is otherwise)."""
        if not u.route or not u.leg_from or u.battle:
            return u.pos
        left, frm, m = MARCH_MS * max(0.0, dt), u.leg_from, u.leg_m
        for nxt in u.route:
            if nxt not in self.g.adj.get(frm, {}):
                break
            length = self.g.length(frm, nxt)
            end = max(0.0, length - STANDOFF_M) if self.owner(nxt) is not u.side else length
            if m + left <= end or nxt == u.route[-1] or self.owner(nxt) is not u.side:
                return self.g.point_along(frm, nxt, min(end, m + left))
            left -= end - m
            frm, m = nxt, 0.0
        return u.pos

    def _leg_end(self, u: GroundUnit, nxt: str) -> float:
        """Where the leg to nxt ends: at it, or short of it if it's the enemy's."""
        length = self.g.length(u.leg_from, nxt)
        return max(0.0, length - STANDOFF_M) if self.owner(nxt) is not u.side else length

    def order(self, u: GroundUnit, path: list[str], order: GroundOrder) -> None:
        """Send a formation along a path of objectives (path[0] is where it is)."""
        if len(path) < 2:
            return
        u.route = list(path[1:])
        u.leg_from = path[0]
        u.leg_m = 0.0
        u.location_id = None
        u.post = None
        u.order = order
        u.order_target = path[-1]

    # ---------- movement ----------

    def _move(self, dt: float) -> None:
        for u in sorted(self.units(), key=lambda u: u.id):
            if not u.route or not u.leg_from or u.battle:
                continue
            nxt = u.route[0]
            if nxt not in self.g.adj.get(u.leg_from, {}):
                u.route, u.leg_from = [], None
                continue
            end = self._leg_end(u, nxt)
            if u.spawn is SpawnState.SPAWNED:
                u.leg_m = max(u.leg_m, min(end, self.g.progress(u.leg_from, nxt, u.pos)))
            else:
                u.leg_m = min(end, u.leg_m + MARCH_MS * dt)
                self._place(u, self.g.point_along(u.leg_from, nxt, u.leg_m))
            u.supply = max(0.0, u.supply - 0.005 * dt / HOUR_S)
            if u.leg_m >= end - (300 if u.spawn is SpawnState.SPAWNED else 1e-6):
                self._arrive(u, nxt)

    def _arrive(self, u: GroundUnit, nxt: str) -> None:
        prev = u.leg_from
        owner = self.owner(nxt)
        if owner is u.side:
            u.route.pop(0)
            if u.route:
                u.leg_from, u.leg_m = nxt, 0.0
                return
            self._settle(u, nxt)
            if u.order is not GroundOrder.RETREAT and nxt in self.state.battles:
                u.battle = nxt   # reinforcing the defence
            u.order = GroundOrder.DEFEND
            return
        if owner is Side.NEUTRAL:
            u.route, u.leg_from = [], None
            return
        # The enemy's: attack from where it stands, short of the objective.
        u.route = []
        defended = [v for v in self.at(nxt, owner) if v.role in MANEUVER] or self.garrison(nxt)
        if not defended:
            self._capture(nxt, u.side)
            self._settle(u, nxt)
            u.rest_until = self.state.clock + REST_S
            u.order = GroundOrder.DEFEND
            return
        b = self.state.battles.get(nxt)
        if b is None:
            b = self.state.battles[nxt] = Battle(nxt, u.side, self.state.clock)
            self.notify(f"{self.side_name(u.side)} attacks {self.oname(nxt)}")
        b.origin[u.id] = prev or nxt
        u.battle = nxt
        u.order = GroundOrder.ATTACK

    # ---------- defence: posts on the edge of a threatened objective ----------

    def threat_from(self, oid: str) -> str | None:
        """The neighbour the enemy is coming from (or is strongest at), if any."""
        side = self.owner(oid)
        enemy = _enemy(side)
        power: dict[str, float] = defaultdict(float)
        for u in self.units(enemy):
            if u.role not in MANEUVER:
                continue
            if u.battle == oid or (u.route and u.route[-1] == oid):
                came = self.state.battles[oid].origin.get(u.id) if oid in self.state.battles else None
                src = came or (u.leg_from if u.route and u.route[0] == oid else None)
                if src is None and u.route:
                    i = u.route.index(oid)
                    src = u.route[i - 1] if i > 0 else u.leg_from
                if src in self.g.adj.get(oid, {}):
                    power[src] += 2 * self.fighter(u).power()   # on its way here: weighs more
            elif u.location_id in self.g.adj.get(oid, {}):
                power[u.location_id] += self.fighter(u).power()
        return max(sorted(power), key=lambda n: power[n]) if power else None

    def _deploy(self) -> None:
        """Formations at a threatened objective take posts on its edge, across the road the threat
        comes on; with no threat they stay where they are."""
        holders: dict[str, list[GroundUnit]] = defaultdict(list)
        for u in self.units():
            if u.role in FRONTLINE and u.location_id and not u.route and u.order is not GroundOrder.HOLD \
                    and not (u.battle and u.battle != u.location_id):
                holders[u.location_id].append(u)
        for oid, us in holders.items():
            nb = self.threat_from(oid)
            if nb is None:
                continue
            c = self.objs[oid].pos
            p = self.g.point_along(oid, nb, edge_m(self.objs[oid]))
            dx, dy = p.x - c.x, p.y - c.y
            n = math.hypot(dx, dy) or 1.0
            px, py = -dy / n, dx / n   # across the road
            us = sorted(us, key=lambda u: u.id)
            for i, u in enumerate(us):
                off = (i - (len(us) - 1) / 2) * DEFEND_SPREAD_M
                post = Pos(round(p.x + px * off, 1), round(p.y + py * off, 1))
                if u.post == post:
                    continue
                u.post = post
                if u.spawn is not SpawnState.SPAWNED:
                    self._place(u, post)

    def _undefended(self) -> None:
        """A battle whose defence is all gone (e.g. killed in DCS) is won now, not at the next hour."""
        for at in sorted(self.state.battles):
            b = self.state.battles[at]
            owner = self.owner(at)
            if owner is b.attacker:
                continue
            att = [u for u in self.units(b.attacker) if u.battle == at and u.role is not GroundRole.ARTILLERY]
            dfd = [u for u in self.at(at, owner) if u.role in MANEUVER and u.role is not GroundRole.ARTILLERY]
            if att and not dfd and not self.garrison(at):
                self._win(b, [u for u in self.units(b.attacker) if u.battle == at])

    # ---------- hourly: fights, recovery ----------

    def _hour(self, hour: int) -> None:
        for at in sorted(self.state.battles):
            self._fight(at, random.Random(f"{self.seed}:{hour}:{at}"))
        self._recover()
        self._disband()

    def _fight(self, at: str, rng: random.Random) -> None:
        b = self.state.battles[at]
        owner = self.owner(at)
        if owner is b.attacker:   # taken some other way
            self._end_battle(b)
            return
        att = [u for u in self.units(b.attacker) if u.battle == at]
        arriving = [u for u in self.units(b.attacker) if u.route and u.route[-1] == at]
        if not att:
            if not arriving:
                self.notify(f"{self.side_name(b.attacker)} attack on {self.oname(at)} fails")
                self._end_battle(b)
            return
        dfd = [u for u in self.at(at, owner) if u.role in MANEUVER]
        for d in dfd:
            d.battle = at
        g = self.garrison(at)
        live = {u.id for u in att + dfd + ([g] if g else []) if u.spawn is SpawnState.SPAWNED}

        def support(side: Side, extra: list[GroundUnit] = ()) -> list[Fighter]:
            guns = [u for u in self.units(side) if u.role is GroundRole.ARTILLERY and not u.route
                    and u.battle is None and u.location_id and (u.location_id == at or at in self.g.adj.get(u.location_id, {}))]
            return [Fighter(u.id, dict(u.composition), u.full or strength(u), u.supply, u.morale, True)
                    for u in guns + list(extra)]

        # Formations in DCS fight with copies: their losses come from DCS, not from here.
        def f_of(u: GroundUnit) -> Fighter:
            f = self.fighter(u)
            return Fighter(f.id, dict(f.composition), f.full, f.supply, f.morale) if u.id in live else f

        a_f = [f_of(u) for u in att if u.role is not GroundRole.ARTILLERY] + support(b.attacker)
        d_main = [f_of(u) for u in dfd if u.role is not GroundRole.ARTILLERY]
        d_f = d_main + ([f_of(g)] if g else []) + support(owner, [u for u in dfd if u.role is GroundRole.ARTILLERY])
        if not d_main and not g:
            self._win(b, att)
            return
        b.hours += 1
        res = combat.ground_hour(a_f, d_f, self.terrain(at), rng)
        by_id = {u.id: u for u in att + dfd + ([g] if g else [])}
        for f in a_f + d_f:
            u = by_id.get(f.id)
            if u is not None and u.id not in live and not f.support:
                u.morale, u.supply = f.morale, f.supply
                u.composition = {t: n for t, n in u.composition.items() if n > 0}
        broken = set(res.broken)
        # In DCS: broken by losses alone.
        for u in by_id.values():
            if u.id in live and strength(u) / (u.full or 1) < combat.GroundConfig().break_strength:
                broken.add(u.id)
        for fid in sorted(broken):
            if g is not None and fid == g.id:
                self.notify(f"the {self.side_name(owner)} garrison of {self.oname(at)} gives up")
                g.composition = {}
            elif fid in by_id and by_id[fid].role is not GroundRole.ARTILLERY:
                self._retreat(by_id[fid], b)
        att = [u for u in self.units(b.attacker) if u.battle == at]
        dfd = [u for u in self.units(owner) if u.battle == at and u.role is not GroundRole.ARTILLERY]
        if att and not dfd and not self.garrison(at):
            self._win(b, att)
            return
        if not att and not [u for u in self.units(b.attacker) if u.route and u.route[-1] == at]:
            self.notify(f"{self.side_name(b.attacker)} attack on {self.oname(at)} fails after {b.hours} h")
            self._end_battle(b)
        elif b.hours >= combat.GroundConfig().max_hours:
            for u in att:
                self._retreat(u, b)
            self.notify(f"{self.side_name(b.attacker)} calls off the attack on {self.oname(at)} after {b.hours} h")
            self._end_battle(b)
        else:
            self._progress(b, live)

    def strengths(self, b: Battle) -> tuple[float, float]:
        """Attackers' and defenders' strength now, as a fraction of full."""
        owner = self.owner(b.at)
        att = [u for u in self.units(b.attacker) if u.battle == b.at and u.role is not GroundRole.ARTILLERY]
        dfd = [u for u in self.at(b.at, owner) if u.role in FRONTLINE or u.role is GroundRole.GARRISON]

        def frac(us: list[GroundUnit]) -> float:
            full = sum(u.full or strength(u) for u in us)
            return sum(strength(u) for u in us) / full if full else 0.0
        return frac(att), frac(dfd)

    def _progress(self, b: Battle, live: set[str]) -> None:
        """The hourly report on a battle: in DCS messages when players are near it, else the log."""
        a, d = self.strengths(b)
        text = (f"{self.oname(b.at)}, hour {b.hours}: {self.side_name(b.attacker)} attackers at {a:.0%}, "
                f"{self.side_name(self.owner(b.at))} defenders at {d:.0%}")
        if live:
            self.notify(text)
        else:
            log.info("WAR %s", text)

    def _win(self, b: Battle, att: list[GroundUnit]) -> None:
        for d in self.at(b.at, self.owner(b.at)):
            if d.role in MANEUVER:
                self._retreat(d, b)
        self.notify(f"{self.side_name(b.attacker)} wins at {self.oname(b.at)} after {b.hours} h")
        self._capture(b.at, b.attacker)
        for u in att:
            u.battle = None
            self._settle(u, b.at)
            u.rest_until = self.state.clock + REST_S
            u.order = GroundOrder.DEFEND

    def _end_battle(self, b: Battle) -> None:
        for u in self.units():
            if u.battle == b.at:
                u.battle = None
                if u.side is b.attacker and not u.route and u.location_id is None:
                    self._retreat(u, b, quiet=True)
        self.state.battles.pop(b.at, None)

    def _retreat(self, u: GroundUnit, b: Battle, quiet: bool = False) -> None:
        u.battle = None
        if u.side is b.attacker:
            back = b.origin.get(u.id) or u.leg_from
            b.origin.pop(u.id, None)
            u.rest_until = self.state.clock + REST_S
            if back and back != b.at and self.owner(back) is u.side and back in self.g.adj.get(b.at, {}):
                # Back along the link it came on: it stands short of the objective.
                u.route, u.leg_from = [back], b.at
                u.leg_m = STANDOFF_M
                u.location_id, u.order, u.order_target = None, GroundOrder.RETREAT, back
            elif back and self.owner(back) is u.side:
                self._settle(u, back)
            if not quiet:
                self.notify(f"{u.name} breaks off at {self.oname(b.at)} "
                            f"({strength(u)} vehicles left)")
            return
        options = [n for n in self.g.adj.get(u.location_id or b.at, {}) if self.owner(n) is u.side]
        if not options:
            self.notify(f"{u.name} is cut off at {self.oname(b.at)} and surrenders")
            u.composition = {}
            return
        enemy = [o for o in self.g.adj if self.owner(o) is b.attacker]
        dest = max(options, key=lambda n: min((self.dist(n, e) for e in enemy), default=0.0))
        self.order(u, [u.location_id or b.at, dest], GroundOrder.RETREAT)
        self.notify(f"{u.name} retreats from {self.oname(b.at)} to {self.oname(dest)}")

    def _capture(self, oid: str, side: Side) -> None:
        st = self.state
        loser = st.objectives[oid].owner
        taken = [oid] + [o for o in st.objectives if self.objs[o].area_of == oid]
        for o in taken:
            st.objectives[o].owner = side
        for u in self.units(loser):
            if u.role is GroundRole.GARRISON and u.location_id == oid:
                u.composition = {}
        garrison_unit(st, self.objs[oid], side, occupiers(side))
        st.battles.pop(oid, None)
        n = len(taken) - 1
        extra = f" (and {n} site{'s' if n > 1 else ''} in its area)" if n else ""
        self.notify(f"{self.side_name(side)} takes {self.oname(oid)}{extra}")
        for u in self.units(loser):
            if u.location_id == oid and u.role in STATIC_ROLES:
                u.composition = {}
                self.notify(f"{u.name} is overrun")
        for sq in st.squadrons.values():
            if sq.base_id != oid or sq.side is side:
                continue
            bases = [b for b, s in st.objectives.items() if s.owner is sq.side and b in self.g.adj
                     and self.objs[b].type is ObjectiveType.AIRBASE]
            enemy = [o for o in self.g.adj if self.owner(o) not in (sq.side, Side.NEUTRAL)]
            safe = [b for b in bases if min((self.dist(b, e) for e in enemy), default=math.inf) > 25 * NM] or bases
            if safe:
                sq.base_id = min(safe, key=lambda b: self.dist(b, oid))
                self.notify(f"{sq.name} moves to {self.oname(sq.base_id)}")
            else:
                self.notify(f"{sq.name} has no base left: {sq.aircraft_ready} aircraft lost")
                sq.aircraft_ready = 0

    def _recover(self) -> None:
        sources = {s: [o for o, os_ in self.state.objectives.items() if os_.owner is s and o in self.g.adj
                       and self.objs[o].type in (ObjectiveType.AIRBASE, ObjectiveType.DEPOT)]
                   for s in (Side.BLUE, Side.RED)}
        for u in self.units():
            if u.role not in MANEUVER or u.battle or u.route:
                continue
            u.morale = min(1.0, u.morale + 0.02)
            src = sources.get(u.side) or []
            here = u.location_id
            if here and src:
                d = min(self.dist(here, s) for s in src)
                u.supply = min(1.0, u.supply + 0.03 * max(0.2, 1 - d * 1.3 / SUPPLY_REACH_M))
        for u in self.units():
            if u.role is GroundRole.GARRISON and u.location_id not in self.state.battles:
                u.morale = min(1.0, u.morale + 0.02)

    def _disband(self) -> None:
        for u in self.units():
            if u.role in MANEUVER and u.role is not GroundRole.ARTILLERY and u.spawn is not SpawnState.SPAWNED \
                    and strength(u) / (u.full or 1) < DISBAND_STRENGTH:
                self.notify(f"{u.name} is destroyed as a unit ({strength(u)} vehicles left)")
                u.composition = {}

    # ---------- commanders ----------

    def command(self) -> None:
        for side in (Side.BLUE, Side.RED):
            o = self.sides.get(side)
            if o is None:
                continue
            if o.posture == "attack":
                self._attacker(side, o)
            else:
                self._defender(side)

    def _goal_dist(self, side: Side, oid: str) -> float:
        goal = self.sides[side].goal
        return self.dist(oid, goal) if goal and goal in self.objs else 0.0

    def _attacker(self, side: Side, o: SideOrders) -> None:
        free = {u.id: u for u in self.idle(side) if u.role is not GroundRole.ARTILLERY}
        targets = sorted(self.front(side), key=lambda n: -(self.objs[n].priority
                                                            + (3 if self.objs[n].type is ObjectiveType.AIRBASE else 0)
                                                            - self._goal_dist(side, n) / NM / 10))
        busy = {u.order_target for u in self.units(side) if u.route} | set(self.state.battles)
        for t in targets:
            if t in busy:
                continue
            need = ATTACK_ODDS * self.defence(t)
            near = sorted((u for u in free.values() if t in self.g.adj.get(u.location_id, {})),
                          key=lambda u: -self.fighter(u).power())
            group, power = [], 0.0
            for u in near:
                if power >= need and group:
                    break
                group.append(u)
                power += self.fighter(u).power()
            if group and power >= need:
                for u in group:
                    self.order(u, [u.location_id, t], GroundOrder.ATTACK)
                    del free[u.id]
                busy.add(t)
                log.info("%s attacks %s with %d formation(s) (%.0f vs %.0f)", self.side_name(side), self.oname(t), len(group),
                         power, need / ATTACK_ODDS)
        # Gather for the best target it can't reach yet but could with everything free.
        total = sum(self.fighter(u).power() for u in free.values())
        for t in targets:
            need = ATTACK_ODDS * self.defence(t)
            if t in busy or not need or total < need:
                continue
            stage = min((n for n in self.g.adj[t] if self.owner(n) is side), key=lambda n: self.dist(n, t), default=None)
            if stage is None:
                continue
            for u in sorted(free.values(), key=lambda u: (self.dist(u.location_id, stage), u.id)):
                if u.location_id == stage or t in self.g.adj.get(u.location_id, {}):
                    continue
                p = self.path(u.location_id, stage, side)
                if p and len(p) > 1:
                    self.order(u, p, GroundOrder.MOVE)
                    del free[u.id]
            log.info("%s gathers for %s at %s", self.side_name(side), self.oname(t), self.oname(stage))
            break
        # Everyone else (and the guns) moves up to the front.
        enemy_front = self.front(_enemy(side))
        if not enemy_front:
            return
        for u in list(free.values()) + [u for u in self.idle(side, fresh=False) if u.role is GroundRole.ARTILLERY]:
            if any(self.owner(n) is _enemy(side) for n in self.g.adj.get(u.location_id, {})):
                continue
            goal = min(enemy_front, key=lambda f: self._goal_dist(side, f) + self.dist(f, u.location_id) / 2)
            p = self.path(u.location_id, goal, side)
            if p and len(p) > 1:
                self.order(u, p, GroundOrder.MOVE)

    def _defender(self, side: Side) -> None:
        enemy = _enemy(side)
        mine = self.sides[side].countries

        def own_ground(oid: str) -> bool:
            return not mine or self.objs[oid].country in mine

        free = {u.id: u for u in self.idle(side) if u.role is not GroundRole.ARTILLERY}
        busy = set(self.state.battles) | {u.order_target for u in self.units(side) if u.route}
        # Counterattack: retake our own ground wherever we can get 2:1 on it, most valuable first.
        # A spearhead that left its rear to garrisons gets cut off this way.
        targets = sorted((o for o in self.front(side) if own_ground(o) and o not in busy),
                         key=lambda o: (-self.objs[o].priority, o))
        for oid in targets:
            need = ATTACK_ODDS * self.defence(oid)
            near = sorted((u for u in free.values() if oid in self.g.adj.get(u.location_id, {})),
                          key=lambda u: (-self.fighter(u).power(), u.id))
            group, power = [], 0.0
            for u in near:
                # Leave one formation on any objective the enemy is next to.
                stay = [v for v in self.at(u.location_id, side) if v.role in MANEUVER and v.id not in
                        {g.id for g in group}]
                if len(stay) <= 1 and any(self.owner(n) is enemy for n in self.g.adj.get(u.location_id, {})
                                          if n != oid):
                    continue
                if power >= need and group:
                    break
                group.append(u)
                power += self.fighter(u).power()
            if group and power >= need:
                for u in group:
                    self.order(u, [u.location_id, oid], GroundOrder.ATTACK)
                    del free[u.id]
                busy.add(oid)
                log.info("%s counterattacks %s with %d formation(s)", self.side_name(side), self.oname(oid), len(group))
        # Reserves: to each threatened objective on the front, the nearest by road, until its
        # defence matches the enemy next to it. No long detours round enemy ground.
        threat: dict[str, float] = {}
        for oid in self.front(enemy):
            near = sum(self.fighter(u).power() for u in self.units(enemy) if u.role in FRONTLINE
                       and (u.location_id in self.g.adj.get(oid, {}) or (u.route and u.route[-1] == oid)
                            or u.battle == oid))
            coming = sum(self.fighter(u).power() for u in self.units(side) if u.route and u.route[-1] == oid)
            short = near - self.defence(oid) - coming * self.cover(oid)
            if short > 0:
                threat[oid] = short
        for oid in sorted(threat, key=lambda k: (-threat[k], k)):
            options = []
            for u in free.values():
                if u.location_id in threat:
                    continue   # needed where it is
                p = self.path(u.location_id, oid, side)
                if not p or len(p) < 2:
                    continue
                road = self.path_m(p)
                if road > REINFORCE_MAX_M or road > REINFORCE_DETOUR * self.dist(u.location_id, oid) + 10_000:
                    continue
                options.append((road, u.id, u, p))
            for road, _, u, p in sorted(options, key=lambda t: t[:2]):
                if threat[oid] <= 0:
                    break
                self.order(u, p, GroundOrder.MOVE)
                del free[u.id]
                threat[oid] -= self.fighter(u).power() * self.cover(oid)
                log.info("%s sends %s to %s (%.0f km by road)", self.side_name(side), u.name, self.oname(oid), road / 1000)

    # ---------- reporting ----------

    def status_lines(self) -> list[str]:
        out = []
        fl = self.front_links()
        def battle(b: Battle) -> str:
            a, d = self.strengths(b)
            return (f"{self.oname(b.at)} ({self.side_name(b.attacker)} attacking, {b.hours} h; "
                    f"attackers {a:.0%}, defenders {d:.0%})")
        out.append(f"Front: {len(fl)} links; battles: " + (", ".join(
            battle(b) for b in self.state.battles.values()) or "none"))
        moving = [u for u in self.maneuver() if u.route]
        if moving:
            out.append("Moving: " + ", ".join(f"{u.name} to {self.oname(u.order_target or u.route[-1])}"
                                              for u in moving[:8]) + (f" (+{len(moving) - 8})" if len(moving) > 8 else ""))
        return out


def _enemy(side: Side) -> Side:
    return Side.RED if side is Side.BLUE else Side.BLUE


def sides_from(sd) -> dict[Side, SideOrders]:
    """The commanders' orders from a scenario definition's [sides.*]."""
    return {side: SideOrders(posture=v.get("posture", "attack" if side is Side.BLUE else "defend"),
                             goal=v.get("goal"), name=v.get("name", ""),
                             countries=frozenset(v.get("countries") or ()), air=v.get("air", ""))
            for side, v in sd.sides.items()}


def main() -> None:
    import argparse
    from pathlib import Path

    from .scenario_file import ROOT, build_state, load_scenario_def, scenario_path

    ap = argparse.ArgumentParser(description="Run a scenario's ground war with no DCS.")
    ap.add_argument("name")
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--quiet", action="store_true", help="only the hourly summary")
    a = ap.parse_args()
    sd = load_scenario_def(scenario_path(a.name))
    built = build_state(sd)
    st = built.state
    events: list[str] = []

    def note(text: str) -> None:
        line = f"T+{int(st.clock // 3600):3d}h{int(st.clock % 3600 // 60):02d}  {text}"
        events.append(line)
        if not a.quiet:
            print(line, flush=True)

    war = GroundWar(st, built.theater, sides_from(sd), seed=a.seed, notify=note, in_play=built.in_play)
    step = 60.0
    for i in range(int(a.hours * 3600 / step)):
        t0 = st.clock
        st.clock += step
        war.advance(t0, st.clock)
        if st.clock % (24 * 3600) == 0:
            held = {s: sum(1 for o in st.objectives.values() if o.owner is s) for s in (Side.BLUE, Side.RED)}
            veh = {s: sum(strength(u) for u in war.units(s) if u.role in MANEUVER) for s in (Side.BLUE, Side.RED)}
            print(f"--- day {int(st.clock // 86400)}: blue holds {held[Side.BLUE]}, red {held[Side.RED]}; "
                  f"maneuver vehicles blue {veh[Side.BLUE]}, red {veh[Side.RED]}; front {len(war.front_links())} links; "
                  f"battles {len(st.battles)}", flush=True)
    from tools.draw_scenario import draw
    path = ROOT / "build" / "war" / f"{sd.name}-{int(a.hours)}h.png"
    draw(path, sd, built, title=f"{sd.title}: T+{int(a.hours)}h (seed {a.seed})")
    out = ROOT / "build" / "war" / f"{sd.name}-{int(a.hours)}h.log"
    out.write_text("\n".join(events) + "\n")
    print(f"picture: {path}\nevents: {out}")


if __name__ == "__main__":
    main()
