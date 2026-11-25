"""The scenario layer (roadmap D3): a campaign's starting state, authored on top of a map layer.

A scenario is ``data/scenarios/<name>.toml``. It names the map and the area in play, the sides
(each owning the objectives in its countries), the squadrons, and hand-placed formations; rules
fill in the routine placements (units at military sites, airbase and city defences). See
docs/scenarios.md. ``build_state`` turns it into a ``CampaignState``.
"""
from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .geo import TERRAINS
from .geography import Geography, load_geography
from .model import (CampaignState, GroundOrder, GroundUnit, ObjectiveDef, ObjectiveState, ObjectiveType, Pos, Side,
                    SideState, Squadron, Theater)
from .ground_war import garrison_for, garrison_unit
from .templates import TEMPLATES
from .theater import load_theater, theater_dir

ROOT = Path(__file__).resolve().parent.parent
NM = 1852.0
RING_M = 1_500        # formations at one site are spread on a ring this far out
SIDES = {"blue": Side.BLUE, "red": Side.RED}


class ScenarioError(Exception):
    pass


# What a military site's name says it holds (first match wins), as templates.
SITE_RULES: list[tuple[str, list[str]]] = [
    (r"air defen[cs]e|syadf", ["red_sa3", "red_sa6"]),
    (r"artillery", ["red_art_bn", "red_art_bn"]),
    (r"armou?red division", ["red_tank_bn", "red_tank_bn", "red_mech_bn"]),
    (r"division", ["red_tank_bn", "red_mech_bn"]),
    (r"brigade|regiment", ["red_tank_bn", "red_mech_bn"]),
    (r"training ground", ["red_old_tank_bn"]),
    (r".", ["red_mech_bn"]),
]


@dataclass
class Placement:
    """Formations at an objective, or at a bearing/range from one."""
    templates: list[str]
    at: str
    side: Side
    brg_nm: tuple[float, float] | None = None   # magnetic bearing, nm from ``at``
    name: str = ""
    order: GroundOrder = GroundOrder.DEFEND


@dataclass
class ScenarioDef:
    name: str
    title: str
    map: str
    center: str
    radius_nm: float
    magvar: float
    sides: dict[Side, dict]
    owners: dict[str, Side] = field(default_factory=dict)
    squadrons: list[dict] = field(default_factory=list)
    placements: list[Placement] = field(default_factory=list)
    rules: dict = field(default_factory=dict)
    description: str = ""


def _side(v: str, where: str) -> Side:
    if v not in SIDES:
        raise ScenarioError(f"{where}: side must be blue or red, not {v!r}")
    return SIDES[v]


def scenario_path(name: str) -> Path:
    return ROOT / "data" / "scenarios" / f"{name}.toml"


def load_scenario_def(path: Path) -> ScenarioDef:
    try:
        d = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ScenarioError(f"{path}: {e}") from None
    known = {"name", "title", "description", "map", "magvar", "area", "sides", "owners", "squadrons", "formations",
             "rules"}
    if set(d) - known:
        raise ScenarioError(f"{path.name}: unknown keys {sorted(set(d) - known)}")
    rules = dict(d.get("rules") or {})
    rule_keys = {"military_sites", "airbase_defence", "city_defence", "garrisons"}
    if set(rules) - rule_keys:
        raise ScenarioError(f"{path.name}: unknown keys in [rules]: {sorted(set(rules) - rule_keys)}")
    for k, v in (d.get("sides") or {}).items():
        extra = set(v) - {"name", "countries", "posture", "goal"}
        if extra:
            raise ScenarioError(f"{path.name}: unknown keys in [sides.{k}]: {sorted(extra)}")
        if v.get("posture", "defend") not in ("attack", "defend"):
            raise ScenarioError(f"sides.{k}.posture must be \"attack\" or \"defend\"")
    for side_key, temps in (rules.get("airbase_defence") or {}).items():
        _side(side_key, "rules.airbase_defence")
        for t in temps:
            if t not in TEMPLATES:
                raise ScenarioError(f"rules.airbase_defence.{side_key}: no template {t!r}")
    for side_key, c in (rules.get("city_defence") or {}).items():
        _side(side_key, "rules.city_defence")
        for t in c.get("templates") or []:
            if t not in TEMPLATES:
                raise ScenarioError(f"rules.city_defence.{side_key}: no template {t!r}")
    try:
        area = d["area"]
        sides = {_side(k, "sides"): v for k, v in d["sides"].items()}
        placements = []
        for i, f in enumerate(d.get("formations") or []):
            for t in f["templates"]:
                if t not in TEMPLATES:
                    raise ScenarioError(f"formations[{i}]: no template {t!r}")
            brg = f.get("brg_nm")
            placements.append(Placement(templates=list(f["templates"]), at=f["at"], side=_side(f["side"], f"formations[{i}]"),
                                        brg_nm=(float(brg[0]), float(brg[1])) if brg else None, name=f.get("name", ""),
                                        order=GroundOrder[f.get("order", "DEFEND").upper()]))
        return ScenarioDef(name=d["name"], title=d.get("title", d["name"]), map=d["map"], center=area["center"],
                           radius_nm=float(area["radius_nm"]), magvar=float(d.get("magvar", 0.0)), sides=sides,
                           owners={k: _side(v, f"owners.{k}") for k, v in (d.get("owners") or {}).items()},
                           squadrons=list(d.get("squadrons") or []), placements=placements,
                           rules=rules, description=d.get("description", ""))
    except KeyError as e:
        raise ScenarioError(f"{path.name}: missing {e}") from None


def _offset(p: Pos, brg_mag: float, nm: float, magvar: float) -> Pos:
    t = math.radians(brg_mag + magvar)
    return Pos(p.x + nm * NM * math.cos(t), p.y + nm * NM * math.sin(t))


@dataclass
class Built:
    state: CampaignState
    theater: Theater
    in_play: set[str]
    warnings: list[str]


def build_state(sd: ScenarioDef, theater: Theater | None = None, geo: Geography | None = None) -> Built:
    """The starting CampaignState of a scenario."""
    theater = theater or load_theater(theater_dir(sd.map) / "map.json")
    geo = geo if geo is not None else load_geography(theater_dir(sd.map))
    objs = theater.objectives
    warnings: list[str] = []
    if sd.center not in objs:
        raise ScenarioError(f"area.center {sd.center!r} is not an objective of the {sd.map} map layer")
    c = objs[sd.center].pos
    in_play = {oid for oid, o in objs.items() if math.dist((o.pos.x, o.pos.y), (c.x, c.y)) <= sd.radius_nm * NM}

    st = CampaignState(theater_map=sd.map, clock=0.0,
                       sides={s: SideState(side=s, target_weights={}) for s in (Side.BLUE, Side.RED)})
    country_side = {country: s for s, v in sd.sides.items() for country in v.get("countries", [])}
    for oid in sorted(in_play):
        o = objs[oid]
        owner = sd.owners.get(oid) or country_side.get(o.country or "", Side.NEUTRAL)
        st.objectives[oid] = ObjectiveState(id=oid, owner=owner)
        if sd.rules.get("garrisons", True) and o.area_of is None:
            g = garrison_for(o, owner)
            if g:
                garrison_unit(st, o, owner, g)
    for oid in sd.owners:
        if oid not in in_play:
            warnings.append(f"owners: {oid!r} isn't in play")
    for side, v in sd.sides.items():
        goal = v.get("goal")
        if goal and goal not in in_play:
            raise ScenarioError(f"sides.{side.name.lower()}.goal: {goal!r} isn't an objective in play")

    def owner(oid: str) -> Side:
        return st.objectives[oid].owner if oid in st.objectives else Side.NEUTRAL

    # Squadrons.
    for i, q in enumerate(sd.squadrons):
        where = f"squadrons[{i}] ({q.get('id')})"
        try:
            side = _side(q["side"], where)
            base = q["base"]
            if base not in objs or objs[base].type is not ObjectiveType.AIRBASE:
                raise ScenarioError(f"{where}: base {base!r} isn't an airbase in the map layer")
            if owner(base) is not side:
                raise ScenarioError(f"{where}: base {base} belongs to {owner(base).name.lower()}, not {q['side']}")
            st.squadrons[q["id"]] = Squadron(
                id=q["id"], side=side, name=q.get("name", q["id"]), aircraft_type=q["type"], base_id=base,
                roles=set(q.get("roles") or []), aircraft_ready=int(q["aircraft"]), aircraft_damaged=0,
                pilots=int(q.get("pilots", q["aircraft"])), player_flyable=bool(q.get("player", False)))
        except KeyError as e:
            raise ScenarioError(f"{where}: missing {e}") from None

    # Formations: hand-placed first, then the rules.
    placements = list(sd.placements)
    rules = sd.rules
    if rules.get("military_sites", True):
        for oid in sorted(in_play):
            o = objs[oid]
            if o.type is ObjectiveType.DEPOT and owner(oid) is Side.RED:
                temps = next(t for rx, t in SITE_RULES if re.search(rx, o.name, re.I))
                placements.append(Placement(temps, oid, Side.RED, name=o.name))
    for side_key, side in SIDES.items():
        ab_def = (rules.get("airbase_defence") or {}).get(side_key) or []
        city = (rules.get("city_defence") or {}).get(side_key) or {}
        for oid in sorted(in_play):
            o = objs[oid]
            if owner(oid) is not side:
                continue
            if o.type is ObjectiveType.AIRBASE and ab_def:
                placements.append(Placement(list(ab_def), oid, side, name=f"{o.name} air defence"))
            if o.type is ObjectiveType.CITY and city and o.priority >= int(city.get("min_priority", 3)):
                placements.append(Placement(list(city["templates"]), oid, side, name=f"{o.name} defence"))

    seq: dict[str, int] = {}
    per_site: dict[str, int] = {}
    for i, p in enumerate(placements):
        if p.at not in objs:
            raise ScenarioError(f"formation at {p.at!r}: not an objective of the map layer")
        if p.at not in in_play:
            warnings.append(f"formation at {p.at} is outside the area in play")
        base = objs[p.at].pos
        if p.brg_nm:
            base = _offset(base, *p.brg_nm, sd.magvar)
        for t in p.templates:
            tpl = TEMPLATES[t]
            k = per_site.get(p.at + str(p.brg_nm), 0)
            per_site[p.at + str(p.brg_nm)] = k + 1
            pos = _ring(base, k, geo)
            n = seq.get(t, 0) + 1
            seq[t] = n
            uid = f"{t}_{n}"
            label = f"{p.name or objs[p.at].name}: {tpl.label}"
            st.ground_units[uid] = GroundUnit(id=uid, side=p.side, name=label, role=tpl.role,
                                              composition=dict(tpl.composition), pos=pos,
                                              location_id=p.at if not p.brg_nm else None, order=p.order)
    return Built(st, theater, in_play, warnings)


def _ring(c: Pos, k: int, geo: Geography | None) -> Pos:
    """The k-th spot around a site (the first at its centre), skipping water."""
    for extra in range(12):
        j = k + extra
        if j == 0:
            p = c
        else:
            a = math.radians(j * 137.5)   # golden angle: spots spread evenly however many there are
            r = RING_M * math.sqrt(j)
            p = Pos(c.x + r * math.cos(a), c.y + r * math.sin(a))
        if geo is None or not geo.is_water(p):
            return p
    return c


def front_links(built: Built) -> list[tuple[str, str]]:
    """Links in play between a blue and a red objective: where the front line runs."""
    st = built.state
    out = []
    for ln in built.theater.links:
        if ln.a in st.objectives and ln.b in st.objectives:
            sides = {st.objectives[ln.a].owner, st.objectives[ln.b].owner}
            if sides == {Side.BLUE, Side.RED}:
                out.append((ln.a, ln.b))
    return out


def summary(sd: ScenarioDef, b: Built) -> str:
    st = b.state
    lines = [f"{sd.title} ({sd.name}): {len(b.in_play)} objectives in play within {sd.radius_nm:.0f} nm of "
             f"{b.theater.objectives[sd.center].name}"]
    for side in (Side.BLUE, Side.RED, Side.NEUTRAL):
        owned = [o for o in st.objectives.values() if o.owner is side]
        kinds: dict[str, int] = {}
        for o in owned:
            t = b.theater.objectives[o.id].type.name
            kinds[t] = kinds.get(t, 0) + 1
        sq = [s for s in st.squadrons.values() if s.side is side]
        gu = [u for u in st.ground_units.values() if u.side is side]
        veh = sum(sum(u.composition.values()) for u in gu)
        lines.append(f"  {side.name.lower():7} {len(owned)} objectives {kinds}; {len(sq)} squadrons "
                     f"({sum(s.aircraft_ready for s in sq)} aircraft); {len(gu)} formations ({veh} vehicles)")
    lines.append(f"  front: {len(front_links(b))} links between blue and red objectives")
    return "\n".join(lines + [f"  WARNING {w}" for w in b.warnings])


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Check a scenario and draw it.")
    ap.add_argument("name")
    a = ap.parse_args()
    sd = load_scenario_def(scenario_path(a.name))
    if sd.map not in TERRAINS:
        raise SystemExit(f"unknown map {sd.map!r}")
    b = build_state(sd)
    print(summary(sd, b))
    from tools.draw_scenario import draw
    path = ROOT / "build" / "scenarios" / f"{sd.name}.png"
    draw(path, sd, b)
    print(f"picture: {path}")


if __name__ == "__main__":
    main()
