"""Keep DCS in step with the bubble plan for ground formations.

The spawner owns the SPAWNED/AGGREGATED transitions of ``GroundUnit``s and nothing else:
- ``on_hello`` reconciles after a mission (re)connect,
- ``on_telemetry`` refreshes observers and the positions of spawned groups,
- ``on_event`` applies ground losses (``unit_killed``) and marks formations in combat,
- ``tick`` plans and executes spawns/despawns.

It talks to DCS through an injected ``call(op, data)`` coroutine (``Bridge.call`` bound to the
mission source), so tests can drive it with a fake.

Loss accounting: a ``unit_killed`` for ``gu:<id>#<n>`` decrements that formation's composition
once per DCS unit name per spawn. When a group despawns, the survivors DCS reports replace the
composition, and every unit that is not among them counts as already dead, so a kill event that
is still queued behind the despawn ack cannot be counted twice.

Siting: placements come from the map layer, which doesn't know where buildings stand. Before a
formation first spawns, the mission's ``clear_spots`` query moves each vehicle of its layout to
the nearest clear ground (within ``SITE_SEARCH_M``): no buildings, statics or units within
``SITE_CLEAR_M``, no water or runway, not steep, and not on another vehicle's spot. Vehicles go
one by one, so a city's defences use its streets, squares and lots instead of leaving town for a
field big enough for the whole layout. The spots are kept (``GroundUnit.spots``); the formation
is sited again only after it moves more than ``MOVED_M`` from them in DCS.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Awaitable, Callable

from .bubble import BubbleConfig, Observer, SpawnPlan, observer_from_telemetry, plan_ground, strength
from .ground_spawn import build_ground_spawn
from .model import CampaignState, GroundUnit, Pos, SpawnState
from .protocol import PREFIX_GROUND, Hello, Op, as_list, group_name, parse_dcs_name

log = logging.getLogger(__name__)

Call = Callable[[str, dict[str, Any]], Awaitable[Any]]

ENGAGED_HOLD_S = 180.0    # keep a formation spawned this long after it kills or loses something
FAIL_BACKOFF_S = 60.0     # don't retry a failed spawn for this long
MISSING_TICKS = 2         # telemetry reports without a spawned group before it counts as gone
SITE_SEARCH_M = 1000.0    # how far a vehicle may move from its layout spot to find clear ground
SITE_CLEAR_M = 20.0       # room around a vehicle (a building's point is its centre)
SITE_SPACING_M = 15.0     # two vehicles' spots at least this far apart
MOVED_M = 100.0           # a spawned group's reported position counts as a move beyond this


class GroundSpawner:
    def __init__(self, state: CampaignState, call: Call, cfg: BubbleConfig | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 on_change: Callable[[GroundUnit], None] | None = None,
                 targets: Callable[[], tuple[set[str], set[str]]] | None = None) -> None:
        self.state = state
        self.call = call
        self.cfg = cfg or BubbleConfig()
        self.clock = clock
        self.on_change = on_change  # called after a formation spawns, despawns or is destroyed
        self.targets = targets      # (keep, spawn): AI flights' targets, see bubble.attack_targets
        self.observers: list[Observer] = []
        self.spawned_at: dict[str, float] = {}
        self.busy: set[str] = set()
        self.engaged_until: dict[str, float] = {}
        self.backoff_until: dict[str, float] = {}
        self.unit_types: dict[str, dict[str, str]] = {}  # formation -> DCS unit name -> type
        self.dead: dict[str, set[str]] = {}               # formation -> DCS unit names counted dead
        self.missing: dict[str, int] = {}
        self.orphans: set[str] = set()                    # DCS group names to remove
        self._tick_lock = asyncio.Lock()

    # ---------- save / load ----------

    def export(self) -> dict[str, Any]:
        """Bookkeeping for spawned formations, for the save file. Times are stored relative to now."""
        now = self.clock()
        return {
            "age": {k: now - t for k, t in self.spawned_at.items()},
            "engaged_for": {k: t - now for k, t in self.engaged_until.items() if t > now},
            "unit_types": self.unit_types,
            "dead": {k: sorted(v) for k, v in self.dead.items()},
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Undo ``export`` after a load, before the mission reconnects."""
        now = self.clock()
        self.spawned_at = {k: now - float(a) for k, a in (data.get("age") or {}).items()}
        self.engaged_until = {k: now + float(r) for k, r in (data.get("engaged_for") or {}).items()}
        self.unit_types = {k: dict(v) for k, v in (data.get("unit_types") or {}).items()}
        self.dead = {k: set(v) for k, v in (data.get("dead") or {}).items()}

    # ---------- inputs ----------

    def on_hello(self, hello: Hello, new_run: bool) -> None:
        live = set(hello.spawned)
        for u in self.state.ground_units.values():
            if u.spawn is not SpawnState.SPAWNED:
                continue
            # A new run means DCS restarted the mission: everything it had is gone.
            if new_run or u.dcs_group not in live:
                log.info("%s no longer exists in DCS; aggregating", u.id)
                self._aggregate(u)
        if new_run:
            self.observers = []  # positions from the old run are meaningless now
            self.dead.clear()
            self.unit_types.clear()
        known = {u.dcs_group for u in self.state.ground_units.values() if u.spawn is SpawnState.SPAWNED}
        for name in live - known:
            parsed = parse_dcs_name(name)
            if parsed and parsed.prefix == PREFIX_GROUND:
                self.orphans.add(name)
        self.busy.clear()  # commands in flight died with the old connection

    def on_telemetry(self, data: dict[str, Any]) -> None:
        players = as_list(data.get("players"))
        self.observers = [o for o in (observer_from_telemetry(p, self.cfg.lookahead_s)
                                      for p in players if isinstance(p, dict)) if o]
        seen = set()
        for g in as_list(data.get("groups")):
            if not isinstance(g, dict):
                continue
            parsed = parse_dcs_name(g.get("id"))
            if not parsed or parsed.prefix != PREFIX_GROUND:
                continue
            seen.add(parsed.model_id)
            u = self.state.ground_units.get(parsed.model_id)
            pos = g.get("pos")
            if u and u.spawn is SpawnState.SPAWNED and isinstance(pos, dict):
                self._moved(u, pos)
        for u in self.state.ground_units.values():
            if u.spawn is not SpawnState.SPAWNED or u.id in self.busy or u.id in seen:
                self.missing.pop(u.id, None)
                continue
            self.missing[u.id] = self.missing.get(u.id, 0) + 1
            if self.missing[u.id] >= MISSING_TICKS:
                log.info("%s vanished from DCS telemetry; aggregating", u.id)
                self._aggregate(u)

    def on_event(self, data: dict[str, Any]) -> None:
        kind = data.get("kind")
        if kind == "unit_killed":
            unit = data.get("unit") or {}
            parsed = parse_dcs_name(unit.get("name"))
            if parsed and parsed.prefix == PREFIX_GROUND and parsed.unit_index is not None:
                self._lost(parsed.model_id, unit["name"], unit.get("type"))
        elif kind == "kill":
            for role in ("killer", "victim"):
                parsed = parse_dcs_name((data.get(role) or {}).get("name"))
                if parsed and parsed.prefix == PREFIX_GROUND:
                    self.engaged_until[parsed.model_id] = self.clock() + ENGAGED_HOLD_S

    # ---------- loop ----------

    def plan(self) -> SpawnPlan:
        now = self.clock()
        keep, spawn = self.targets() if self.targets else (set(), set())
        return plan_ground(
            self.state.ground_units.values(), self.observers, now, self.cfg,
            spawned_at=self.spawned_at, busy=self.busy,
            pinned={k for k, t in self.engaged_until.items() if t > now} | keep,
            blocked={k for k, t in self.backoff_until.items() if t > now},
            forced=spawn,
        )

    async def tick(self) -> SpawnPlan:
        """Plan once and carry it out. Overlapping calls are skipped, not queued."""
        if self._tick_lock.locked():
            return SpawnPlan()
        async with self._tick_lock:
            plan = self.plan()
            jobs = [self._remove_orphan(n) for n in list(self.orphans)]
            jobs += [self._despawn(self.state.ground_units[i]) for i in plan.despawn]
            jobs += [self._spawn(self.state.ground_units[i]) for i in plan.spawn]
            for i in plan.despawn + plan.spawn:
                self.busy.add(i)
            if jobs:
                await asyncio.gather(*jobs)
            return plan

    # ---------- commands ----------

    async def _site(self, u: GroundUnit) -> None:
        """Move each vehicle to clear ground near its layout spot, once (see the module notes)."""
        want = [Pos(v["x"], v["y"]) for v in build_ground_spawn(u).data["group"]["units"]]
        try:
            res = await self.call(Op.QUERY, {"what": "clear_spots", "points": [{"x": p.x, "y": p.y} for p in want],
                                             "radius": SITE_CLEAR_M, "spacing": SITE_SPACING_M,
                                             "max": SITE_SEARCH_M})
        except Exception as e:  # noqa: BLE001 - spawn on the layout; try again next spawn
            log.warning("siting %s failed: %s", u.id, e)
            return
        got = as_list((res or {}).get("spots")) if isinstance(res, dict) else []
        spots, stuck, far = [], 0, 0.0
        for i, p in enumerate(want):
            q = got[i] if i < len(got) else None
            if isinstance(q, dict) and "x" in q:
                spot = Pos(float(q["x"]), float(q["y"]))
                far = max(far, math.hypot(spot.x - p.x, spot.y - p.y))
            else:
                spot, stuck = p, stuck + 1
            spots.append(spot)
        u.spots, u.sited = spots, True
        if stuck:
            log.warning("sited %s: %d of %d vehicles found no clear ground within %.0f m", u.id, stuck,
                        len(want), SITE_SEARCH_M)
        log.info("sited %s: %d vehicles, moved up to %.0f m (clear ground)", u.id, len(want), far)

    def _moved(self, u: GroundUnit, pos: dict) -> None:
        p = Pos(float(pos["x"]), float(pos["y"]))
        # Telemetry and despawn report one vehicle's position, not the formation's: only a real
        # move (away from every spot it was given) changes its position and needs siting again.
        ref = u.spots if u.sited and u.spots else [u.pos]
        if min((p.x - q.x) ** 2 + (p.y - q.y) ** 2 for q in ref) > MOVED_M ** 2:
            u.pos = p
            u.sited, u.spots = False, []

    async def _spawn(self, u: GroundUnit) -> None:
        try:
            # Siting is for formations that stand somewhere; marching ones go to the road.
            if not (u.sited and u.spots) and not u.route and u.battle is None:
                await self._site(u)
            spawn = build_ground_spawn(u)
            await self.call(Op.SPAWN_GROUP, spawn.data)
        except Exception as e:  # noqa: BLE001 - any failure: back off, reconcile on next hello
            log.warning("spawn %s failed: %s", u.id, e)
            self.backoff_until[u.id] = self.clock() + FAIL_BACKOFF_S
            if "already exists" in str(e):
                self.orphans.add(group_name(PREFIX_GROUND, u.id))
            return
        finally:
            self.busy.discard(u.id)
        u.spawn = SpawnState.SPAWNED
        u.dcs_group = spawn.data["group"]["name"]
        self.spawned_at[u.id] = self.clock()
        self.unit_types[u.id] = spawn.unit_types
        self.dead[u.id] = set()
        self.missing.pop(u.id, None)
        log.info("spawned %s (%d vehicles)", u.id, len(spawn.unit_types))
        self._changed(u)

    async def _despawn(self, u: GroundUnit) -> None:
        try:
            res = await self.call(Op.DESPAWN_GROUP, {"group": u.dcs_group})
        except Exception as e:  # noqa: BLE001 - leave it spawned; retried next tick
            log.warning("despawn %s failed: %s", u.id, e)
            return
        finally:
            self.busy.discard(u.id)
        res = res or {}
        if not res.get("gone"):
            survivors = res.get("survivors") or {}
            u.composition = {t: int(n) for t, n in survivors.items() if int(n) > 0}
            alive = set(as_list(res.get("alive")))
            self.dead[u.id] = set(self.unit_types.get(u.id, {})) - alive
            pos = res.get("pos")
            if isinstance(pos, dict):
                self._moved(u, pos)
        log.info("despawned %s (%d vehicles left)", u.id, strength(u))
        self._aggregate(u)

    async def _remove_orphan(self, name: str) -> None:
        try:
            await self.call(Op.DESPAWN_GROUP, {"group": name})
            self.orphans.discard(name)
            log.info("removed orphan group %s", name)
        except Exception as e:  # noqa: BLE001
            log.warning("removing orphan %s failed: %s", name, e)

    # ---------- state changes ----------

    def _lost(self, unit_id: str, dcs_name: str, dcs_type: str | None) -> None:
        u = self.state.ground_units.get(unit_id)
        if u is None:
            return
        dead = self.dead.setdefault(unit_id, set())
        if dcs_name in dead:
            return
        dead.add(dcs_name)
        t = self.unit_types.get(unit_id, {}).get(dcs_name) or dcs_type
        if t and u.composition.get(t, 0) > 0:
            u.composition[t] -= 1
            if u.composition[t] == 0:
                del u.composition[t]
        self.engaged_until[unit_id] = self.clock() + ENGAGED_HOLD_S
        log.info("%s lost a %s (%d left)", unit_id, t, strength(u))
        if strength(u) == 0:
            log.info("%s destroyed", unit_id)
            # DCS removes a group whose last unit dies; nothing left to despawn.
            if u.spawn is SpawnState.SPAWNED:
                self._aggregate(u)
            else:
                self._changed(u)

    def _aggregate(self, u: GroundUnit) -> None:
        u.spawn = SpawnState.AGGREGATED
        u.dcs_group = None
        self.spawned_at.pop(u.id, None)
        self.missing.pop(u.id, None)
        self._changed(u)

    def _changed(self, u: GroundUnit) -> None:
        if self.on_change:
            try:
                self.on_change(u)
            except Exception:  # noqa: BLE001
                log.exception("on_change failed for %s", u.id)
