"""Riding to the jets: Fly as a ride in a Humvee (backlog item 2; proof P22, Runs 29 and 30).

With the client script (version RIDE_VERSION or later) and ``[server] ride_to_jet`` on, Fly doesn't
put the player straight into the cockpit:

1. **Stand-ins.** When the session mission loads, a static of the flight's type stands on each of
   its spots (a Client jet isn't in the world until its player takes the slot, and the AI seats
   only come with the first player). A seat's stand-in goes just before its player is put in;
   the AI seats' go as the first player of the flight gets in (they spawn with them). All go when
   the flight takes off or goes to the AI.
2. **Boarding.** The players don't have to do anything: once the session mission is loaded,
   each committed player who is a blue spectator is boarded by themselves when Fly opens (the
   "briefing room": until then they're told when). Fly does the same by hand. The first of a
   flight starts its ride: a Humvee beside the base's tower (or
   the building nearest the jets), and the player's view in its passenger seat (the client
   script's ``campaignRide``: DCS's own camera on the vehicle, F7 and F4 pressed for them). It
   waits GATHER_S for the rest of the flight: each Fly in that time boards too (front seat, then
   the back seats). A Fly after it has left goes straight into the jet, as without the ride.
3. **The ride** (campaign/ride.py plans it, campaign/taxinet.py gives the taxiways): along the
   taxiways to the row of jets, beside each nose in turn, nearest first, dropping each rider at
   their own jet (into it, as Fly does) and stopping a moment at the others (the AI pilots); then
   straight on and away, and removed out of sight.

Without the client script, or with an older one, Fly works as before.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import ride as plan_ride
from . import taxinet
from .bridge import BridgeError
from .catalog import COUNTRY_FOR_SIDE
from .model import Flight, FlightState, Pos, Side, SpawnState
from .protocol import as_list
from .seats import seat_name

if TYPE_CHECKING:
    from .server import Engine

log = logging.getLogger("campaign")

RIDE_VERSION = 24          # the client script's first version with the ride's camera
VEHICLE = "Hummer"
GATHER_S = 15.0            # the Humvee waits this long after the first rider boards
DROP_S = 6.0               # stopped at each jet this long
ARRIVE_M = 4.0             # this close to the end of a leg: there
SPAWN_WAIT_S = 1.0         # the Humvee has to exist before the cameras go to it
PARK_S = 1.5               # parked this long at a jet before its rider goes in
ENTER_S = 0.5              # in the slot this long before the client script enters the cockpit
POS_S = 0.25               # the Humvee's position to the riders' client scripts this often
STUCK_S = 20.0             # not moving this long: tell the riders
LEG_GIVE_UP_S = 240.0      # a leg taking longer: on to the next stop anyway
SCENERY_M = 1500.0         # the tower and buildings this far around the jets
EYE = (0.2, 0.45, 1.55)    # the front passenger's seat camera (forward, right, up; Run 29)
BACK_SEAT_M = 1.1          # the back seats this far behind it
CHAIRS = ("front passenger seat", "back seat, right", "back seat, left")
KMH = 1 / 3.6
FT = 3.281
# The client script's ride progress (``client_web``, stage "ride") worth telling the rider.
RIDE_CHAT = [
    ("focus: mounted at the seat", "You're in the Humvee: look around as you like."),
    ("native: in the seat", "You're in the Humvee: look around as you like."),
    ("focus: DCS doesn't keep", "You're in the Humvee (the script holds the camera there)."),
    ("focus: chase view", "Behind the Humvee: look around as you like."),
    ("focus: F7 didn't reach", "Couldn't get the view onto the Humvee: placing the camera in it instead."),
    ("native: the vehicle isn't", "Can't see the Humvee from your PC: placing the camera instead."),
    ("let go:", "Your view changed, so the ride let go of the camera. You'll still be put in your jet."),
]


def eye_for(chair: int) -> list[float]:
    """Chair 0 is the front passenger's; 1 and 2 the back seats behind them and behind the driver."""
    f, r, u = EYE
    return [f, r, u] if chair == 0 else [f - BACK_SEAT_M, r if chair == 1 else -r, u]


@dataclass
class RideJet:
    seat: int
    unit: str                  # the unit's name in the mission ("Uzi 4-1")
    slot: str | None           # its slot id, for a Client seat
    type: str
    pos: Pos
    heading: float             # radians from north
    static: str = ""
    up: bool = False           # its stand-in is there

    def __post_init__(self) -> None:
        self.static = self.static or f"Stand-in {self.unit}"


@dataclass
class Rider:
    pid: int
    ucid: str | None
    seat: int
    chair: int
    dropped: bool = False
    viewing: bool = False      # campaignRide sent


@dataclass
class Ride:
    flight_id: str
    group: str
    riders: dict[int, Rider] = field(default_factory=dict)
    boarding: bool = True
    spawned: bool = False
    task: asyncio.Task | None = None

    @property
    def unit(self) -> str:
        return f"{self.group}-1"


@dataclass
class BaseInfo:
    net: taxinet.TaxiNet | None
    start: Pos | None = None
    beside: str = ""


def _dist(a: Pos, b: Pos) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


class Rides:
    """The rides to the jets (see the module docstring); owned by the Session."""

    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.jets: dict[str, list[RideJet]] = {}     # flight id -> its jets in this mission
        self.rides: dict[str, Ride] = {}             # flight id -> its ride, while there is one
        self.bases: dict[str, BaseInfo] = {}         # DCS airbase name -> taxiways and the start
        self.slot_of: dict[int, str] = {}            # player id -> their slot ("" spectators), from the Hooks
        self.theatre = ""
        self.dcs_dir: Path | None = None             # the DCS install (taxiway files); None: tools' default
        self.boarded: set[tuple[str, str]] = set()   # (flight id, ucid): boarded by themselves this mission
        self.told: set[tuple[str, str]] = set()      # (flight id, ucid): told when their ride comes
        self.waiting: dict[str, str] = {}            # ucid -> why they aren't boarded yet (logged on change)

    @property
    def st(self):
        return self.e.state

    # ----- who rides -----

    def can_ride(self, pid: int) -> bool:
        if not getattr(self.e.settings.server, "ride_to_jet", False):
            return False
        wm = self.e.warmap
        v = wm.viewers.get(pid) if wm is not None else None
        try:
            return v is not None and v.version is not None and int(v.version) >= RIDE_VERSION
        except (TypeError, ValueError):
            return False

    def in_a_jet(self, pid: int) -> bool:
        return self.slot_of.get(pid, "") not in ("", "0")

    # ----- the mission: jets and stand-ins -----

    async def on_mission(self, groups: dict[str, list[dict[str, Any]]], flights: list[Flight]) -> None:
        """The session mission is loaded: each committed flight's jets, from its Client group in the
        .miz (``groups``: group name -> units), and their stand-ins."""
        self.jets, self.rides, self.bases = {}, {}, {}
        self.boarded, self.told, self.waiting = set(), set(), {}
        if not getattr(self.e.settings.server, "ride_to_jet", False):
            return
        try:
            info = await self.e.mission_call("query", {"what": "mission"})
            self.theatre = str((info or {}).get("theatre") or "")
        except (BridgeError, asyncio.TimeoutError) as e:
            log.warning("RIDE no theatre (%s): driving straight across the ground", e)
        for f in flights:
            units = {str(u.get("name")): u for u in groups.get(f.client_group or "", []) if isinstance(u, dict)}
            jets = []
            for s in range(1, f.aircraft + 1):
                u = units.get(seat_name(f, s))
                if u is None or not isinstance(u.get("x"), (int, float)) or not isinstance(u.get("y"), (int, float)):
                    continue
                jets.append(RideJet(s, seat_name(f, s), f.slots.get(s), str(u.get("type") or ""),
                                    Pos(float(u["x"]), float(u["y"])), float(u.get("heading") or 0.0)))
            if not jets:
                continue
            base = await self.base(f)
            for j in jets:   # nose towards the taxiway it leaves by (DCS parks them so, Run 30)
                h = plan_ride.parked_heading(base.net, j.pos)
                if h is not None:
                    j.heading = h
            self.jets[f.id] = jets
            if f.spawn is not SpawnState.SPAWNED:
                for j in jets:
                    if j.slot is None or j.slot not in self.slot_of.values():
                        await self.stand_in(j)
            log.info("RIDE %s: %d jets at %s, stand-ins up", f.callsign, len(jets), self.airbase(f) or "?")

    def airbase(self, f: Flight) -> str:
        sq = self.st.squadrons.get(f.squadron_id)
        if sq is None:
            return ""
        name = getattr(self.e, "dcs_airbase", None)
        return name(sq.base_id) if name else sq.base_id

    async def base(self, f: Flight) -> BaseInfo:
        name = self.airbase(f)
        if name in self.bases:
            return self.bases[name]
        net = None
        if name and self.theatre:
            if self.dcs_dir is None:
                from tools.extract_theater import DCS_DIR
                self.dcs_dir = DCS_DIR
            try:
                net = await asyncio.to_thread(taxinet.load, self.dcs_dir, self.theatre, name)
            except (OSError, ValueError) as e:
                log.warning("RIDE can't read %s's taxiways: %s", name, e)
            if net is None:
                log.info("RIDE no taxiway file for %s: the Humvee drives straight", name)
            else:
                log.info("RIDE %s: %d taxiway segments, %.1f km", name, len(net.edges),
                         sum(e.length for e in net.edges) / 1000)
        self.bases[name] = BaseInfo(net)
        return self.bases[name]

    async def start_for(self, base: BaseInfo, jets: list[RideJet]) -> Pos:
        """Where the ride starts: beside the tower or the building nearest the jets (once per base)."""
        if base.start is not None:
            return base.start
        c = Pos(sum(j.pos.x for j in jets) / len(jets), sum(j.pos.y for j in jets) / len(jets))
        found: list[dict[str, Any]] = []
        try:
            res = await self.e.mission_call("query", {"what": "scenery", "center": {"x": c.x, "y": c.y},
                                                      "radius": SCENERY_M, "limit": 400,
                                                      "match": list(plan_ride.TOWER_WORDS + plan_ride.BUILDING_WORDS)})
            found = [o for o in as_list((res or {}).get("found")) if isinstance(o, dict)]
        except (BridgeError, asyncio.TimeoutError) as e:
            log.warning("RIDE scenery query failed: %s", e)
        pick = plan_ride.briefing_point(found, base.net, [j.pos for j in jets])
        if pick is not None:
            base.start, base.beside = pick
        else:   # no building: 300 m off along the taxiways
            far = Pos(c.x + 300, c.y)
            base.start, base.beside = (base.net.nearest(far)[2] if base.net else far), "the taxiway"
        log.info("RIDE start beside %s: x %.0f y %.0f", base.beside, base.start.x, base.start.y)
        return base.start

    async def stand_in(self, j: RideJet) -> None:
        try:
            await self.e.mission_call("remove_static", {"name": j.static})
            await self.e.mission_call("spawn_static", {"country": COUNTRY_FOR_SIDE[Side.BLUE], "static": {
                "name": j.static, "type": j.type, "category": "Planes", "x": j.pos.x, "y": j.pos.y,
                "heading": j.heading, "livery_id": ""}})
            j.up = True
        except (BridgeError, asyncio.TimeoutError) as e:
            log.warning("RIDE stand-in for %s: %s", j.unit, e)

    async def take_down(self, jets: list[RideJet]) -> None:
        for j in jets:
            if not j.up:
                continue
            j.up = False
            try:
                await self.e.mission_call("remove_static", {"name": j.static})
            except (BridgeError, asyncio.TimeoutError) as e:
                log.warning("RIDE removing %s's stand-in: %s", j.unit, e)

    async def getting_in(self, f: Flight, seat: int) -> None:
        """A player is about to be in seat ``seat``: its stand-in goes, and, if the flight isn't in
        DCS yet, the AI seats' too (they spawn with the first player)."""
        jets = self.jets.get(f.id, [])
        first = f.spawn is not SpawnState.SPAWNED
        await self.take_down([j for j in jets if j.seat == seat or (first and j.slot is None)])

    def done_with(self, f: Flight) -> None:
        """The flight took off, or went to the AI: no more stand-ins, rides or stops for it."""
        jets = self.jets.pop(f.id, None)
        if jets:
            self.e.background(self.take_down(jets))
        r = self.rides.get(f.id)
        if r is not None and r.task is not None and not r.task.done():
            r.task.cancel()

    def tick(self) -> None:
        for fid in list(self.jets):
            f = self.st.flights.get(fid)
            if f is None or f.state not in (FlightState.PLANNED, FlightState.TAXI):
                if f is not None:
                    self.done_with(f)
                else:
                    self.jets.pop(fid, None)

    # ----- the Hooks and the client script -----

    def on_player(self, data: dict[str, Any], flights: list[Flight]) -> None:
        kind = data.get("kind")
        pid = data.get("player_id")
        if not isinstance(pid, int):
            return
        if kind == "client_web" and data.get("stage") == "ride":
            detail = str(data.get("detail") or "")
            log.info("RIDE player %s: %s", pid, detail)
            text = next((t for prefix, t in RIDE_CHAT if detail.startswith(prefix)), None)
            if text and any(pid in r.riders and not r.riders[pid].dropped for r in self.rides.values()):
                self.e.background(self.reply(pid, text))
        elif kind == "player_slot":
            slot = str(data.get("slot") or "")
            self.slot_of[pid] = slot
            for f in flights:   # into a campaign jet from the slot list: its stand-ins go first
                seat = next((s for s, sid in f.slots.items() if sid == slot), None)
                if seat is not None and f.id in self.jets:
                    self.e.background(self.getting_in(f, seat))
            if slot not in ("", "0"):
                for r in self.rides.values():
                    rider = r.riders.get(pid)
                    if rider is not None:
                        rider.dropped = True   # in a jet (theirs or not): off the ride
        elif kind == "player_disconnect":
            self.slot_of.pop(pid, None)
            for r in self.rides.values():
                if pid in r.riders:
                    r.riders[pid].dropped = True

    async def reply(self, pid: int, text: str) -> None:
        try:
            await self.e.hooks_call("chat_to", {"player_id": pid, "text": "> " + text})
        except (BridgeError, asyncio.TimeoutError) as e:
            log.warning("RIDE reply failed: %s", e)

    async def rpc(self, pid: int, method: str, *args: Any) -> None:
        try:
            await self.e.hooks_call("client_rpc", {"player_id": pid, "method": method, "args": list(args)})
        except (BridgeError, asyncio.TimeoutError) as e:
            log.warning("RIDE %s to player %s failed: %s", method, pid, e)

    # ----- boarding -----

    async def auto_board(self, flights: list[Flight], opens_at: Callable[[Flight], float],
                         tod: Callable[[float], str]) -> None:
        """The briefing room: committed players waiting as blue spectators board their flight's ride
        by themselves once Fly opens (``opens_at``), and are told when it comes before that."""
        if not getattr(self.e.settings.server, "ride_to_jet", False):
            return
        due = [(f, seat, ucid) for f in flights if f.id in self.jets and f.state is FlightState.PLANNED
               and f.spawn is not SpawnState.SPAWNED
               for seat, ucid in sorted(f.player_seats.items())
               if ucid and seat in f.slots and (f.id, ucid) not in self.boarded]
        if not due:
            return
        try:
            res = await self.e.hooks_call("players", {})
        except (BridgeError, asyncio.TimeoutError) as e:
            log.warning("RIDE can't list the players: %s", e)
            return
        by_ucid = {str(p.get("ucid")): p for p in as_list(res) if isinstance(p, dict) and p.get("ucid")}
        for f, seat, ucid in due:
            p = by_ucid.get(ucid)
            if p is None or not isinstance(p.get("id"), int):
                self._wait(ucid, "not connected")
                continue
            pid, slot = p["id"], str(p.get("slot") or "")
            self.slot_of[pid] = slot
            if slot.isdigit() and slot != "0":
                self._wait(ucid, f"in a jet (slot {slot})")
                continue
            if p.get("side") != 2:
                self._wait(ucid, f"side {p.get('side')}, not blue")
                continue
            if not self.can_ride(pid):
                self._wait(ucid, "no client script 24")
                continue
            opens = opens_at(f)
            if self.st.clock < opens:
                self._wait(ucid, "Fly isn't open yet")
                if (f.id, ucid) not in self.told:
                    self.told.add((f.id, ucid))
                    await self.reply(pid, f"Your ride to {seat_name(f, seat)} comes at {tod(opens)}, 20 min before "
                                          "take-off. Stay a blue spectator; the map's yours till then.")
                continue
            self.boarded.add((f.id, ucid))
            text = self.board(pid, ucid, f, seat)
            self._wait(ucid, "boarded" if text else "the ride had left")
            if text is not None:
                log.info("RIDE player %s boards for %s by themselves", pid, seat_name(f, seat))
                await self.reply(pid, text)

    def _wait(self, ucid: str, why: str) -> None:
        if self.waiting.get(ucid) != why:
            self.waiting[ucid] = why
            log.info("RIDE player %s: %s", ucid, why)

    def board(self, pid: int, ucid: str | None, f: Flight, seat: int) -> str | None:
        """Fly with the ride. The reply, or None if this player can't ride now (Fly goes straight in)."""
        if f.id not in self.jets or self.in_a_jet(pid):
            return None
        r = self.rides.get(f.id)
        if r is not None and not r.boarding:
            return None    # it has left: straight in
        if r is None:
            r = Ride(f.id, f"Ride {f.callsign}")
            self.rides[f.id] = r
            r.task = asyncio.ensure_future(self.run(r, f))
        mine = next((x for x in r.riders.values() if x.seat == seat), None)
        if mine is not None and mine.pid != pid:
            return None
        if pid in r.riders:
            return f"You're on the ride to {seat_name(f, seat)} already: the Humvee leaves shortly"
        if len(r.riders) >= len(CHAIRS):
            return None
        rider = Rider(pid, ucid, seat, len(r.riders))
        r.riders[pid] = rider
        if r.spawned:
            self.e.background(self.view(r, rider))
        where = self.bases.get(self.airbase(f))
        beside = f" beside {where.beside}" if where is not None and where.beside else ""
        return (f"To {seat_name(f, seat)} by Humvee: {CHAIRS[rider.chair]}{beside}. It leaves {GATHER_S:.0f} s after "
                f"the first of {f.callsign} boards, and stops at each of its jets. Your view goes into it now.")

    async def view(self, r: Ride, rider: Rider) -> None:
        rider.viewing = True
        await self.rpc(rider.pid, "campaignRide", {"unit": r.unit, "view": "seat", "eye": eye_for(rider.chair),
                                                   "chase": [12, 5], "look": True, "native": True})

    # ----- the ride -----

    async def run(self, r: Ride, f: Flight) -> None:
        try:
            await self._run(r, f)
        except asyncio.CancelledError:
            log.info("RIDE %s cancelled", r.group)
            await self._end(r, "the ride was called off")
            raise
        except (BridgeError, asyncio.TimeoutError, KeyError, TypeError, ValueError) as e:
            log.warning("RIDE %s failed: %s %s", r.group, type(e).__name__, e)
            await self._end(r, "the ride failed")
            # anyone not dropped yet goes straight in, as Fly does without the ride
            for rider in [x for x in r.riders.values() if not x.dropped]:
                rider.dropped = True
                await self.into_jet(f, rider)
        finally:
            r.boarding = False
            if self.rides.get(r.flight_id) is r:
                del self.rides[r.flight_id]

    async def _end(self, r: Ride, why: str) -> None:
        for rider in r.riders.values():
            if not rider.dropped:
                await self.rpc(rider.pid, "campaignRideStop", why)
        try:
            await self.e.mission_call("despawn_group", {"group": r.group})
        except (BridgeError, asyncio.TimeoutError):
            pass

    async def _run(self, r: Ride, f: Flight) -> None:
        jets = self.jets[f.id]
        base = await self.base(f)
        start = await self.start_for(base, jets)
        legs = plan_ride.plan(base.net, start, [j.pos for j in jets], [j.heading for j in jets])
        speed = float(getattr(self.e.settings.server, "ride_kmh", 20.0)) * KMH
        first = legs[0].points
        hdg = math.atan2(first[1].y - first[0].y, first[1].x - first[0].x) if len(first) > 1 else 0.0
        sq = self.st.squadrons.get(f.squadron_id)
        side = sq.side if sq is not None else Side.BLUE
        await self.e.mission_call("despawn_group", {"group": r.group})
        await self.e.mission_call("spawn_group", {"category": "ground", "country": COUNTRY_FOR_SIDE[side], "group": {
            "name": r.group, "task": "Ground Nothing",
            "units": [{"name": r.unit, "type": VEHICLE, "x": start.x, "y": start.y, "heading": hdg,
                       "skill": "Average", "playerCanDrive": False}],
            "route": {"points": plan_ride.waypoints([start], speed)}}})
        r.spawned = True
        total = sum(leg.length for leg in legs)
        log.info("RIDE %s at %s: %d legs, %.0f m: %s", r.group, base.beside, len(legs), total,
                 ", ".join(f"{jets[leg.jet].unit if leg.jet is not None else 'off'} {leg.length:.0f} m" for leg in legs))
        loop = asyncio.get_running_loop()
        await asyncio.sleep(SPAWN_WAIT_S)
        until = loop.time() + GATHER_S
        while loop.time() < until:
            for rider in list(r.riders.values()):
                if not rider.viewing and not rider.dropped:
                    await self.view(r, rider)
            await self.send_pos(r)
            await asyncio.sleep(POS_S)
        r.boarding = False
        await self.tell(r, f"Off we go: {total * FT:.0f} ft, about {max(1, round(total / speed / 60))} min, "
                           f"stopping at {len(jets)} jet{'s' if len(jets) != 1 else ''}.")
        for leg in legs:
            what = jets[leg.jet].unit if leg.jet is not None else "away"
            await self.retry("set_route", {"group": r.group, "points": plan_ride.waypoints(leg.points, speed)})
            if not await self.drive_leg(r, leg, what):
                break
            if leg.jet is not None:
                await self.at_jet(r, f, jets[leg.jet])
        await self.e.mission_call("despawn_group", {"group": r.group})
        log.info("RIDE %s done", r.group)

    async def tell(self, r: Ride, text: str) -> None:
        for rider in list(r.riders.values()):
            if not rider.dropped:
                await self.reply(rider.pid, text)

    async def retry(self, op: str, data: dict[str, Any], tries: int = 4) -> Any:
        """A mission command that must get through: DCS can be slow while it spawns a player's jet
        (Run 30: a timeout there left the Humvee parked)."""
        for n in range(tries):
            try:
                return await asyncio.wait_for(self.e.mission_call(op, data), 10)
            except asyncio.TimeoutError:
                log.info("RIDE %s: no answer in 10 s (try %d of %d)", op, n + 1, tries)
        raise BridgeError(f"{op}: no answer after {tries} tries")

    async def send_pos(self, r: Ride) -> dict[str, Any] | None:
        """The Humvee, also sent to its riders: {} if DCS didn't answer in time, None if it's gone."""
        try:
            g = await asyncio.wait_for(self.e.mission_call("query", {"what": "group", "group": r.group}), 5)
        except asyncio.TimeoutError:
            return {}
        if not isinstance(g, dict) or not g.get("exists") or not as_list(g.get("units")):
            return None
        u = as_list(g["units"])[0]
        for rider in list(r.riders.values()):
            if not rider.dropped and rider.viewing:
                await self.rpc(rider.pid, "campaignRidePos", {"x": u["pos"]["x"], "y": u["pos"]["y"],
                                                              "alt": u.get("alt"), "hdg": u.get("hdg"),
                                                              "spd": u.get("spd")})
        return u

    async def drive_leg(self, r: Ride, leg: plan_ride.Leg, what: str) -> bool:
        """Until the Humvee is at the end of the leg. False if it's gone."""
        loop = asyncio.get_running_loop()
        began = last_move = loop.time()
        last: Pos | None = None
        while True:
            u = await self.send_pos(r)
            if u == {}:
                await asyncio.sleep(POS_S)
                continue
            if u is None:
                raise BridgeError("the Humvee is gone")
            here = Pos(float(u["pos"]["x"]), float(u["pos"]["y"]))
            now = loop.time()
            if last is None or _dist(here, last) > 0.5:
                last, last_move = here, now
            to_go = _dist(here, leg.to)
            if to_go <= ARRIVE_M:
                return True
            if now - last_move >= STUCK_S:
                log.warning("RIDE %s stuck %.0f m short of %s at x %.0f y %.0f", r.group, to_go, what, here.x, here.y)
                last_move = now
            if now - began >= LEG_GIVE_UP_S:
                log.warning("RIDE %s gave up on %s after %.0f s", r.group, what, LEG_GIVE_UP_S)
                return True
            await asyncio.sleep(POS_S)

    async def at_jet(self, r: Ride, f: Flight, jet: RideJet) -> None:
        """Stopped beside a jet: its rider gets out (into it); otherwise a moment's stop all the same
        (the AI pilot's, or a seat nobody's riding to)."""
        mine = [x for x in r.riders.values() if x.seat == jet.seat and not x.dropped]
        loop = asyncio.get_running_loop()
        until = loop.time() + DROP_S
        if not mine:
            await self.tell(r, f"Stopping at {jet.unit}.")
        await asyncio.sleep(PARK_S)
        for rider in mine:
            rider.dropped = True
            await self.into_jet(f, rider)
        while loop.time() < until:
            if await self.send_pos(r) is None:
                break
            await asyncio.sleep(POS_S)

    async def into_jet(self, f: Flight, rider: Rider) -> None:
        """As Fly without the ride: the stand-in goes, the player into the slot, the cockpit."""
        me = seat_name(f, rider.seat)
        slot = f.slots.get(rider.seat)
        if slot is None:
            await self.rpc(rider.pid, "campaignRideStop", "no jet")
            await self.reply(rider.pid, f"{me} has no jet in this mission")
            return
        if any(s == slot for p, s in self.slot_of.items() if p != rider.pid):
            await self.rpc(rider.pid, "campaignRideStop", "taken")
            await self.reply(rider.pid, f"Someone else is in {me}")
            return
        await self.getting_in(f, rider.seat)
        try:
            res = await self.e.hooks_call("force_slot", {"player_id": rider.pid, "side": 2, "slot": slot})
        except (BridgeError, asyncio.TimeoutError) as e:
            res = {"error": str(e)}
        if not (isinstance(res, dict) and res.get("ok")):
            await self.rpc(rider.pid, "campaignRideStop", "force_slot failed")
            await self.reply(rider.pid, f"DCS didn't move you into {me}: pick blue in the slot list first "
                                        f"(spectators are fine), or take {me} there")
            jet = next((j for j in self.jets.get(f.id, []) if j.seat == rider.seat), None)
            if jet is not None and f.spawn is not SpawnState.SPAWNED:
                await self.stand_in(jet)
            return
        await asyncio.sleep(ENTER_S)
        await self.rpc(rider.pid, "campaignEnter")
        log.info("RIDE player %s into %s (slot %s)", rider.pid, me, slot)
