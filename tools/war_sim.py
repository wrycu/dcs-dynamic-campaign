"""Proof P7, part 2: run a war around Khalkhalah with no DCS, on the map layer from P6.

    python3 -m tools.war_sim [--days 14] [--seed 1] [--verbose] [--theater build/theater/syria-probe.json]

Blue, a superpower, pushes north from Jordan and Khalkhalah towards Damascus; red, a minor
country, defends with older armour, SAMs and MiGs. Each hour the ground war moves and fights
(campaign/combat.py); three times a day each side flies its air missions. Simple commanders give
the orders: blue attacks where it has 2:1, red holds, reinforces and counterattacks weak units.

Writes build/war/war.json (daily figures and events) and build/war/war.png (who holds what at
the end, and where the front started). A fixed --seed gives the same war.
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from campaign import combat
from campaign.combat import AirSide, Fighter

NM = 1852.0
BLUE, RED = "blue", "red"
OTHER = {BLUE: RED, RED: BLUE}
LANDMARK = re.compile(r"Mosque|Ministry|Tower|Cathedral|Hotel|Bank|Palace|Temple|Stadium|University|"
                      r"Hospital|Church|Suburb|Square", re.I)
SPEED_MS = 20_000 / 3600  # a formation's road march, halts included
AIR_CYCLES = (7, 12, 17)  # hours of the day each side flies
AVAILABILITY = 0.7        # fraction of an air force's aircraft ready for a cycle
ATTACK_ODDS = 2.0         # attack only with this much power over the defence (terrain included)
REST_H = 6                # hours a formation regroups after an attack before the next one
SUPPLY_REACH_M = 150_000  # resupply fades to a fifth this far by road from a depot
RED_FRONT_BNS = 8         # red mech battalions on its front line at the start
REPAIRABLE = {"ground": 0.4, "air": 0.25}  # share of knocked-out vehicles that can be repaired
REPAIR_H = 48             # hours a repairable vehicle takes to come back (unit supplied, not fighting)


# ---------- theater ----------

@dataclass
class Obj:
    name: str
    kind: str  # "airbase" | "town"
    x: float
    y: float
    size: int = 0
    owner: str = BLUE
    start_owner: str = BLUE
    garrison: Fighter | None = None  # local infantry and guns; never moves, gives up when broken


def in_play(xr: float, yr: float) -> bool:
    """Syria and Jordan only, in nm from Khalkhalah (north +x, east +y): no Israel or Lebanon."""
    if math.hypot(xr, yr) > 60 or yr < -33 or xr > 50:
        return False
    return not (xr > 40 and yr < -24)


def load_theater(path: Path) -> tuple[dict[str, Obj], dict[tuple[str, str], float], int]:
    """Objectives and road links from the P6 probe output, cleaned the way P6 said to:
    runway-less "airbases" dropped, landmarks merged into their town, links rebuilt."""
    d = json.loads(path.read_text())
    cx, cy = d["center"]["x"], d["center"]["y"]
    rel = lambda x, y: ((x - cx) / NM, (y - cy) / NM)  # noqa: E731
    objs: dict[str, Obj] = {}
    for a in d["airbases"]:
        x, y = a["pos"]["x"], a["pos"]["y"]
        if a["runways"] and in_play(*rel(x, y)):
            objs[a["name"]] = Obj(a["name"], "airbase", x, y)
    alias: dict[str, str] = {n: n for n in objs}
    clusters: list[list[dict]] = []
    for p in sorted(d["places"], key=lambda p: -p["objects"]):
        if p["objects"] < 1200 or not in_play(*rel(p["x"], p["y"])):
            continue
        for c in clusters:
            if math.hypot(c[0]["x"] - p["x"], c[0]["y"] - p["y"]) < 3000:
                c.append(p)
                break
        else:
            clusters.append([p])
    for c in clusters:
        named = [p for p in c if not LANDMARK.search(p["name"])] or c
        name = named[0]["name"]
        if name in objs:
            name += " (town)"
        objs[name] = Obj(name, "town", c[0]["x"], c[0]["y"], sum(p["objects"] for p in c))
        for p in c:
            alias[p["name"]] = name
    road: dict[tuple[str, str], float] = {}
    for link in d["links"]:
        a, b = alias.get(link["a"]), alias.get(link["b"])
        if a and b and a != b and link.get("road_m"):
            key = tuple(sorted((a, b)))
            road[key] = min(road.get(key, math.inf), link["road_m"])
    links: dict[tuple[str, str], float] = {}
    names = sorted(objs)
    dist = lambda a, b: math.hypot(objs[a].x - objs[b].x, objs[a].y - objs[b].y)  # noqa: E731
    for a in names:
        for b in sorted((n for n in names if n != a), key=lambda n: dist(a, n))[:3]:
            if dist(a, b) < 20 * NM:
                key = tuple(sorted((a, b)))
                links[key] = road.get(key, dist(a, b) * 1.4)
    # join any separate pieces at their closest pair
    while True:
        comp = _component(names[0], links)
        rest = [n for n in names if n not in comp]
        if not rest:
            break
        a, b = min(((a, b) for a in comp for b in rest), key=lambda p: dist(*p))
        links[tuple(sorted((a, b)))] = dist(a, b) * 1.4
    from_road = sum(1 for k in links if k in road)
    return objs, links, from_road


def _component(start: str, links: dict[tuple[str, str], float]) -> set[str]:
    adj = defaultdict(set)
    for a, b in links:
        adj[a].add(b)
        adj[b].add(a)
    seen, todo = {start}, [start]
    while todo:
        for n in adj[todo.pop()] - seen:
            seen.add(n)
            todo.append(n)
    return seen


# ---------- forces ----------

@dataclass
class Unit:
    f: Fighter
    side: str
    role: str  # "armor" | "mech" | "artillery"
    at: str    # objective it is at (or left from, while moving)
    dest: str | None = None
    left_m: float = 0.0  # still to go to dest
    retreating: bool = False
    battle: str | None = None  # objective it is fighting at
    rest_until: int = 0  # hour it is ready to attack again (regrouping after a fight)
    repairs: list[tuple[int, str]] = field(default_factory=list)  # (hour it is back, vehicle type)

    @property
    def id(self) -> str:
        return self.f.id


@dataclass
class Squadron:
    side: str
    type: str
    base: str
    aircraft: int


@dataclass
class Sam:
    type: str
    at: str
    down_until: float = -1.0
    destroyed: bool = False


@dataclass
class Battle:
    at: str
    attacker: str
    hours: int = 0
    origin: dict[str, str] = field(default_factory=dict)  # attacking unit id -> where it came from


TANK_BLUE = {"M-1 Abrams": 30, "M1097 Avenger": 2}
MECH_BLUE = {"M-2 Bradley": 30, "M-1 Abrams": 14, "M1097 Avenger": 2}
ARTY_BLUE = {"M-109": 18}
TANK_RED = {"T-72B": 31, "ZSU-23-4 Shilka": 2}
MECH_RED = {"BMP-1": 30, "T-55": 10, "ZSU-23-4 Shilka": 2, "Strela-1 9P31": 2}
ARTY_RED = {"SAU Gvozdika": 18}


def garrison(o: Obj) -> Fighter:
    """Local troops holding an objective: more in bigger towns, more at airbases."""
    if o.kind == "airbase":
        comp = {"Infantry AK": 40, "BTR-80": 4, "ZU-23 Emplacement": 6}
    else:
        comp = {"Infantry AK": 15 + o.size // 150, "BTR-80": 2 + o.size // 1500, "ZU-23 Emplacement": 2}
    return Fighter(f"g:{o.name}", comp, combat.vehicles(comp))


def occupiers(o: Obj) -> Fighter:
    """What the side that takes an objective leaves behind to hold it."""
    comp = {"Infantry AK": 12, "ZU-23 Emplacement": 1}
    return Fighter(f"g:{o.name}", comp, combat.vehicles(comp))


class War:
    def __init__(self, objs: dict[str, Obj], links: dict[tuple[str, str], float], seed: int,
                 verbose: bool = False, red_strength: int = 1) -> None:
        self.red_strength = red_strength
        self.objs, self.links = objs, links
        self.rng = random.Random(seed)
        self.verbose = verbose
        self.adj: dict[str, dict[str, float]] = defaultdict(dict)
        for (a, b), m in links.items():
            self.adj[a][b] = m
            self.adj[b][a] = m
        self.units: dict[str, Unit] = {}
        self.squadrons: list[Squadron] = []
        self.sams: list[Sam] = []
        self.battles: dict[str, Battle] = {}
        self.hour = 0
        self.depots: dict[str, list[str]] = {}
        self.reinforcements: list[tuple[int, Unit]] = []
        self.events: list[str] = []
        self.day = self._new_day()
        self.days: list[dict] = []
        self.full_total = {BLUE: 0, RED: 0}  # every battalion's full strength, including lost ones
        self.capital_fell: int | None = None

    # ----- helpers -----

    def dist(self, a: str, b: str) -> float:
        return math.hypot(self.objs[a].x - self.objs[b].x, self.objs[a].y - self.objs[b].y)

    def units_at(self, obj: str, side: str | None = None) -> list[Unit]:
        return [u for u in self.units.values() if u.at == obj and u.dest is None
                and (side is None or u.side == side)]

    def front(self, side: str) -> list[str]:
        """Objectives of the other side that touch ours."""
        return sorted({n for o in self.objs.values() if o.owner == side
                       for n in self.adj[o.name] if self.objs[n].owner != side})

    def path(self, a: str, b: str, side: str) -> list[str] | None:
        """Shortest road path through objectives the side holds (b may be the enemy's)."""
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
            for m, w in self.adj[n].items():
                if (self.objs[m].owner != side and m != b) or d + w >= best.get(m, math.inf):
                    continue
                best[m], prev[m] = d + w, n
                heapq.heappush(q, (d + w, m))
        return None

    def supplied(self, side: str) -> set[str]:
        out: set[str] = set()
        for depot in self.depots[side]:
            if self.objs[depot].owner == side:
                out |= {n for n in _component(depot, {k: v for k, v in self.links.items()
                                                      if self.objs[k[0]].owner == side
                                                      and self.objs[k[1]].owner == side})}
                out.add(depot)
        return out

    def log(self, text: str) -> None:
        line = f"D{self.hour // 24 + 1} {self.hour % 24:02d}h  {text}"
        self.events.append(line)
        if self.verbose:
            print("        " + line, flush=True)

    def _new_day(self) -> dict:
        return {"sorties": {BLUE: 0, RED: 0},
                "aircraft lost": {BLUE: defaultdict(int), RED: defaultdict(int)},
                "vehicles lost": {BLUE: defaultdict(int), RED: defaultdict(int)},
                "captured": [], "battles": 0, "attacks won": 0, "sam radars hit": 0,
                "vehicles repaired": {BLUE: 0, RED: 0}}

    def lose_vehicles(self, u: Unit, killed: dict[str, int], cause: str) -> None:
        n = sum(killed.values())
        if n:
            self.day["vehicles lost"][u.side][cause] += n
        for t, k in sorted(killed.items()):
            for _ in range(combat.binomial(self.rng, k, REPAIRABLE.get(cause, 0.0))):
                u.repairs.append((self.hour + REPAIR_H, t))

    # ----- setup -----

    def setup(self) -> None:
        cx = self.objs["Khalkhalah"].x
        for o in self.objs.values():
            o.owner = o.start_owner = BLUE if (o.x - cx) / NM <= 3 else RED
            o.garrison = garrison(o)
        damascus = max((o for o in self.objs.values() if o.kind == "town"),
                       key=lambda o: o.size if self.dist(o.name, "Damascus") < 30 * NM else 0).name
        self.capital = damascus
        self.depots = {BLUE: ["King Hussein Air College", "Khalkhalah"],
                       RED: [damascus, "Al-Dumayr"]}
        red_front = self.front(BLUE)
        blue_front = self.front(RED)
        score = lambda n: self.objs[n].size + (5000 if self.objs[n].kind == "airbase" else 0)  # noqa: E731
        second = sorted({m for n in red_front for m in self.adj[n]
                         if self.objs[m].owner == RED and m not in red_front}, key=score, reverse=True)
        n = 0

        def add(side: str, role: str, comp: dict, at: str) -> Unit:
            nonlocal n
            n += 1
            uid = f"{side[0]}{n:02d}"
            return Unit(Fighter(uid, dict(comp), combat.vehicles(comp)), side, role, at)

        def place(u: Unit) -> None:
            self.units[u.id] = u
            self.full_total[u.side] += u.f.full

        # blue: on its front objectives closest to Damascus
        bf = sorted(blue_front, key=lambda o: self.dist(o, damascus))
        for i, (role, comp) in enumerate([("armor", TANK_BLUE), ("mech", MECH_BLUE), ("armor", TANK_BLUE),
                                          ("mech", MECH_BLUE), ("mech", MECH_BLUE)]):
            place(add(BLUE, role, comp, bf[i % len(bf)]))
        place(add(BLUE, "artillery", ARTY_BLUE, bf[0]))
        # red: a mech battalion on the biggest front objectives, T-72s and guns behind
        for _ in range(self.red_strength):
            for o in sorted(red_front, key=score, reverse=True)[:RED_FRONT_BNS]:
                place(add(RED, "mech", MECH_RED, o))
            for o in second[:3]:
                place(add(RED, "armor", TANK_RED, o))
            for o in second[:2]:
                place(add(RED, "artillery", ARTY_RED, o))
            place(add(RED, "armor", TANK_RED, damascus))
            place(add(RED, "armor", TANK_RED, damascus))
            for _ in range(2):  # from the north on day 4
                self.reinforcements.append((3 * 24, add(RED, "armor", TANK_RED, "Al Qutayfah")))
        self.squadrons = [Squadron(BLUE, "F-16C_50", "King Hussein Air College", 24),
                          Squadron(BLUE, "FA-18C_hornet", "Khalkhalah", 12),
                          Squadron(BLUE, "A-10C_2", "Tha'lah", 12),
                          Squadron(RED, "MiG-29A", "Sayqal", 12 * self.red_strength),
                          Squadron(RED, "MiG-23MLD", "Al-Dumayr", 16 * self.red_strength),
                          Squadron(RED, "MiG-21Bis", "Damascus", 12 * self.red_strength),
                          Squadron(RED, "Su-22M4", "Al-Dumayr", 12 * self.red_strength)]
        weakest = sorted(red_front, key=score)
        self.sams = [Sam("SA-2", damascus), Sam("SA-3", "Mezzeh"), Sam("SA-3", "Al-Dumayr"),
                     Sam("SA-6", second[0]), Sam("SA-6", second[1]), Sam("SA-8", weakest[-1]),
                     Sam("SA-8", weakest[-2])]

    # ----- ground -----

    def order(self, u: Unit, dest: str, retreat: bool = False) -> None:
        u.dest, u.left_m, u.retreating = dest, self.adj[u.at][dest], retreat

    def commanders(self) -> None:
        self.blue_commander()
        self.red_commander()

    def idle(self, side: str, fresh: bool = True) -> list[Unit]:
        return [u for u in self.units.values() if u.side == side and u.dest is None and u.battle is None
                and (not fresh or (u.f.strength() >= 0.7 and u.f.morale >= 0.6 and u.f.supply >= 0.4
                                   and self.hour >= u.rest_until))]

    def defence(self, obj: str) -> float:
        """Combat power holding an objective, with its terrain bonus."""
        o = self.objs[obj]
        power = sum(d.f.power() for d in self.units_at(obj, o.owner) if d.role != "artillery")
        power += o.garrison.power() if o.garrison else 0.0
        fire, cover = combat.POSTURE["town" if o.kind == "town" else "airbase"]
        return power * fire * cover

    def blue_commander(self) -> None:
        free = {u.id: u for u in self.idle(BLUE) if u.role != "artillery"}
        targets = sorted(self.front(BLUE), key=lambda n: -(self.objs[n].size / 1000
                                                           + (5 if self.objs[n].kind == "airbase" else 0)
                                                           - self.dist(n, self.capital) / NM / 5))
        for t in targets:
            if t in self.battles or any(u.dest == t for u in self.units.values() if u.side == BLUE):
                continue
            need = ATTACK_ODDS * self.defence(t)
            near = sorted((u for u in free.values() if t in self.adj[u.at]), key=lambda u: -u.f.power())
            group, power = [], 0.0
            for u in near:
                if power >= need and group:
                    break
                group.append(u)
                power += u.f.power()
            if group and power >= need:
                if need:
                    self.battles[t] = Battle(t, BLUE, origin={u.id: u.at for u in group})
                for u in group:
                    self.order(u, t)
                    del free[u.id]
                self.log(f"blue attacks {t} with {len(group)} bn(s) ({power:.0f} vs {need / ATTACK_ODDS:.0f})")
        # gather for the best target that's out of reach now but not for everything blue has free
        red_front = self.front(RED)
        if not red_front:
            return
        total = sum(u.f.power() for u in free.values())
        for t in targets:
            need = ATTACK_ODDS * self.defence(t)
            if t in self.battles or not need or total < need:
                continue
            stage = min((n for n in self.adj[t] if self.objs[n].owner == BLUE),
                        key=lambda n: self.dist(n, t), default=None)
            if stage is None:
                continue
            for u in sorted(free.values(), key=lambda u: self.dist(u.at, stage)):
                if u.at == stage or t in self.adj[u.at]:
                    continue
                p = self.path(u.at, stage, BLUE)
                if p and len(p) > 1:
                    self.order(u, p[1])
                    del free[u.id]
            self.log(f"blue gathers for {t} at {stage} (needs {need:.0f}, has {total:.0f} free)")
            break
        # everyone else idle moves up to the front, one hop at a time
        for u in list(free.values()) + [u for u in self.idle(BLUE) if u.role == "artillery"]:
            if any(self.objs[n].owner == RED for n in self.adj[u.at]):
                continue
            goal = min(red_front, key=lambda o: self.dist(o, self.capital) + self.dist(o, u.at) / 2)
            p = self.path(u.at, goal, BLUE)
            if p and len(p) > 1:
                self.order(u, p[1])

    def red_commander(self) -> None:
        free = {u.id: u for u in self.idle(RED) if u.role != "artillery"}
        # counterattack weak blue units next to us
        for o in sorted({u.at for u in self.units.values() if u.side == BLUE and u.dest is None
                         and u.battle is None}):
            blue = self.units_at(o, BLUE)
            if not blue or min(b.f.strength() for b in blue) >= 0.6 or o in self.battles:
                continue
            near = [u for u in free.values() if o in self.adj[u.at] and len(self.units_at(u.at, RED)) > 1]
            if near and sum(u.f.power() for u in near) >= ATTACK_ODDS * self.defence(o):
                self.battles[o] = Battle(o, RED, origin={u.id: u.at for u in near})
                for u in near:
                    self.order(u, o)
                    del free[u.id]
                self.log(f"red counterattacks {o} with {len(near)} bn(s)")
        # retake empty objectives next to us
        for o in self.front(RED):
            if not self.units_at(o, BLUE) and not self.objs[o].garrison and o not in self.battles \
                    and not any(u.dest == o for u in self.units.values()):
                near = [u for u in free.values() if o in self.adj[u.at]]
                if near:
                    u = min(near, key=lambda u: u.f.power())
                    self.order(u, o)
                    del free[u.id]
                    self.log(f"red reoccupies {o} with {u.id}")
        # reserves reinforce the most threatened front objective
        threat = {}
        for o in self.front(BLUE):
            blue_near = sum(u.f.power() for u in self.units.values() if u.side == BLUE
                            and (u.at in self.adj[o] or u.dest == o))
            red_here = self.defence(o) + sum(u.f.power() for u in self.units.values()
                                             if u.side == RED and u.dest == o)
            if blue_near * ATTACK_ODDS > red_here:
                threat[o] = blue_near * ATTACK_ODDS - red_here
        front = set(self.front(BLUE))
        for u in sorted(free.values(), key=lambda u: u.id):
            if u.at in front and u.at in threat:
                continue  # stay where it is needed
            if not threat:
                break
            o = max(threat, key=lambda k: threat[k] - self.dist(u.at, k) / NM)
            if o == u.at:
                continue
            p = self.path(u.at, o, RED)
            if p and len(p) > 1 and all(self.objs[n].owner == RED for n in p):
                self.order(u, p[1])
                threat[o] -= u.f.power()
                if threat[o] <= 0:
                    del threat[o]

    def move(self) -> None:
        for u in sorted(self.units.values(), key=lambda u: u.id):
            if u.dest is None:
                continue
            u.left_m -= SPEED_MS * 3600
            u.f.supply = max(0.0, u.f.supply - 0.005)
            if u.left_m > 0:
                continue
            u.at, u.dest, u.retreating = u.dest, None, False
            o = self.objs[u.at]
            if o.owner == u.side:
                if u.at in self.battles:
                    u.battle = u.at  # reinforcing defenders
                continue
            if self.units_at(u.at, o.owner) or o.garrison:
                if u.at not in self.battles:
                    self.battles[u.at] = Battle(u.at, u.side, origin={u.id: u.at})
                u.battle = u.at
            else:
                self.capture(u.at, u.side)
                u.rest_until = self.hour + REST_H

    def capture(self, obj: str, side: str) -> None:
        o = self.objs[obj]
        o.owner = side
        o.garrison = occupiers(o)
        self.battles.pop(obj, None)
        for u in self.units.values():
            if u.battle == obj:
                u.battle = None
        self.day["captured"].append(f"{obj} ({side})")
        if obj == self.capital and side == BLUE and self.capital_fell is None:
            self.capital_fell = self.hour // 24 + 1
        self.log(f"{side} takes {obj}")
        for s in self.sams:
            if s.at == obj and not s.destroyed:
                s.destroyed = True
                self.log(f"{s.type} site at {obj} overrun")
        for sq in self.squadrons:
            if sq.base == obj and sq.side != side:
                bases = [b for b in self.objs.values() if b.kind == "airbase" and b.owner == sq.side]
                enemy = [e.name for e in self.objs.values() if e.owner != sq.side]
                safe = [b for b in bases if min(self.dist(b.name, e) for e in enemy) > 25 * NM] or bases
                if safe:
                    sq.base = min(safe, key=lambda b: self.dist(b.name, obj)).name
                    self.log(f"{sq.side} {sq.type} squadron moves to {sq.base}")
                elif sq.aircraft:
                    self.log(f"{sq.side} {sq.type} squadron has no base left: {sq.aircraft} aircraft lost")
                    self.day["aircraft lost"][sq.side]["no base"] += sq.aircraft
                    sq.aircraft = 0

    def fight(self) -> None:
        for at in sorted(self.battles):
            b = self.battles[at]
            o = self.objs[at]
            att = [u for u in self.units.values() if u.battle == at and u.side == b.attacker]
            arriving = [u for u in self.units.values() if u.dest == at and u.side == b.attacker]
            if not att:
                if not arriving:
                    self.log(f"{b.attacker} attack on {at} fails after {b.hours} h")
                    self.end_battle(b)
                continue
            dfd = [u for u in self.units.values() if u.at == at and u.dest is None and u.side != b.attacker]
            for d in dfd:
                d.battle = at

            def support(side: str, extra: list[Unit] = ()) -> list[Fighter]:
                guns = [u for u in self.units.values() if u.side == side and u.role == "artillery"
                        and u.dest is None and u.battle is None and (u.at == at or at in self.adj[u.at])]
                return [Fighter(u.id, u.f.composition, u.f.full, u.f.supply, u.f.morale, True)
                        for u in guns + list(extra)]

            a_f = [u.f for u in att] + support(b.attacker)
            d_f = [u.f for u in dfd if u.role != "artillery"] + ([o.garrison] if o.garrison else [])
            d_f += support(OTHER[b.attacker], [d for d in dfd if d.role == "artillery"])
            if not [f for f in d_f if not f.support]:
                self.win(b, att)
                continue
            b.hours += 1
            if b.hours == 1:
                self.day["battles"] += 1
            res = combat.ground_hour(a_f, d_f, "town" if o.kind == "town" else "airbase", self.rng)
            for fid, killed in res.losses.items():
                if fid.startswith("g:"):
                    self.day["vehicles lost"][o.owner]["garrison"] += sum(killed.values())
                else:
                    self.lose_vehicles(self.units[fid], killed, "ground")
            for fid in res.broken:
                if fid.startswith("g:"):
                    self.log(f"{o.owner} garrison of {at} gives up")
                    o.garrison = None
                elif self.units[fid].role != "artillery":
                    self.retreat(self.units[fid], b)
            self.disband()
            att = [u for u in self.units.values() if u.battle == at and u.side == b.attacker]
            dfd = [u for u in self.units.values() if u.battle == at and u.side != b.attacker
                   and u.role != "artillery"]
            if att and not dfd and not o.garrison:
                self.win(b, att)
            elif not att and not [u for u in self.units.values() if u.dest == at and u.side == b.attacker]:
                self.log(f"{b.attacker} attack on {at} fails after {b.hours} h")
                self.end_battle(b)
            elif b.hours >= combat.GroundConfig().max_hours:
                for u in att:
                    self.retreat(u, b)
                self.log(f"{b.attacker} calls off the attack on {at} after {b.hours} h")
                self.end_battle(b)

    def win(self, b: Battle, att: list[Unit]) -> None:
        for d in [u for u in self.units.values() if u.at == b.at and u.side != b.attacker and u.dest is None]:
            self.retreat(d, b)  # artillery left behind
        self.day["attacks won"] += 1
        self.log(f"{b.attacker} wins at {b.at} after {b.hours} h")
        self.capture(b.at, b.attacker)
        for u in att:
            u.rest_until = self.hour + REST_H

    def end_battle(self, b: Battle) -> None:
        for u in self.units.values():
            if u.battle == b.at:
                u.battle = None
        self.battles.pop(b.at, None)

    def disband(self) -> None:
        for u in list(self.units.values()):
            if u.role != "artillery" and u.f.strength() < 0.2:
                left = combat.vehicles(u.f.composition)
                self.day["vehicles lost"][u.side]["disbanded"] += left
                self.log(f"{u.side} {u.role} {u.id} destroyed as a unit ({left} vehicles left)")
                del self.units[u.id]

    def retreat(self, u: Unit, b: Battle) -> None:
        u.battle = None
        if u.side == b.attacker:
            back = b.origin.get(u.id, u.at)
            if back != u.at and self.objs[back].owner == u.side:
                u.at = back  # it pulls back to its start line
            u.rest_until = self.hour + REST_H
            self.log(f"{u.side} {u.role} {u.id} breaks off at {b.at} ({u.f.strength():.0%})")
            return
        options = [n for n in self.adj[u.at] if self.objs[n].owner == u.side]
        if not options:
            left = combat.vehicles(u.f.composition)
            self.log(f"{u.side} {u.role} {u.id} cut off at {u.at} and surrenders ({left} vehicles)")
            self.day["vehicles lost"][u.side]["surrendered"] += left
            del self.units[u.id]
            return
        enemy = [o.name for o in self.objs.values() if o.owner != u.side]
        dest = max(options, key=lambda n: min(self.dist(n, e) for e in enemy))
        self.order(u, dest, retreat=True)
        self.log(f"{u.side} {u.role} {u.id} retreats from {u.at} to {dest} ({u.f.strength():.0%})")

    def recover(self) -> None:
        sup = {s: self.supplied(s) for s in (BLUE, RED)}
        for u in self.units.values():
            if u.battle is None and u.dest is None:
                u.f.morale = min(1.0, u.f.morale + 0.02)
                if u.at in sup[u.side]:
                    back = [t for h, t in u.repairs if h <= self.hour]
                    if back:
                        u.repairs = [(h, t) for h, t in u.repairs if h > self.hour]
                        for t in back:
                            u.f.composition[t] = u.f.composition.get(t, 0) + 1
                        self.day["vehicles repaired"][u.side] += len(back)
                if u.at in sup[u.side]:
                    depot = min(self.dist(u.at, d) for d in self.depots[u.side] if self.objs[d].owner == u.side)
                    u.f.supply = min(1.0, u.f.supply + 0.03 * max(0.2, 1 - depot * 1.3 / SUPPLY_REACH_M))
                else:
                    u.f.supply = max(0.0, u.f.supply - 0.005)

    # ----- air -----

    def ready(self, side: str, types: set[str] | None = None) -> list[str]:
        out = []
        for sq in self.squadrons:
            if sq.side == side and (types is None or sq.type in types) and sq.aircraft > 0:
                out += [sq.type] * sum(1 for _ in range(sq.aircraft) if self.rng.random() < AVAILABILITY)
        return out

    def lose_aircraft(self, side: str, types: list[str], cause: str) -> None:
        for t in types:
            sq = next(s for s in self.squadrons if s.side == side and s.type == t and s.aircraft > 0)
            sq.aircraft -= 1
            self.day["aircraft lost"][side][cause] += 1

    def sites_against(self, target: str, base: str) -> list[tuple[Sam, bool]]:
        out = []
        tx, ty = self.objs[target].x, self.objs[target].y
        bx, by = self.objs[base].x, self.objs[base].y
        for s in self.sams:
            if s.destroyed or s.down_until > self.hour:
                continue
            sx, sy = self.objs[s.at].x, self.objs[s.at].y
            r = combat.SAM_TYPES[s.type].range_m
            if math.hypot(sx - tx, sy - ty) <= r:
                out.append((s, True))
            elif _seg_dist(sx, sy, bx, by, tx, ty) <= r:
                out.append((s, False))
        return out

    def air_cycle(self) -> None:
        blue_pool = self.ready(BLUE)
        red_pool = self.ready(RED)

        def take(pool: list[str], types: tuple[str, ...], n: int) -> list[str]:
            got = []
            for t in types:
                while len(got) < n and t in pool:
                    pool.remove(t)
                    got.append(t)
            return got

        # red CAP and blue CAP meet first
        red_cap = take(red_pool, ("MiG-29A", "MiG-23MLD", "MiG-21Bis"), 4)
        blue_cap = take(blue_pool, ("F-16C_50", "FA-18C_hornet"), 4) if red_cap or self.red_fighters() else []
        self.day["sorties"][RED] += len(red_cap)
        self.day["sorties"][BLUE] += len(blue_cap)
        if red_cap and blue_cap:
            lb, lr = combat.air_battle(AirSide(blue_cap, True), AirSide(red_cap, False), self.rng)
            self.lose_aircraft(BLUE, [blue_cap[i] for i in lb], "air to air")
            self.lose_aircraft(RED, [red_cap[i] for i in lr], "air to air")
            if lr or lb:
                self.log(f"CAP fight: blue {len(blue_cap)} lose {len(lb)}, red {len(red_cap)} lose {len(lr)}")
            red_cap = [t for i, t in enumerate(red_cap) if i not in lr]
            blue_cap = [t for i, t in enumerate(blue_cap) if i not in lb]
        # blue CAS packages against red units in battle, then red units on the front
        targets = [u for u in self.units.values() if u.side == RED and u.battle and u.role != "artillery"]
        targets += [u for u in self.units.values() if u.side == RED and not u.battle and u.dest is None
                    and any(self.objs[n].owner == BLUE for n in self.adj[u.at])]
        for tu in targets:
            if combat.vehicles(tu.f.composition) == 0:
                continue
            if tu.id not in self.units:
                continue
            strikers = take(blue_pool, ("A-10C_2", "F-16C_50", "FA-18C_hornet"), 4)
            if len(strikers) < 2:
                break
            base = self.base_of(BLUE, strikers[0])
            sites = self.sites_against(tu.at, base)
            sead = take(blue_pool, ("F-16C_50", "FA-18C_hornet"), 2) if sites else []
            self.day["sorties"][BLUE] += len(strikers) + len(sead)
            if sead:
                at_target = [s for s, t in sites if t] or [sites[0][0]]
                hit = at_target[0]
                if combat.sead_kills(sead, self.rng):
                    hit.down_until = self.hour + combat.SamConfig().repair_h
                    self.day["sam radars hit"] += 1
                    self.log(f"SEAD knocks out the {hit.type} radar at {hit.at}")
                    sites = [(s, t) for s, t in sites if s is not hit]
                lost = combat.sam_losses(sead, [(s.type, t) for s, t in sites], True, self.rng)
                self.lose_aircraft(BLUE, [sead[i] for i in lost], "SAM")
            if red_cap and (not blue_cap and self.rng.random() < 0.5 or self.rng.random() < 0.15):
                lr, lb = combat.air_battle(AirSide(red_cap, False), AirSide(strikers, True), self.rng)
                self.lose_aircraft(RED, [red_cap[i] for i in lr], "air to air")
                self.lose_aircraft(BLUE, [strikers[i] for i in lb], "air to air")
                self.log(f"red CAP jumps blue {strikers[0]} flight over {tu.at}: red lose {len(lr)}, blue {len(lb)}")
                red_cap = [t for i, t in enumerate(red_cap) if i not in lr]
                strikers = [t for i, t in enumerate(strikers) if i not in lb]
            lost = combat.sam_losses(strikers, [(s.type, t) for s, t in sites], bool(sead), self.rng)
            self.lose_aircraft(BLUE, [strikers[i] for i in lost], "SAM")
            strikers = [t for i, t in enumerate(strikers) if i not in lost]
            lost = combat.shorad_losses(strikers, tu.f.composition, self.rng)
            self.lose_aircraft(BLUE, [strikers[i] for i in lost], "SHORAD")
            strikers = [t for i, t in enumerate(strikers) if i not in lost]
            cover = 2.0 if self.objs[tu.at].kind == "town" and tu.dest is None else 1.0
            killed = combat.cas_kills(strikers, tu.f.composition, self.rng, cover)
            for t, k in killed.items():
                tu.f.composition[t] -= k
            tu.f.morale = max(0.0, tu.f.morale - 0.03 * sum(killed.values()))
            self.lose_vehicles(tu, killed, "air")
        # red strikes blue units in battle or on the front
        for tu in [u for u in self.units.values() if u.side == BLUE and (u.battle or any(
                self.objs[n].owner == RED for n in self.adj[u.at]))][:2]:
            strikers = take(red_pool, ("Su-22M4",), 4)
            if len(strikers) < 2:
                break
            self.day["sorties"][RED] += len(strikers)
            if blue_cap and self.rng.random() < 0.7:
                lr, lb = combat.air_battle(AirSide(strikers, False), AirSide(blue_cap, True), self.rng)
                self.lose_aircraft(RED, [strikers[i] for i in lr], "air to air")
                self.lose_aircraft(BLUE, [blue_cap[i] for i in lb], "air to air")
                strikers = [t for i, t in enumerate(strikers) if i not in lr]
                if self.rng.random() < 0.6:
                    strikers = []  # the rest jettison and run
            lost = combat.shorad_losses(strikers, tu.f.composition, self.rng)
            self.lose_aircraft(RED, [strikers[i] for i in lost], "SHORAD")
            strikers = [t for i, t in enumerate(strikers) if i not in lost]
            killed = combat.cas_kills(strikers, tu.f.composition, self.rng)
            for t, k in killed.items():
                tu.f.composition[t] -= k
            self.lose_vehicles(tu, killed, "air")
            self.log(f"red Su-22 strike on {tu.id} at {tu.at}: {len(strikers)} reach it, "
                     f"{sum(killed.values())} vehicles killed")

    def red_fighters(self) -> int:
        return sum(s.aircraft for s in self.squadrons if s.side == RED and s.type != "Su-22M4")

    def base_of(self, side: str, t: str) -> str:
        return next(s.base for s in self.squadrons if s.side == side and s.type == t)

    # ----- loop -----

    def run(self, days: int) -> None:
        for _ in range(days * 24):
            for when, u in list(self.reinforcements):
                if when == self.hour:
                    self.units[u.id] = u
                    self.full_total[u.side] += u.f.full
                    self.reinforcements.remove((when, u))
                    self.log(f"red reinforcements arrive at {u.at}: {u.role} {u.id}")
            if self.hour % 3 == 0:
                self.commanders()
            self.move()
            if self.hour % 24 in AIR_CYCLES:
                self.air_cycle()
                self.disband()
            self.fight()
            self.recover()
            self.hour += 1
            if self.hour % 24 == 0:
                self.end_day()

    def strength(self, side: str) -> tuple[int, int]:
        us = [u for u in self.units.values() if u.side == side]
        return sum(combat.vehicles(u.f.composition) for u in us), self.full_total[side]

    def end_day(self) -> None:
        d = self.day
        held = {s: sum(1 for o in self.objs.values() if o.owner == s) for s in (BLUE, RED)}
        rec = {"day": self.hour // 24, "held": held, "strength": {s: self.strength(s) for s in (BLUE, RED)},
               "aircraft": {s: sum(q.aircraft for q in self.squadrons if q.side == s) for s in (BLUE, RED)},
               "sorties": d["sorties"], "battles": d["battles"], "attacks won": d["attacks won"],
               "captured": d["captured"], "sam radars hit": d["sam radars hit"],
               "aircraft lost": {s: dict(d["aircraft lost"][s]) for s in (BLUE, RED)},
               "vehicles lost": {s: dict(d["vehicles lost"][s]) for s in (BLUE, RED)},
               "taken_from_start": sum(1 for o in self.objs.values() if o.owner != o.start_owner and o.owner == BLUE),
               "lost_from_start": sum(1 for o in self.objs.values() if o.owner != o.start_owner and o.owner == RED)}
        self.days.append(rec)
        fmt = lambda m: ", ".join(f"{k} {v}" for k, v in sorted(m.items())) or "none"  # noqa: E731
        bs, rs = rec["strength"][BLUE], rec["strength"][RED]
        print(f"DAY {rec['day']:>2}  blue holds {held[BLUE]}/{len(self.objs)} ({rec['taken_from_start']} taken from red, "
              f"{rec['lost_from_start']} lost to red); {d['battles']} battles, {d['attacks won']} won", flush=True)
        print(f"        ground: blue {bs[0]}/{bs[1]} vehicles, red {rs[0]}/{rs[1]}; lost blue "
              f"[{fmt(rec['vehicles lost'][BLUE])}], red [{fmt(rec['vehicles lost'][RED])}]; repaired blue "
              f"{d['vehicles repaired'][BLUE]}, red {d['vehicles repaired'][RED]}", flush=True)
        print(f"        air: sorties blue {d['sorties'][BLUE]}, red {d['sorties'][RED]}; lost blue "
              f"[{fmt(rec['aircraft lost'][BLUE])}], red [{fmt(rec['aircraft lost'][RED])}]; "
              f"left blue {rec['aircraft'][BLUE]}, red {rec['aircraft'][RED]}", flush=True)
        if d["captured"]:
            print(f"        captured: {', '.join(d['captured'])}", flush=True)
        self.day = self._new_day()


def _seg_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def summary(war: War) -> None:
    days = war.days
    tot = lambda side, key: sum(sum(d[key][side].values()) for d in days)  # noqa: E731
    sorties = {s: sum(d["sorties"][s] for d in days) for s in (BLUE, RED)}
    by_cause = lambda side: {c: sum(d["aircraft lost"][side].get(c, 0) for d in days)  # noqa: E731
                             for c in ("air to air", "SAM", "SHORAD")}
    print("\nSUMMARY", flush=True)
    for s in (BLUE, RED):
        v, full = war.strength(s)
        al = tot(s, "aircraft lost")
        print(f"  {s:<4} ground {v}/{full} vehicles left ({100 * (1 - v / full):.0f}% lost); "
              f"aircraft lost {al} in {sorties[s]} sorties ({100 * al / max(1, sorties[s]):.2f}% per sortie) "
              f"{by_cause(s)}", flush=True)
    a2a_b, a2a_r = by_cause(BLUE)["air to air"], by_cause(RED)["air to air"]
    print(f"  air-to-air exchange: red {a2a_r} lost to blue {a2a_b}", flush=True)
    fell = f"fell on day {war.capital_fell}" if war.capital_fell else "still red"
    print(f"  {war.capital} {fell}; blue holds {days[-1]['held'][BLUE]}/{len(war.objs)} objectives after "
          f"{len(days)} days", flush=True)


def compare(objs: dict[str, Obj], links: dict[tuple[str, str], float], args: argparse.Namespace) -> None:
    import contextlib
    import copy
    import io
    for seed in range(args.seed, args.seed + args.seeds):
        war = War(copy.deepcopy(objs), links, seed, False, args.red_strength)
        war.setup()
        with contextlib.redirect_stdout(io.StringIO()):
            war.run(args.days)
        b, r = war.strength(BLUE), war.strength(RED)
        lost = {s: sum(sum(d["aircraft lost"][s].values()) for d in war.days) for s in (BLUE, RED)}
        sorties = sum(d["sorties"][BLUE] for d in war.days)
        print(f"seed {seed:>3}: {war.capital} {'day ' + str(war.capital_fell) if war.capital_fell else 'held':<7} "
              f"blue holds {war.days[-1]['held'][BLUE]:>2}/{len(objs)}; ground lost blue {100 - 100 * b[0] // b[1]:>2}% "
              f"red {100 - 100 * r[0] // r[1]:>3}%; aircraft lost blue {lost[BLUE]:>2} "
              f"({100 * lost[BLUE] / max(1, sorties):.2f}%/sortie) red {lost[RED]:>3}", flush=True)


def draw(war: War, path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont
    W = 1200
    xs = [o.x for o in war.objs.values()]
    ys = [o.y for o in war.objs.values()]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) * 1.08
    mx, my = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    px = lambda o: (W / 2 + (o.y - my) / span * W, W / 2 - (o.x - mx) / span * W)  # noqa: E731
    img = Image.new("RGB", (W, W + 40), (238, 232, 216))
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=13)
    col = {BLUE: (40, 80, 170), RED: (190, 50, 40)}
    for (a, b) in war.links:
        oa, ob = war.objs[a], war.objs[b]
        c = (160, 160, 150) if oa.owner == ob.owner else (60, 60, 60)
        d.line([px(oa), px(ob)], fill=c, width=1 if oa.owner == ob.owner else 2)
    for o in war.objs.values():
        x, y = px(o)
        r = 7 if o.kind == "airbase" else 3 + min(6, o.size / 2000)
        shape = d.rectangle if o.kind == "airbase" else d.ellipse
        shape([x - r, y - r, x + r, y + r], fill=col[o.owner],
              outline=(240, 200, 0) if o.owner != o.start_owner else None, width=3)
        d.text((x + r + 2, y - 7), o.name, fill=(40, 40, 40), font=font)
    for u in war.units.values():
        x, y = px(war.objs[u.at])
        off = 12 + 10 * sorted(v.id for v in war.units.values() if v.at == u.at).index(u.id)
        d.rectangle([x - 4, y + off - 4, x + 4, y + off + 4], outline=col[u.side], width=2)
    for s in war.sams:
        x, y = px(war.objs[s.at])
        d.text((x - 18, y - 20), s.type + (" x" if s.destroyed else ""), fill=(120, 0, 120), font=font)
    d.text((10, W + 10), f"P7 war after {len(war.days)} days: fill = owner now; yellow ring = changed hands; "
           f"dark links = front; small squares = battalions; purple = SAM sites (x = destroyed)",
           fill=(20, 20, 20), font=font)
    img.save(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--theater", type=Path, default=Path("build/theater/syria-probe.json"))
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--red-strength", type=int, default=1, help="red battalions and aircraft x this")
    ap.add_argument("--seeds", type=int, default=0,
                    help="run this many seeds quietly and print one line each, instead of one war")
    ap.add_argument("--out", type=Path, default=Path("build/war"))
    args = ap.parse_args()
    if not args.theater.exists():
        raise SystemExit(f"{args.theater} not found: run tools.theater_probe in DCS first (Run 13)")
    objs, links, from_road = load_theater(args.theater)
    print(f"THEATER {len(objs)} objectives ({sum(o.kind == 'airbase' for o in objs.values())} airbases), "
          f"{len(links)} links ({from_road} with DCS road lengths)", flush=True)
    if args.seeds:
        compare(objs, links, args)
        return
    war = War(objs, links, args.seed, args.verbose, args.red_strength)
    war.setup()
    print(f"FORCES  blue {sum(u.side == BLUE for u in war.units.values())} bns, red "
          f"{sum(u.side == RED for u in war.units.values())} bns (+{len(war.reinforcements)} on day 4); "
          f"red holds {war.capital} and {sum(o.owner == RED for o in objs.values())} objectives", flush=True)
    war.run(args.days)
    summary(war)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "war.json").write_text(json.dumps({"days": war.days, "events": war.events}, indent=1))
    draw(war, args.out / "war.png")
    print(f"WROTE   {args.out / 'war.json'} and {args.out / 'war.png'}", flush=True)


if __name__ == "__main__":
    main()
