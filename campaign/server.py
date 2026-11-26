"""The campaign server: one engine process for a whole session (backlog item 6).

    python3 -m campaign.server --scenario khalkhalah [--config config/campaign.toml] [--save PATH] [--new]

It owns the campaign state and everything that changes it:
- the bridge, with the mission (events, telemetry, spawning) and the Hooks (players, slots);
- the ground bubble (``GroundSpawner``), the AI flights in DCS (``AirSpawner``) and the flight
  bubble (``AirBubble``, which also flies the flights that aren't in DCS);
- systems that plug in: the war simulation and the planner, when they exist (items 8 and 2);
- the scenario, which builds the starting state and may add its own displays;
- saving: loaded at startup, written while running and on stop (see docs/saves.md).

Each telemetry report (every 3 s) runs one tick, always in this order:
1. inputs: telemetry updates positions and players (events are applied as they arrive);
2. clock: the campaign clock advances by the DCS model time since the last tick, so a paused
   mission stops the war and time acceleration speeds it up (D1);
3. war: each system's ``advance(t0, t1)``, and the flights that aren't in DCS fly their routes;
4. planning: each system's ``tick()``;
5. bubbles: flights recovered or aborted, flights and formations put into or taken out of DCS;
6. scenario: its ``tick()`` (displays and the like);
7. save, when ``autosave_s`` has passed and something changed.
A tick that is still running when the next report arrives makes that report skip its tick;
the clock catches up on the next one, up to ``MAX_STEP_S`` at once.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import math
import os
import signal
import time
from pathlib import Path
from typing import Any

from .air import AirSpawner
from .air_bubble import AIRBORNE, MAX_STEP_S, AirBubble
from .bridge import Bridge, BridgeError, Connection
from .bubble import NM, attack_targets, strength
from .flight_spawn import Takeoff
from .ghosts import Ghosts
from .model import CampaignState, Flight, FlightState, GroundRole, GroundUnit, Pos, SpawnState, Squadron, Theater
from .persist import Autosave, SaveError, Snapshot, load
from .protocol import Envelope, Hello, Src, as_list
from .settings import Settings, SettingsError, load_settings
from .spawner import GroundSpawner

log = logging.getLogger("campaign")

DEFAULT_CONFIG = Path("config/campaign.toml")
DEFAULT_LOG = Path("build/logs/server.log")
# Parking spot types a fighter fits (Term_Type in DCS's parking list), best first.
FIGHTER_SPOTS = (68, 104, 72)


class System:
    """Something that runs the war each tick: the ground war, the planner. All methods optional.

    ``name`` keys its saved bookkeeping in the save file's ``extra``."""
    name = "system"

    def export(self) -> dict[str, Any]:
        return {}

    def restore(self, data: dict[str, Any]) -> None:
        pass

    def on_hello(self, hello: Hello, new_run: bool) -> None:
        pass

    def on_event(self, data: dict[str, Any]) -> None:
        pass

    def advance(self, t0: float, t1: float) -> None:
        """Run this part of the war from campaign time ``t0`` to ``t1`` (seconds)."""

    async def tick(self) -> None:
        pass


class Scenario:
    """A campaign's starting state, and anything it adds on top (displays, menus, test aids).

    A scenario module in ``campaign/scenarios/`` defines ``make() -> Scenario``."""
    name = "empty"
    theater_map = ""
    magvar = 0.0       # magnetic variation on the map, degrees east: bearings shown to pilots are magnetic
    home = ""          # the airbase ghost bearings (--ghost BRG/NM) are measured from
    theater: Theater | None = None   # the map layer, if the scenario uses one (squadron bases are objective ids)
    announce = False   # also show flight and formation changes as DCS messages (test scenarios)

    def new_state(self) -> CampaignState:
        return CampaignState(theater_map=self.theater_map, clock=0.0, sides={})

    def systems(self, engine: Engine) -> list[System]:
        return []

    async def setup(self, engine: Engine) -> None:
        """Once per campaign, on the first mission connection (DCS can be queried): fill the state."""

    async def on_mission(self, engine: Engine, new_run: bool) -> None:
        """On every mission connection, after ``setup``: e.g. warehouse stock, drawings, menus."""

    def on_event(self, engine: Engine, data: dict[str, Any]) -> None:
        pass

    async def tick(self, engine: Engine) -> None:
        pass


def load_scenario(name: str) -> Scenario:
    try:
        module = importlib.import_module(f"campaign.scenarios.{name}")
    except ModuleNotFoundError as e:
        if e.name != f"campaign.scenarios.{name}":
            raise
        raise SystemExit(f"no scenario {name!r} (looked for campaign/scenarios/{name}.py)") from None
    return module.make()


class WarClock:
    """Turns DCS model time (telemetry ``t``) into campaign time steps."""

    def __init__(self) -> None:
        self.last_t: float | None = None

    def reset(self) -> None:
        """A new mission run: its model time starts again from 0."""
        self.last_t = None

    def step(self, t: float) -> float:
        if self.last_t is None or t < self.last_t:
            self.last_t = t
            return 0.0
        dt = min(t - self.last_t, MAX_STEP_S)
        self.last_t = t
        return dt


class Engine:
    def __init__(self, settings: Settings, scenario: Scenario, snap: Snapshot | None = None, *,
                 save_path: Path | None = None, mission_call=None, hooks_call=None,
                 ghosts: list[str] | None = None) -> None:
        self.settings = settings
        self.scenario = scenario
        self.ghosts = Ghosts(ghosts or [])   # test aid: fixed points the bubbles treat as players
        self.state: CampaignState = snap.state if snap else scenario.new_state()
        self.bridge: Bridge | None = None
        self._mission_call = mission_call
        self._hooks_call = hooks_call
        cfg = settings.bubble
        self.ground = GroundSpawner(self.state, self.mission_call, cfg, clock=self.now, on_change=self._ground_changed,
                                    targets=self.flight_targets)
        self.air = AirSpawner(self.state, self.mission_call, clock=self.now, notify=self._flight_note)
        self.air_bubble = AirBubble(self.state, self.air, cfg, base_pos=self.base_pos, takeoff_for=self.takeoff_for)
        self.air_bubble.prepare = self.prepare_flight
        self.systems: list[System] = scenario.systems(self)
        self.clock = WarClock()
        extra = snap.extra if snap else {}
        self.ready = bool(extra.get("scenario_ready"))
        self.ground.restore(extra.get("ground") or {})
        self.air.restore(extra.get("air") or {})
        self.air_bubble.restore(extra.get("air_bubble") or {})
        for s in self.systems:
            s.restore((extra.get("systems") or {}).get(s.name) or {})
        self.runs = snap.runs if snap else {}
        self.airbases: dict[str, dict] = {}     # DCS airbase name -> {name, id, pos, alt, ...}
        self.players: list[dict] = []           # telemetry "players" entries
        self.model_t: float | None = None       # DCS model time of the latest telemetry
        self.exits: dict[str, Flight] = {}      # flights that reached an EXIT waypoint, for the next tick
        self.tick_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task[Any]] = set()
        self.autosave = Autosave(save_path, self.snapshot) if save_path else None
        self.last_save = time.monotonic()

    # ---------- time and DCS ----------

    def now(self) -> float:
        """The campaign clock: what dwell times, pins and watchdogs are measured on."""
        return self.state.clock

    async def mission_call(self, op: str, data: dict[str, Any]) -> Any:
        if self._mission_call:
            return await self._mission_call(op, data)
        if self.bridge is None:
            raise BridgeError("no bridge")
        return await self.bridge.call(Src.MISSION, op, data)

    async def hooks_call(self, op: str, data: dict[str, Any]) -> Any:
        if self._hooks_call:
            return await self._hooks_call(op, data)
        if self.bridge is None:
            raise BridgeError("no bridge")
        return await self.bridge.call(Src.HOOKS, op, data)

    def background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def say(self, text: str, duration: int = 15, audience: str = "all", log_it: bool = True) -> None:
        """A message in DCS (and on the console). Never raises; a failure is logged."""
        if log_it:
            log.info("MSG %s", text.replace("\n", " | "))

        async def send() -> None:
            try:
                await self.mission_call("message", {"audience": audience, "text": text, "duration": duration})
            except (BridgeError, asyncio.TimeoutError) as e:
                log.warning("message failed: %s", e)

        self.background(send())

    # ---------- airbases ----------

    def airbase_pos(self, name: str) -> Pos | None:
        ab = self.airbases.get(name)
        return Pos(ab["pos"]["x"], ab["pos"]["y"]) if ab else None

    def dcs_airbase(self, base_id: str) -> str:
        """The DCS airbase name for a squadron's base: an objective id of the scenario's map layer, or
        (scenarios without one) the DCS name itself. Anything else (e.g. "offmap") has no airbase."""
        th = self.scenario.theater
        o = th.objectives.get(base_id) if th else None
        return o.dcs_airbase if o and o.dcs_airbase else base_id

    def base_pos(self, sq: Squadron) -> Pos | None:
        return self.airbase_pos(self.dcs_airbase(sq.base_id))

    def prepare_flight(self, f: Flight) -> None:
        """Before a flight goes into DCS: its LAND waypoint needs DCS's id for its base (planned
        flights name the base's objective, ``params["base"]``). A base DCS doesn't know: fly there."""
        for wp in f.waypoints:
            if wp.action == "LAND" and "airdrome_id" not in wp.params:
                ab = self.airbases.get(self.dcs_airbase(str(wp.params.get("base", ""))))
                if ab is not None and "id" in ab:
                    wp.params["airdrome_id"] = int(ab["id"])
                else:
                    log.warning("%s: no DCS airbase for %s; it flies there instead of landing", f.id,
                                wp.params.get("base"))
                    wp.action = None

    async def takeoff_for(self, f: Flight) -> Takeoff:
        name = self.dcs_airbase(self.state.squadrons[f.squadron_id].base_id)
        ab = self.airbases.get(name)
        if ab is None:
            raise ValueError(f"no airbase {name!r} in DCS")
        res = await self.mission_call("query", {"what": "parking", "airbase": name, "free_only": True})
        spots = [s for s in as_list(res.get("spots")) if s.get("type") in FIGHTER_SPOTS]
        spots.sort(key=lambda s: (FIGHTER_SPOTS.index(s["type"]), s.get("dist_rw") or 0))
        if len(spots) < f.aircraft:
            raise ValueError(f"only {len(spots)} free fighter parking spots at {name}")
        return Takeoff(airdrome_id=int(ab["id"]), pos=Pos(ab["pos"]["x"], ab["pos"]["y"]),
                       elev_m=float(ab.get("alt") or 0.0),
                       parking=[(int(s["term"]), Pos(s["pos"]["x"], s["pos"]["y"])) for s in spots[:f.aircraft]])

    # ---------- bridge callbacks ----------

    async def on_hello(self, conn: Connection, hello: Hello, new_run: bool) -> None:
        log.info("HELLO %s run=%s new_run=%s map=%s", conn.src, hello.run_id, new_run, hello.map)
        if conn.src == Src.MISSION:
            await self.mission_connected(hello, new_run)
        elif conn.src == Src.HOOKS:
            try:
                await self.hooks_call("slot_rules", {"allowed_sides": self.settings.server.slot_sides})
            except (BridgeError, asyncio.TimeoutError) as e:
                log.warning("slot rules failed: %s", e)

    async def mission_connected(self, hello: Hello, new_run: bool) -> None:
        self.ground.on_hello(hello, new_run)
        self.air.on_hello(hello, new_run)
        for s in self.systems:
            s.on_hello(hello, new_run)
        if new_run:
            self.air_bubble.on_new_run()
            self.clock.reset()
        try:
            res = await self.mission_call("query", {"what": "airbases"})
            self.airbases = {ab["name"]: ab for ab in as_list(res.get("airbases")) if isinstance(ab, dict)
                             and ab.get("name") and isinstance(ab.get("pos"), dict)}
            if not self.ready:
                await self.scenario.setup(self)
                self.ready = True
                log.info("scenario %s set up: %s", self.scenario.name, self.summary())
            await self.scenario.on_mission(self, new_run)
            if self.ghosts:
                for line in self.ghosts.place(self.airbase_pos, self.scenario.home, self.scenario.magvar):
                    log.info(line)
                await self.ghosts.draw(self.mission_call)
            if new_run:
                await self.mission_call("menu", {"audience": "all", "items": [{"label": "Status", "key": "status"}]})
        except (BridgeError, asyncio.TimeoutError) as e:
            log.error("mission setup failed: %s", e)
        self.save_if_due(force=True)

    def on_disconnect(self, conn: Connection) -> None:
        log.info("DISCONNECT %s", conn.src)
        if conn.src == Src.MISSION:
            self.save_if_due(force=True)

    def on_message(self, conn: Connection, env: Envelope) -> None:
        if env.type == "telemetry":
            self.on_telemetry(env.t, env.data)
            self.background(self.tick())
        elif env.type == "event":
            self.on_event(env.data)
        elif env.type == "player":
            self.on_player(env.data)

    def on_telemetry(self, t: float | None, data: dict[str, Any]) -> None:
        self.players = [p for p in as_list(data.get("players")) if isinstance(p, dict)]
        if t is not None:
            self.model_t = float(t)
        data = self.ghosts.inject(data)   # the bubbles see ghosts as players; status doesn't
        self.ground.on_telemetry(data)
        for f in self.air.on_telemetry(data):
            self.exits[f.id] = f
        self.air_bubble.observe(data)

    def on_event(self, data: dict[str, Any]) -> None:
        kind = data.get("kind")
        if kind in ("unit_killed", "kill", "birth", "takeoff", "land", "ejection", "unit_removed"):
            u = data.get("unit") or data.get("victim") or {}
            log.info("EVENT %s %s (%s)%s", kind, u.get("name"), u.get("type"),
                     f" player {u['player']}" if u.get("player") else "")
        self.ground.on_event(data)
        self.air.on_event(data)
        self.air_bubble.on_event(data)
        for s in self.systems:
            s.on_event(data)
        self.scenario.on_event(self, data)
        if kind == "menu_select" and data.get("key") == "status":
            self.say(self.status_text(), duration=30)

    def on_player(self, data: dict[str, Any]) -> None:
        kind = data.get("kind")
        if kind in ("player_connect", "player_disconnect", "player_slot", "slot_denied"):
            log.info("PLAYER %s %s side=%s slot=%s%s", kind, data.get("name") or data.get("player_id"),
                     data.get("side"), data.get("slot"), f" ({data['reason']})" if data.get("reason") else "")

    # ---------- the tick ----------

    async def tick(self) -> None:
        if self.tick_lock.locked():
            return
        async with self.tick_lock:
            # 2-3: clock and war
            dt = self.clock.step(self.model_t) if self.model_t is not None else 0.0
            if dt > 0:
                t0 = self.state.clock
                self.air_bubble.advance(dt)  # the clock, and the flights that aren't in DCS
                for s in self.systems:
                    s.advance(t0, self.state.clock)
            # 4: planning
            for s in self.systems:
                await self._guard(s.tick(), s.name)
            # 5: bubbles
            exits, self.exits = list(self.exits.values()), {}
            await self._guard(self.air.tick(exits), "air")
            await self._guard(self.air_bubble.tick(), "air bubble")
            await self._guard(self.ground.tick(), "ground bubble")
            # 6-7
            await self._guard(self.scenario.tick(self), "scenario")
            self.save_if_due()

    async def _guard(self, coro, what: str) -> None:
        """One part failing must not stop the rest of the tick."""
        try:
            await coro
        except Exception:  # noqa: BLE001
            log.exception("%s failed this tick", what)

    # ---------- saving ----------

    def snapshot(self) -> Snapshot:
        runs = dict(self.runs)
        if self.bridge is not None:
            runs.update({src: run for src in (Src.MISSION, Src.HOOKS) if (run := self.bridge.run_state(src))})
        extra = {"scenario": self.scenario.name, "scenario_ready": self.ready,
                 "ground": self.ground.export(), "air": self.air.export(), "air_bubble": self.air_bubble.export(),
                 "systems": {s.name: s.export() for s in self.systems}}
        return Snapshot(self.state, runs=runs, extra=extra)

    def save_if_due(self, force: bool = False) -> None:
        if self.autosave is None:
            return
        if not force and time.monotonic() - self.last_save < self.settings.server.autosave_s:
            return
        self.last_save = time.monotonic()
        try:
            if self.autosave.maybe_save():
                log.info("saved %s", self.autosave.path)
        except (SaveError, OSError) as e:
            log.error("save failed: %s", e)

    def save_now(self) -> None:
        if self.autosave is None:
            return
        try:
            if self.autosave.maybe_save():  # unchanged since the last save: nothing to write
                log.info("saved %s", self.autosave.path)
        except (SaveError, OSError) as e:
            log.error("final save failed: %s", e)

    # ---------- reporting ----------

    def _flight_note(self, f: Flight, what: str) -> None:
        log.info("FLIGHT %s %s: %s", self.clock_text(), f.callsign, what)
        if self.scenario.announce:
            self.say(f"{f.callsign}: {what}", log_it=False)

    def _ground_changed(self, u: GroundUnit) -> None:
        text = f"{u.name}: {u.spawn.name.lower()}, {strength(u)} vehicles"
        log.info("GROUND %s %s", self.clock_text(), text)
        if self.scenario.announce:
            self.say(text, log_it=False)

    def flight_targets(self) -> tuple[set[str], set[str]]:
        st = self.state
        sides = {f.id: st.squadrons[f.squadron_id].side for f in st.flights.values() if f.squadron_id in st.squadrons}
        return attack_targets(st.flights.values(), st.ground_units.values(), sides, self.settings.bubble)

    def bra(self, pos: Pos | None) -> str:
        """Magnetic bearing and range from the first player, e.g. '355M 12 nm'."""
        me = (self.players[0].get("pos") or {}) if self.players else {}
        if pos is None or "x" not in me:
            return "?"
        dx, dy = pos.x - me["x"], pos.y - me["y"]
        return f"{(math.degrees(math.atan2(dy, dx)) - self.scenario.magvar) % 360:03.0f}M {math.hypot(dx, dy) / NM:.0f} nm"

    def clock_text(self) -> str:
        s = int(self.state.clock)
        return f"T+{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"

    def summary(self) -> str:
        st = self.state
        flights = st.flights.values()
        live_veh = sum(strength(u) for u in st.ground_units.values() if u.spawn is SpawnState.SPAWNED)
        return (f"{self.clock_text()}, {len(self.players)} player(s); "
                f"flights: {sum(f.state in AIRBORNE for f in flights)} airborne "
                f"({sum(f.spawn is SpawnState.SPAWNED for f in flights)} in DCS), "
                f"{sum(f.state is FlightState.PLANNED for f in flights)} planned, "
                f"{sum(f.state in (FlightState.LANDED, FlightState.LOST) for f in flights)} done; "
                f"ground: {sum(u.spawn is SpawnState.SPAWNED for u in st.ground_units.values())}"
                f"/{len(st.ground_units)} formations in DCS ({live_veh} vehicles)")

    def status_text(self) -> str:
        """Bearings and ranges are from the first player (magnetic, nm)."""
        st, cfg = self.state, self.settings.bubble
        lines = [f"CAMPAIGN {self.scenario.name}: {self.summary()}",
                 f"Flights spawn within {cfg.air_spawn / NM:.0f} nm, leave DCS beyond {cfg.air_despawn / NM:.0f} nm; "
                 f"formations spawn within {cfg.ground_spawn / NM:.0f} nm (SAMs further)."]
        flights = list(st.flights.values())
        if len(flights) > 12:
            # Many (the planner): those in DCS, and the next few to take off.
            soon = sorted((f for f in flights if f.state is FlightState.PLANNED), key=lambda f: f.takeoff_time)[:6]
            shown_f = [f for f in flights if f.spawn is SpawnState.SPAWNED] + soon
        else:
            shown_f = flights
        for f in shown_f:
            where = "[DCS]" if f.spawn is SpawnState.SPAWNED else "[engine]"
            if f.state is FlightState.PLANNED:
                sq = st.squadrons.get(f.squadron_id)
                base = self.scenario.theater.objectives.get(sq.base_id) if sq and self.scenario.theater else None
                what = f" {sq.aircraft_type} from {base.name if base else sq.base_id}" if sq else ""
                t = int(f.takeoff_time)
                lines.append(f"{f.callsign}: {f.aircraft}x {f.mission}{what}, takeoff T+{t // 3600}:{t // 60 % 60:02d}")
                continue
            alt = f" {f.alt_m / 0.3048:,.0f} ft" if f.alt_m is not None and f.state in AIRBORNE else ""
            orbit = (f", orbiting {int(f.hold_until - st.clock) // 60} min more"
                     if f.hold_until is not None and f.state in AIRBORNE else "")
            pos = f" {self.bra(f.pos)}{alt}" if f.state in AIRBORNE or f.spawn is SpawnState.SPAWNED else ""
            lines.append(f"{f.callsign}: {f.aircraft}x {f.mission} {f.state.name.lower()} {where}{pos}{orbit}")
        if len(shown_f) < len(flights):
            lines.append(f"(and {len(flights) - len(shown_f)} more flights not in DCS)")
        alive = [u for u in st.ground_units.values() if strength(u) > 0]
        units = [u for u in alive if u.role is not GroundRole.GARRISON]
        shown = units if len(units) <= 15 else [u for u in units if u.spawn is SpawnState.SPAWNED]
        for u in shown:
            lines.append(f"{u.name}: {strength(u)} veh {'[DCS]' if u.spawn is SpawnState.SPAWNED else '[engine]'} "
                         f"{self.bra(u.pos)}")
        if len(shown) < len(units):
            lines.append(f"(and {len(units) - len(shown)} formations not in DCS)")
        gar = [u for u in alive if u.role is GroundRole.GARRISON]
        if gar:
            live = [u for u in gar if u.spawn is SpawnState.SPAWNED]
            lines.append(f"Garrisons: {len(gar)}, {len(live)} in DCS ({sum(strength(u) for u in live)} veh)")
        for sq in st.squadrons.values():
            lines.append(f"{sq.name}: {sq.aircraft_ready} ready")
        for sys_ in self.systems:
            lines += getattr(sys_, "status_lines", lambda: [])()
        return "\n".join(lines)

    async def report_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.server.status_s)
            log.info("STATUS %s", self.summary())

    # ---------- running ----------

    async def run(self, stop: asyncio.Event) -> None:
        cfg = self.settings.server
        self.bridge = Bridge(self.on_message, self.on_hello, self.on_disconnect, host=cfg.host, port=cfg.port)
        for src, (run_id, seq) in self.runs.items():
            self.bridge.restore_run(src, run_id, seq)
        await self.bridge.start()
        log.info("scenario %s; listening on %s:%d; waiting for DCS", self.scenario.name, cfg.host, self.bridge.port)
        reporter = asyncio.create_task(self.report_loop())
        try:
            await stop.wait()
        finally:
            reporter.cancel()
            log.info("saving and closing")
            async with self.tick_lock:  # let a running tick finish, so the save is consistent
                self.save_now()
            await self.bridge.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the campaign server.")
    ap.add_argument("--scenario", help="scenario name (campaign/scenarios/NAME.py); "
                                       "with an existing save, it must match the save's")
    ap.add_argument("--config", type=Path,
                    help=f"settings TOML (default {DEFAULT_CONFIG}, if it exists)")
    ap.add_argument("--save", type=Path, help="save file (default: [server] save in the settings)")
    ap.add_argument("--new", action="store_true",
                    help="start the scenario over; the old save is kept as <save>.1 once this one saves")
    ap.add_argument("--port", type=int, help="bridge port (default: [server] port)")
    ap.add_argument("--ghost", action="append", default=[], metavar="BRG/NM|AIRBASE",
                    help="test aid: a ghost player the bubbles treat as real, at a magnetic bearing/range (nm) from "
                         "the scenario's home airbase (e.g. 090/100) or on an airbase by name; repeat for more")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG,
                    help=f"also append the log to this file (default {DEFAULT_LOG}); no need for tee")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if str(args.log):
        args.log.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")
    config = args.config or (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None)
    try:
        settings = load_settings(config)
        if args.port is not None:
            settings.server.port = args.port
    except (SettingsError, OSError) as e:
        raise SystemExit(f"bad settings: {e}")
    save_path = args.save or Path(settings.server.save)

    snap = None
    if not args.new and save_path.exists():
        try:
            snap = load(save_path)
        except SaveError as e:
            raise SystemExit(f"can't load the save: {e}\n(fix it, pick an older {save_path.name}.N with "
                             f"--save, or start over with --new)")
    saved = snap.extra.get("scenario") if snap else None
    name = args.scenario or saved
    if not name:
        raise SystemExit("no scenario: pass --scenario NAME (e.g. khalkhalah)")
    if saved and name != saved:
        raise SystemExit(f"{save_path} is a {saved!r} campaign, not {name!r}; use --new to start {name!r} over "
                         f"(the old save is kept), or --save to pick another file")
    scenario = load_scenario(name)
    log.info("%s %s (scenario %s)", "loaded" if snap else "new campaign:", save_path, name)
    try:
        engine = Engine(settings, scenario, snap, save_path=save_path, ghosts=args.ghost)
    except ValueError as e:
        raise SystemExit(str(e))

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        def on_signal() -> None:
            if stop.is_set():   # a second Ctrl-C: give up on a clean stop
                log.warning("stopping now, without waiting")
                os._exit(1)
            log.info("stopping (Ctrl-C again to quit at once)")
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, on_signal)
            except NotImplementedError:  # Windows: Ctrl-C raises KeyboardInterrupt instead
                pass
        await engine.run(stop)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
