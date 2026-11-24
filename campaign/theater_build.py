"""Building a map layer (roadmap D3) from what DCS knows: the rules, without DCS.

``tools/extract_theater.py`` asks DCS for the facts (airbases, places, buildings, bridges, road
routes) and uses these functions to turn them into a ``Theater``:

1. **Places** from the map's ``towns.lua`` are sorted into towns, landmarks and military sites
   by name (``classify``). Landmarks (mosques, hotels, ministries...) aren't towns: each joins the
   nearest town within ``LANDMARK_JOIN_M``, or is dropped. Towns within ``TOWN_MERGE_M`` of each
   other are one town (``merge_places``).
2. **Size** is the number of buildings near a town (houses and blocks; not trash cans, walls or
   market stalls: ``is_building``).
3. **Objectives**: every airdrome (an "airbase" with runways; runway-less ones are helipads), the
   largest towns, and military sites. Hand edits (``Edits``) rename, drop, keep or add.
4. **Links**: the relative neighbourhood graph of the objectives (a link a-b unless some c is
   closer to both), plus a minimum spanning tree so everything is connected, up to a maximum
   length. Each link gets DCS's road route, and the bridges on that route become BRIDGE
   objectives the link depends on (``via``).
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field

from .model import Feature, Link, ObjectiveDef, ObjectiveType, Pos

TOWN_MERGE_M = 2_500       # towns closer than this are one town
LANDMARK_JOIN_M = 4_000    # a landmark joins the nearest town within this, or is dropped
BRIDGE_ON_ROUTE_M = 40     # a bridge this close to a link's road route carries it
BRIDGE_MERGE_M = 60        # bridge objects this close together are one bridge (twin spans)

LANDMARK_WORDS = ("mosque", "cathedral", "church", "tower", "hotel", "bank", "ministry", "palace", "university",
                  "hospital", "stadium", "square", "gate", "citadel", "museum", "station", "market", "school",
                  "dam", "reservoir", "racetrack", "monastery", "shrine", "tomb", "mall", "embassy", "college",
                  "park", "castle", "fort", "temple", "airport", "port", "power plant", "factory", "refinery")
MILITARY_WORDS = ("brigade", "division", "regiment", "battalion", "barracks", "military", "army", "saa", "syadf",
                  "air defense", "air defence", "base", "training ground")
BUILDING_WORDS = ("HOUSE", "BUILDING", "BLOCK")


def slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "x"


def dist(a: Pos, b: Pos) -> float:
    return math.dist((a.x, a.y), (b.x, b.y))


def _words(words: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


_MILITARY = _words(MILITARY_WORDS)
_LANDMARK = _words(LANDMARK_WORDS)


def clean_name(name: str) -> str:
    """A place name without stray symbols (towns.lua has e.g. "SAA 171th Brigade%")."""
    return re.sub(r"[^\w)'\]]+$", "", name.strip()) or name


def classify(name: str) -> str:
    """'military', 'landmark' or 'town', from a place name (whole words: "Damascus" is no dam)."""
    if _MILITARY.search(name):
        return "military"
    if _LANDMARK.search(name):
        return "landmark"
    return "town"


def is_building(scenery_type: str) -> bool:
    t = scenery_type.upper()
    return any(w in t for w in BUILDING_WORDS)


@dataclass
class Place:
    name: str
    pos: Pos


@dataclass
class Town:
    id: str
    name: str
    pos: Pos
    members: list[str] = field(default_factory=list)   # place names merged into it
    buildings: int = 0


def merge_places(places: list[Place]) -> tuple[list[Town], list[Town]]:
    """(towns, military sites) from a map's places."""
    towns = [p for p in places if classify(p.name) == "town"]
    marks = [p for p in places if classify(p.name) == "landmark"]
    military = [Town(f"mil_{slug(p.name)}", p.name, p.pos, [p.name]) for p in places if classify(p.name) == "military"]

    parent = list(range(len(towns)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(towns)):
        for j in range(i + 1, len(towns)):
            if dist(towns[i].pos, towns[j].pos) < TOWN_MERGE_M:
                parent[root(i)] = root(j)
    groups: dict[int, list[Place]] = {}
    for i, p in enumerate(towns):
        groups.setdefault(root(i), []).append(p)
    out = []
    for members in groups.values():
        cx = sum(p.pos.x for p in members) / len(members)
        cy = sum(p.pos.y for p in members) / len(members)
        c = Pos(cx, cy)
        name = min(members, key=lambda p: dist(p.pos, c)).name
        out.append(Town(f"town_{slug(name)}", name, c, sorted(p.name for p in members)))
    for m in marks:
        near = min(out, key=lambda t: dist(t.pos, m.pos), default=None)
        if near and dist(near.pos, m.pos) <= LANDMARK_JOIN_M:
            near.members.append(m.name)
    _unique_ids(out)
    _unique_ids(military)
    return out, military


def _unique_ids(items: list) -> None:
    seen: dict[str, int] = {}
    for it in items:
        n = seen.get(it.id, 0)
        seen[it.id] = n + 1
        if n:
            it.id = f"{it.id}_{n + 1}"


@dataclass
class Edits:
    """Hand clean-up of a map layer (``data/theaters/<map>/edits.toml``), applied on every build."""
    rename: dict[str, str] = field(default_factory=dict)   # objective id -> name
    drop: set[str] = field(default_factory=set)            # objective ids
    keep: set[str] = field(default_factory=set)            # towns to include even if not among the largest
    add: list[ObjectiveDef] = field(default_factory=list)
    drop_links: set[frozenset[str]] = field(default_factory=set)
    add_links: set[frozenset[str]] = field(default_factory=set)

    @classmethod
    def from_dict(cls, d: dict) -> Edits:
        known = {"objectives", "links"}
        if set(d) - known:
            raise ValueError(f"edits: unknown sections {sorted(set(d) - known)}")
        o, ln = d.get("objectives") or {}, d.get("links") or {}
        if set(o) - {"rename", "drop", "keep", "add"} or set(ln) - {"drop", "add"}:
            raise ValueError("edits: unknown keys (objectives: rename, drop, keep, add; links: drop, add)")
        adds = []
        for a in o.get("add") or []:
            try:
                adds.append(ObjectiveDef(id=a["id"], name=a["name"], type=ObjectiveType[a["type"]],
                                         pos=Pos(float(a["x"]), float(a["y"])), features=[],
                                         priority=int(a.get("priority", 1)), dcs_airbase=a.get("dcs_airbase")))
            except (KeyError, ValueError) as e:
                raise ValueError(f"edits: bad added objective {a!r}: {e}") from None

        def pairs(key: str) -> set[frozenset[str]]:
            out = set()
            for p in ln.get(key) or []:
                if len(p) != 2 or p[0] == p[1]:
                    raise ValueError(f"edits: links.{key} needs pairs of two objective ids, got {p!r}")
                out.add(frozenset(p))
            return out

        return cls(rename=dict(o.get("rename") or {}), drop=set(o.get("drop") or []), keep=set(o.get("keep") or []),
                   add=adds, drop_links=pairs("drop"), add_links=pairs("add"))


# DCS helipads on Syria: HS (Syria), HL (Lebanon), HI (Israel), HJ (Jordan), HC (Cyprus), HT (Turkey),
# HMed (hospitals), HStad (stadiums), HOil (oil fields), H_med_orig_NN, and "... Heliport".
HELIPAD_RE = re.compile(r"^(H[A-Z]?[a-z]*\d+|H_\w+|.*\bHeliport)$")


def is_helipad(name: str) -> bool:
    return bool(HELIPAD_RE.match(name.strip()))


def airbase_objective(ab: dict) -> ObjectiveDef | None:
    """An airbase from a DCS ``airbases`` query entry; None for helipads and ships. Some real
    airfields (At Tanf, Kharab Ishk) report no runways, so helipads are told apart by name."""
    runways = ab.get("runways") or []
    if ab.get("category") != 0 or not ab.get("pos") or is_helipad(ab.get("name") or ""):
        return None
    feats = [Feature(id=f"rwy_{i}", dcs_ref=str(r.get("name")), value=1 / len(runways)) for i, r in enumerate(runways)]
    if not runways:
        feats = [Feature(id="strip", dcs_ref="", value=1.0)]   # a strip DCS reports no runway for
    return ObjectiveDef(id=f"ab_{slug(ab['name'])}", name=ab["name"], type=ObjectiveType.AIRBASE,
                        pos=Pos(ab["pos"]["x"], ab["pos"]["y"]), features=feats, priority=5,
                        dcs_airbase=ab["name"])


def town_priority(buildings: int) -> int:
    return 3 if buildings >= 3000 else 2 if buildings >= 1500 else 1


def pick_objectives(airdromes: list[ObjectiveDef], towns: list[Town], military: list[Town], min_buildings: int,
                    edits: Edits, max_towns: int = 0) -> dict[str, ObjectiveDef]:
    """Towns with at least ``min_buildings`` (at most ``max_towns`` of the largest, if set), plus
    those the edits keep."""
    objs: dict[str, ObjectiveDef] = {o.id: o for o in airdromes}
    ranked = [t for t in sorted(towns, key=lambda t: -t.buildings) if t.buildings >= min_buildings]
    if max_towns:
        ranked = ranked[:max_towns]
    picked = {t.id for t in ranked}
    chosen = ranked + [t for t in towns if t.id in edits.keep and t.id not in picked]
    for t in chosen:
        objs[t.id] = ObjectiveDef(id=t.id, name=t.name, type=ObjectiveType.CITY, pos=t.pos, features=[],
                                  priority=town_priority(t.buildings))
    for m in military:
        objs[m.id] = ObjectiveDef(id=m.id, name=m.name, type=ObjectiveType.DEPOT, pos=m.pos, features=[], priority=2)
    for a in edits.add:
        objs[a.id] = a
    for oid in edits.drop:
        objs.pop(oid, None)
    for oid, name in edits.rename.items():
        if oid in objs:
            objs[oid].name = name
    return objs


def candidate_links(objs: dict[str, ObjectiveDef], max_m: float, edits: Edits | None = None) -> list[tuple[str, str]]:
    """Relative neighbourhood graph up to max_m, plus a minimum spanning tree (any length) so the
    network is connected; then the hand edits. Pairs are sorted (a < b)."""
    ids = sorted(objs)
    pos = [objs[i].pos for i in ids]
    n = len(ids)
    d = [[dist(pos[i], pos[j]) for j in range(n)] for i in range(n)]
    # Each objective's neighbours by distance: a blocker k for i-j is closer to i than j is.
    order = [sorted(range(n), key=lambda k: d[i][k]) for i in range(n)]
    links: set[tuple[int, int]] = set()
    for i in range(n):
        for j in range(i + 1, n):
            dij = d[i][j]
            if dij > max_m:
                continue
            blocked = False
            for k in order[i]:
                if d[i][k] >= dij:
                    break
                if k != i and d[j][k] < dij:
                    blocked = True
                    break
            if not blocked:
                links.add((i, j))
    # Prim's MST, to connect what the length limit cut off.
    if n:
        inside = {0}
        best = {j: (d[0][j], 0) for j in range(1, n)}
        while best:
            j = min(best, key=lambda k: best[k][0])
            _, i = best.pop(j)
            links.add((min(i, j), max(i, j)))
            inside.add(j)
            for k in best:
                if d[j][k] < best[k][0]:
                    best[k] = (d[j][k], j)
    out = {(ids[i], ids[j]) for i, j in links}
    if edits:
        out = {p for p in out if frozenset(p) not in edits.drop_links}
        out |= {tuple(sorted(p)) for p in edits.add_links if all(x in objs for x in p)}
    return sorted(out)


def _point_seg(p: Pos, a: Pos, b: Pos) -> float:
    dx, dy = b.x - a.x, b.y - a.y
    seg2 = dx * dx + dy * dy
    k = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / seg2))
    return math.dist((p.x, p.y), (a.x + dx * k, a.y + dy * k))


def merge_bridges(found: list[dict]) -> list[dict]:
    """Bridge scenery objects ({id, type, x, y}) with twin spans and repeats merged into one."""
    out: list[dict] = []
    grid: dict[tuple[int, int], list[dict]] = {}
    for b in found:
        cx, cy = int(b["x"] // BRIDGE_MERGE_M), int(b["y"] // BRIDGE_MERGE_M)
        near = (o for dx in (-1, 0, 1) for dy in (-1, 0, 1) for o in grid.get((cx + dx, cy + dy), ()))
        if any(math.dist((b["x"], b["y"]), (o["x"], o["y"])) < BRIDGE_MERGE_M for o in near):
            continue
        out.append(b)
        grid.setdefault((cx, cy), []).append(b)
    return out


def bridges_on(route: list[Pos], bridges: list[dict]) -> list[dict]:
    """The bridges a road route crosses, in order along it."""
    if len(route) < 2:
        return []
    m = BRIDGE_ON_ROUTE_M
    x0, x1 = min(p.x for p in route) - m, max(p.x for p in route) + m
    y0, y1 = min(p.y for p in route) - m, max(p.y for p in route) + m
    hits = []
    for b in bridges:
        if not (x0 <= b["x"] <= x1 and y0 <= b["y"] <= y1):
            continue
        p = Pos(b["x"], b["y"])
        best = None
        along = 0.0
        for a, c in zip(route, route[1:]):
            dd = _point_seg(p, a, c)
            if dd <= BRIDGE_ON_ROUTE_M and (best is None or dd < best[0]):
                best = (dd, along + dist(a, p))
            along += dist(a, c)
        if best:
            hits.append((best[1], b))
    return [b for _, b in sorted(hits, key=lambda h: h[0])]


def bridge_objective(b: dict) -> ObjectiveDef:
    kind = "Rail bridge" if "RAIL" in b["type"].upper() else "Bridge"
    return ObjectiveDef(id=f"br_{slug(str(b['id']))}", name=f"{kind} {b['id']}", type=ObjectiveType.BRIDGE,
                        pos=Pos(b["x"], b["y"]), features=[Feature(id="span", dcs_ref=str(b["id"]), value=1.0)],
                        priority=1)


def make_link(a: str, b: str, objs: dict[str, ObjectiveDef], route: list[Pos], bridges: list[dict]) -> Link:
    on = bridges_on(route, bridges)
    for br in on:
        o = bridge_objective(br)
        objs.setdefault(o.id, o)
    length = sum(dist(p, q) for p, q in zip(route, route[1:])) if len(route) >= 2 else dist(objs[a].pos, objs[b].pos)
    return Link(a=a, b=b, kind="road", via=[bridge_objective(br).id for br in on], route=route,
                length_m=round(length, 1))


# ---------- infrastructure, from the scenery survey ----------

@dataclass(frozen=True)
class InfraRule:
    type: ObjectiveType
    label: str
    match: str            # regex on the scenery type name
    cluster_m: float      # objects this close are one site
    min_objects: int
    priority: int
    skip_airbases: bool = False   # clusters within AIRBASE_AREA_M of an airbase belong to it


AIRBASE_AREA_M = 3_000
MAX_FEATURES = 20
# Tuned on the Syria survey (2026-11-24): 29 refineries, ~20 power plants, 41 substations, 68 fuel
# depots, 5 dams, 4 cement works and ~53 industrial areas.
INFRA_RULES = [
    InfraRule(ObjectiveType.REFINERY, "Refinery", r"^OILGAS_REFINERY", 1_500, 3, 4),
    InfraRule(ObjectiveType.POWER_PLANT, "Power plant", r"^POWER_PLANT|ELECTROSTATION", 1_000, 2, 4),
    InfraRule(ObjectiveType.SUBSTATION, "Substation", r"^POWER_TRANS_STATION$", 400, 1, 2),
    InfraRule(ObjectiveType.FUEL_DEPOT, "Fuel depot", r"^(FUEL_STORAGE|LIQ_TANK_BIG|LIQ_TANK|TANKER_BIG)", 600, 10, 3,
              skip_airbases=True),
    InfraRule(ObjectiveType.DAM, "Dam", r"DAM_|_DAM$", 2_000, 1, 3),
    InfraRule(ObjectiveType.FACTORY, "Cement works", r"CEMENT|KEAN_FACTORY", 1_000, 1, 3),
    InfraRule(ObjectiveType.FACTORY, "Industrial area", r"INDUSTRIAL", 300, 200, 2, skip_airbases=True),
]


# Sites DCS models individually, named after the real thing (scenery type prefix -> name).
NAMED_SITES = {"DAM_TISHRIN": "Tishrin Dam", "DAM_ATATURKA": "Atatürk Dam", "AKSIFAT_DAM": "Aksifat Dam",
               "BERDAN_DAM": "Berdan Dam", "LAFARGE_CEMENT": "Lafarge cement works",
               "KONYA_CEMENT": "Konya cement works", "LIMASOL_KEAN": "KEAN factory, Limassol",
               "ALEPPO_ELECTROSTATION": "Aleppo power station"}


def cluster(items: list[dict], within_m: float) -> list[list[dict]]:
    """Single-linkage clusters of {x, y, ...} items (a grid keeps it fast for 100,000 items)."""
    cells: dict[tuple[int, int], list[int]] = {}
    for i, it in enumerate(items):
        cells.setdefault((int(it["x"] // within_m), int(it["y"] // within_m)), []).append(i)
    parent = list(range(len(items)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, it in enumerate(items):
        cx, cy = int(it["x"] // within_m), int(it["y"] // within_m)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in cells.get((cx + dx, cy + dy), ()):
                    if j > i and math.dist((it["x"], it["y"]), (items[j]["x"], items[j]["y"])) <= within_m:
                        parent[root(i)] = root(j)
    groups: dict[int, list[dict]] = {}
    for i, it in enumerate(items):
        groups.setdefault(root(i), []).append(it)
    return list(groups.values())


def infra_objectives(survey: list[dict], objs: dict[str, ObjectiveDef],
                     rules: list[InfraRule] = INFRA_RULES) -> dict[str, ObjectiveDef]:
    """Infrastructure objectives from survey objects ({id, type, x, y}), each in the area of the
    nearest town, airbase or military site in ``objs`` and named after it."""
    anchors = [o for o in objs.values() if o.type in (ObjectiveType.CITY, ObjectiveType.AIRBASE, ObjectiveType.DEPOT)]
    airbases = [o for o in anchors if o.type is ObjectiveType.AIRBASE]
    out: dict[str, ObjectiveDef] = {}
    used: set = set()
    for rule in rules:
        rx = re.compile(rule.match)
        items = [f for f in survey if f.get("id") is not None and f["id"] not in used and rx.search(f["type"])]
        # A named site in several pieces (a dam's sections, a power station's elements) is one site.
        groups: dict[str, list[dict]] = {}
        plain = []
        for c in cluster(items, rule.cluster_m):
            named = next((v for k, v in NAMED_SITES.items() for f in c if f["type"].startswith(k)), None)
            if named:
                groups.setdefault(named, []).extend(c)
            else:
                plain.append(c)
        for c in list(groups.values()) + plain:
            if len(c) < rule.min_objects:
                continue
            p = Pos(sum(f["x"] for f in c) / len(c), sum(f["y"] for f in c) / len(c))
            if rule.skip_airbases and any(dist(p, a.pos) < AIRBASE_AREA_M for a in airbases):
                continue
            used.update(f["id"] for f in c)
            near = min(anchors, key=lambda o: dist(o.pos, p), default=None)
            named = next((v for k, v in NAMED_SITES.items() for f in c if f["type"].startswith(k)), None)
            base = named or (f"{rule.label} near {near.name}" if near else rule.label)
            name, n = base, 1
            while any(o.name == name for o in out.values()):
                n += 1
                name = f"{base} {n}"
            core = sorted(c, key=lambda f: dist(Pos(f["x"], f["y"]), p))[:MAX_FEATURES]
            oid = f"{rule.type.name.lower()}_{slug(name)}"
            out[oid] = ObjectiveDef(id=oid, name=name, type=rule.type, pos=p, priority=rule.priority,
                                    features=[Feature(id=f"obj_{f['id']}", dcs_ref=str(f["id"]), value=1 / len(core))
                                              for f in core],
                                    area_of=near.id if near else None)
    return out


def apply_edits_to(objs: dict[str, ObjectiveDef], edits: Edits) -> None:
    """Drops and renames for objectives made after ``pick_objectives`` (e.g. infrastructure)."""
    for oid in edits.drop:
        objs.pop(oid, None)
    for oid, name in edits.rename.items():
        if oid in objs:
            objs[oid].name = name
