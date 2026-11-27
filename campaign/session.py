"""Players into their jets: the session mission (backlog item 2; decided 2026-11-27).

1. **Join** (the war map): players take seats in a player flight (``Flight.player_seats``).
2. **Commit:** the flight's lead presses it once everyone is in. The server builds the session
   mission: the template (``[server] mission_template``) with a Client group for every committed
   flight still on the ground. Seats with a player are Client slots, the rest AI in the same group
   (the native wingman, Run 25). Each group has the flight's type, loadout, route, callsigns
   (``Uzi 4-1``), parking (checked as for AI ramp starts) and, for a Hornet, each Client seat's
   cartridge, set to load itself. The mission's date and time are the campaign's now. The
   template's own player slots are dropped. The Hooks then load it, so everyone goes back to the
   slot screen. Refused while a player is in a jet: the reload would take it away.
3. **Fly:** from ``FLY_OPEN_S`` before take-off, a seat's Fly button moves its player into their
   slot (``force_player_slot``; they must be blue) and the client script puts them in the
   cockpit. Without the script: take the slot from the slot list. The Hooks keep a held seat for
   its owner and refuse a seat that isn't open yet; a free seat is anyone's, and whoever takes it
   has it.
4. **In the jet:** the Client group is the flight once its first unit is born (``AirSpawner.adopt``);
   the war follows it like any flight in DCS.
5. **Deadline:** nobody in the jets ``LATE_S`` after take-off, or the loaded mission doesn't have
   the flight's group: it goes to the AI (its slots close) and flies as any other flight.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .bridge import BridgeError
from .catalog import flight_cruise_ms
from .dtc import DtcWaypoint, hornet_dtc
from .flight_spawn import Takeoff, build_flight_spawn
from .model import Flight, FlightState, Side, SpawnState, StartMode
from .protocol import as_list
from .seats import FLY_OPEN_S, LATE_S, group_label, seat_name, seat_of

if TYPE_CHECKING:
    from .server import Engine

log = logging.getLogger("campaign")

RELOAD_DELAY_S = 15       # the countdown before the session mission loads
SESSION_FILES = ("campaign-session-a.miz", "campaign-session-b.miz")   # alternating (Run 11)
HORNET = "FA-18C_hornet"
HORNET_MAX_WP = 59
# Internal fuel (kg) for types whose size query gives none (DCS's own fuelMassMax normally).
FUEL_KG = {"FA-18C_hornet": 4900.0, "F-16C_50": 3249.0, "A-10C": 5029.0, "A-10C_2": 5029.0, "F-15C": 6103.0,
           "F-15ESE": 10257.0, "AV8BNA": 3519.0, "M-2000C": 3165.0, "F-14B": 7348.0, "JF-17": 2325.0}


def _hhmm(t: float) -> str:
    s = int(t)
    return f"{s // 3600 % 24:02d}:{s // 60 % 60:02d}"


def cartridge_name(f: Flight, seat: int) -> str:
    return seat_name(f, seat).lower().replace(" ", "_").replace("-", "_")


def client_group(f: Flight, aircraft_type: str, side: Side, takeoff: Takeoff, *, cold: bool = False,
                 fuel_kg: float | None = None, cruise_ms: float | None = None,
                 theatre: str = "Syria") -> tuple[dict[str, Any], dict[str, bytes]]:
    """The flight's Client group for the session mission (the patcher's ``client_groups`` entry,
    tools/miz/patch_mission.lua), and the files it needs in the .miz (cartridges)."""
    start = StartMode.RAMP_COLD if cold else StartMode.RAMP_HOT
    spawn = build_flight_spawn(dataclasses.replace(f, start=start, next_wp=0), aircraft_type, side, takeoff=takeoff,
                               number=f.number or None, speed_ms=cruise_ms)
    g = spawn.data["group"]
    fuel = fuel_kg or FUEL_KG.get(aircraft_type)
    files: dict[str, bytes] = {}
    units = []
    for i, u in enumerate(g["units"], 1):
        player = bool(f.player_seats.get(i))
        pl = u["payload"]
        payload: dict[str, Any] = {"pylons": {int(k): v for k, v in pl["pylons"].items()},
                                   "chaff": pl["chaff"], "flare": pl["flare"], "gun": pl["gun"]}
        if fuel:
            payload["fuel"] = round(fuel * float(pl["fuel_fraction"]), 1)
        spec: dict[str, Any] = {"name": seat_name(f, i), "skill": "Client" if player else u["skill"],
                                "type": aircraft_type, "payload": payload, "callsign": u["callsign"],
                                "onboard_num": u["onboard_num"], "x": u["x"], "y": u["y"], "heading": u["heading"]}
        if "parking" in u:
            spec["parking"] = u["parking"]
        if player and aircraft_type == HORNET:
            name = cartridge_name(f, i)
            pts = [DtcWaypoint(wp.pos, 0.0, wp.action or f"WP{n}")
                   for n, wp in enumerate(f.waypoints[:HORNET_MAX_WP], 1)]
            files[f"DTC/{name}.dtc"] = json.dumps(hornet_dtc(name, theatre, pts), indent=4).encode()
            spec["dtc"] = name
        units.append(spec)
    group = {"name": group_label(f), "airdrome_id": takeoff.airdrome_id, "x": takeoff.pos.x, "y": takeoff.pos.y,
             "start": "cold" if cold else "hot", "task": g["task"], "points": g["route"]["points"], "units": units}
    return group, files


def mission_now(info: dict[str, Any]) -> dict[str, Any]:
    """The patcher's ``date`` and ``start_time`` for a mission starting where the loaded one is now
    (its date plus ``timer.getAbsTime()``)."""
    d = info["date"]
    when = dt.datetime(int(d["Year"]), int(d["Month"]), int(d["Day"])) + dt.timedelta(seconds=float(info["abs"]))
    return {"date": {"Year": when.year, "Month": when.month, "Day": when.day},
            "start_time": when.hour * 3600 + when.minute * 60 + when.second}


class Session:
    """Commit, Fly, the slot rules and the deadline (see the module docstring)."""

    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.building = False
        self.closed: dict[str, str] = {}     # slot id -> why it's closed (its flight went to the AI)
        self._rules_sent: Any = None

    @property
    def st(self):
        return self.e.state

    def tod(self, t: float) -> str:
        wm = self.e.warmap
        return _hhmm(t + (wm.tod_offset if wm is not None else 0.0))

    def pilot(self, ucid: str | None) -> str:
        wm = self.e.warmap
        return (wm.pilots.get(ucid) if wm is not None and ucid else None) or "a player"

    def committed(self) -> list[Flight]:
        return [f for f in self.st.flights.values() if f.committed]

    # ----- where the mission is built -----

    def paths(self) -> tuple[Path, Path, str, str]:
        """(template, output folder, file name, the path the Hooks load: relative to Saved Games' Missions)."""
        from tools.build_test_missions import DEFAULT_TEMPLATE, SAVED_GAMES
        cfg = self.e.settings.server
        template = Path(cfg.mission_template).expanduser() if cfg.mission_template else DEFAULT_TEMPLATE
        out = Path(cfg.missions_dir).expanduser() if cfg.missions_dir else SAVED_GAMES / "Missions" / "campaign-dev"
        name = SESSION_FILES[self.st.session_builds % len(SESSION_FILES)]
        try:
            rel = (out / name).resolve().relative_to((SAVED_GAMES / "Missions").resolve())
        except ValueError:
            raise ValueError(f"missions_dir {out} isn't under {SAVED_GAMES / 'Missions'}, where DCS loads missions from")
        return template, out, name, "\\".join(rel.parts)

    # ----- commit -----

    def commit(self, player_id: int, ucid: str | None, flight_id: str) -> str:
        """The lead commits their flight: checks, then builds and loads the session mission in the
        background. Returns the reply."""
        f = self.st.flights.get(flight_id)
        if f is None or not f.player_preferred:
            return "That flight isn't one players can fly"
        if seat_of(f, ucid) != 1:
            return f"Only {seat_name(f, 1)}, the lead, can commit {f.callsign}"
        if f.committed:
            return f"{f.callsign} is committed already"
        if f.state is not FlightState.PLANNED or f.spawn is SpawnState.SPAWNED:
            return f"{f.callsign} has taken off"
        if self.st.clock > f.takeoff_time + LATE_S:
            return f"Too late: {f.callsign} was due off at {self.tod(f.takeoff_time)}"
        if self.building:
            return "A mission is being built already: try again in a moment"
        flying = [p for p in self.e.players if p.get("player")]
        if flying:
            who = ", ".join(f"{p['player']} ({p.get('name') or p.get('type') or 'a jet'})" for p in flying[:3])
            return (f"Can't restart the mission while {who} {'is' if len(flying) == 1 else 'are'} flying: commit "
                    "once they're out of their jets")
        self.building = True
        self.e.background(self._commit(player_id, f))
        seats = sorted(s for s, who in f.player_seats.items() if who)
        return (f"Committing {f.callsign}: {len(seats)} player{'s' if len(seats) != 1 else ''}"
                + (f", {f.aircraft - len(seats)} AI" if f.aircraft > len(seats) else "")
                + f". Building the mission; it restarts in about {RELOAD_DELAY_S} s")

    async def _commit(self, player_id: int, f: Flight) -> None:
        try:
            f.committed = True
            name = await self.build()
            self.e.say(f"Campaign: {f.callsign} {f.mission} is committed. The mission restarts in {RELOAD_DELAY_S} s "
                       f"with its jets. Pick blue when it's loaded; Fly opens at "
                       f"{self.tod(f.takeoff_time - FLY_OPEN_S)}.", duration=RELOAD_DELAY_S + 5)
            await self.e.hooks_call("load_mission", {"file": name, "delay": RELOAD_DELAY_S})
            log.info("SESSION %s committed by player %s; loading %s in %d s", f.id, player_id, name, RELOAD_DELAY_S)
            self.e.save_if_due(force=True)
        except (Exception, SystemExit) as e:  # noqa: BLE001 - tell the lead, put the flight back (build() exits on a bad patch)
            f.committed = False
            log.warning("SESSION commit of %s failed: %s", f.id, e)
            await self._reply(player_id, f"Commit failed: {e}")
        finally:
            self.building = False

    async def build(self) -> str:
        """Write the session mission for every committed flight still on the ground; returns the
        file name the Hooks load."""
        from tools.build_test_missions import build
        template, out, name, rel = self.paths()
        info = await self.e.mission_call("query", {"what": "mission"})
        groups, files, used = [], {}, {}
        cold = self.e.settings.server.player_start == "cold"
        for f in sorted(self.committed(), key=lambda f: f.takeoff_time):
            if f.state is not FlightState.PLANNED or f.spawn is SpawnState.SPAWNED:
                continue
            sq = self.st.squadrons[f.squadron_id]
            self.e.prepare_flight(f)
            takeoff = await self.e.takeoff_for(f, skip=used.setdefault(sq.base_id, set()))
            used[sq.base_id].update(term for term, _ in takeoff.parking or [])
            fuel = None
            try:
                fuel = (await self.e.mission_call("query", {"what": "type_size", "type": sq.aircraft_type})).get("fuel_max")
            except (BridgeError, asyncio.TimeoutError, AttributeError) as e:
                log.warning("SESSION fuel of %s unknown (%s)", sq.aircraft_type, e)
            group, more = client_group(f, sq.aircraft_type, sq.side, takeoff, cold=cold, fuel_kg=fuel,
                                       cruise_ms=flight_cruise_ms(self.st, f), theatre=str(info.get("theatre") or ""))
            groups.append(group)
            files.update(more)
            f.client_group = group["name"]
        extra = {"client_groups": groups, "drop_players": True, **mission_now(info)}
        path = await asyncio.to_thread(build, template, out, name, "lua/mission/campaign_bridge.lua", "",
                                       "Load campaign bridge", None, True, None, extra, files, None, False)
        self.st.session_mission = rel
        self.st.session_builds += 1
        log.info("SESSION built %s: %s", path, ", ".join(g["name"] for g in groups))
        return rel

    # ----- fly -----

    async def fly(self, player_id: int, ucid: str | None, flight_id: str) -> str:
        f = self.st.flights.get(flight_id)
        seat = seat_of(f, ucid) if f else None
        if f is None or seat is None:
            return "You aren't in that flight"
        me = seat_name(f, seat)
        if not f.committed:
            return f"{f.callsign} isn't committed yet: {seat_name(f, 1)} commits once everyone's in"
        slot = f.slots.get(seat)
        if slot is None:
            return f"{me} has no jet in this mission (it's AI, or the mission hasn't loaded yet)"
        if f.state not in (FlightState.PLANNED, FlightState.TAXI):
            return f"{f.callsign} has taken off"
        if self.st.clock < f.takeoff_time - FLY_OPEN_S:
            return f"Fly opens at {self.tod(f.takeoff_time - FLY_OPEN_S)}, {FLY_OPEN_S // 60} min before take-off"
        res = await self.e.hooks_call("force_slot", {"player_id": player_id, "side": 2, "slot": slot})
        if not (isinstance(res, dict) and res.get("ok")):
            return f"DCS didn't move you into {me}: pick blue in the slot list first (spectators are fine), or take {me} there"
        await self.e.hooks_call("client_rpc", {"player_id": player_id, "method": "campaignEnter", "args": []})
        log.info("SESSION player %s (%s) into %s (slot %s)", player_id, self.pilot(ucid), me, slot)
        return f"Into {me}: {f.mission}, take-off {self.tod(f.takeoff_time)}"

    # ----- the mission, slots and events -----

    async def on_mission(self) -> None:
        """Each mission connection: which committed flights have their jets in this mission (the
        mission bridge lists its Client groups). The others go to the AI."""
        self.closed = {}
        self._rules_sent = None
        try:
            res = await self.e.mission_call("query", {"what": "client_groups"})
        except (BridgeError, asyncio.TimeoutError) as e:
            log.warning("SESSION can't list the mission's Client groups (%s): committed flights keep waiting", e)
            return
        groups = {g.get("name"): as_list(g.get("units")) for g in as_list((res or {}).get("groups")) if isinstance(g, dict)}
        for f in self.committed():
            units = groups.get(f.client_group)
            if f.state is not FlightState.PLANNED:   # flown: done with its jets
                f.committed, f.slots, f.client_group = False, {}, None
                continue
            if units is None:
                self.release(f, "this mission doesn't have its jets")
                continue
            ids = {u.get("name"): str(u.get("id")) for u in units if isinstance(u, dict) and u.get("skill") == "Client"}
            f.slots = {s: ids[seat_name(f, s)] for s in range(1, f.aircraft + 1) if seat_name(f, s) in ids}
            log.info("SESSION %s has its jets: %s", f.callsign,
                     ", ".join(f"{seat_name(f, s)} slot {i}" for s, i in sorted(f.slots.items())) or "no Client seats")
        await self.push_rules()

    def release(self, f: Flight, why: str) -> None:
        """The flight goes to the AI: its slots close, its seats empty."""
        for sid in f.slots.values():
            self.closed[sid] = f"{f.callsign} has gone to the AI"
        f.committed, f.slots, f.client_group = False, {}, None
        f.player_seats = {}
        log.info("SESSION %s goes to the AI: %s", f.id, why)
        self.e.news(f"{f.callsign} {f.mission} goes to the AI: {why}")

    def slot_rules(self) -> dict[str, dict[str, Any]]:
        """Per slot id: {owner, reason} for the Hooks (see commands.slot_rules there)."""
        out: dict[str, dict[str, Any]] = {sid: {"reason": why} for sid, why in self.closed.items()}
        for f in self.committed():
            opens = f.takeoff_time - FLY_OPEN_S
            for seat, sid in f.slots.items():
                owner = f.player_seats.get(seat)
                if self.st.clock < opens:
                    out[sid] = {"reason": f"{seat_name(f, seat)} opens at {self.tod(opens)}, "
                                          f"{FLY_OPEN_S // 60} min before take-off"}
                elif owner:
                    out[sid] = {"owner": owner, "reason": f"{seat_name(f, seat)} is {self.pilot(owner)}'s seat"}
        return out

    async def push_rules(self) -> None:
        rules = self.slot_rules()
        if rules == self._rules_sent:
            return
        try:
            await self.e.hooks_call("slot_rules", {"allowed_sides": self.e.settings.server.slot_sides, "slots": rules})
            self._rules_sent = rules
        except (BridgeError, asyncio.TimeoutError) as e:
            log.warning("SESSION slot rules failed: %s", e)

    async def seats_changed(self) -> None:
        await self.push_rules()

    def on_event(self, data: dict[str, Any]) -> None:
        """A Client unit of a committed flight is born: the flight is in DCS."""
        if data.get("kind") != "birth":
            return
        unit = data.get("unit") or {}
        group = unit.get("group")
        f = next((f for f in self.committed() if f.client_group and f.client_group == group), None)
        if f is None or f.spawn is SpawnState.SPAWNED:
            return
        names = [seat_name(f, s) for s in range(1, f.aircraft + 1)]
        self.e.air.adopt(f, group, names)
        log.info("SESSION %s is in DCS (%s in %s)", f.callsign, unit.get("player") or "AI", unit.get("name"))

    def on_player(self, data: dict[str, Any]) -> None:
        """A player took a campaign slot from the slot list: a free seat is theirs now."""
        if data.get("kind") != "player_slot" or not data.get("ucid") or not data.get("slot"):
            return
        ucid, slot = str(data["ucid"]), str(data["slot"])
        for f in self.committed():
            seat = next((s for s, sid in f.slots.items() if sid == slot), None)
            if seat is None or f.player_seats.get(seat) == ucid:
                continue
            if f.player_seats.get(seat):
                return   # held for someone else: the Hooks should have refused it
            for other in self.st.flights.values():   # one flight at a time
                s_ = seat_of(other, ucid)
                if s_ is not None:
                    other.player_seats[s_] = None
            f.player_seats[seat] = ucid
            if self.e.warmap is not None and data.get("name"):
                self.e.warmap.pilots[ucid] = str(data["name"])
            log.info("SESSION %s took %s from the slot list", data.get("name") or ucid, seat_name(f, seat))
            self.e.background(self.push_rules())
            return

    async def tick(self) -> None:
        """The deadline, and the slot rules as seats open."""
        for f in self.committed():
            if (f.state is FlightState.PLANNED and f.spawn is SpawnState.AGGREGATED
                    and self.st.clock > f.takeoff_time + LATE_S):
                self.release(f, f"nobody got in by {self.tod(f.takeoff_time + LATE_S)}")
            elif f.state in (FlightState.LANDED, FlightState.LOST) and f.spawn is SpawnState.AGGREGATED:
                for sid in f.slots.values():   # flown: its jets aren't campaign flights any more
                    self.closed[sid] = f"{f.callsign} has flown its mission"
                f.committed, f.slots, f.client_group = False, {}, None
        await self.push_rules()

    async def _reply(self, player_id: int, text: str) -> None:
        try:
            await self.e.hooks_call("chat_to", {"player_id": player_id, "text": "> " + text})
        except (BridgeError, asyncio.TimeoutError) as e:
            log.warning("SESSION reply failed: %s", e)
