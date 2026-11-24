"""Test scenario around Khalkhalah (Syria): the bubble and flight demos on the campaign server.

Ground formations (aggregated until a player comes within the bubble distances):
- blue1: 4 M-1 Abrams, 2 nm south of the field;
- near1: red BTRs and trucks, 3 nm north;  armor1: red T-72s and BMPs, 13 nm north (the CAS target);
- mech1: red BTRs, 24 nm north;  sa6: a red SA-6 battery, 32 nm east.

AI flights, on the campaign clock from the start:
- Red Ferry: 2 unarmed MiG-29A from 60 nm north (off the map), orbit 35 nm north for 15 min, leave
  to the north. Weapons hold.
- Blue CAS: 2 F/A-18C, hot ramp start at Khalkhalah at +5 min, attack armor1, land again.
- Blue Ferry: 2 F/A-18C from the airbase closest to 100 nm away, taking off at +1 min, landing at
  Khalkhalah.

Use build/campaign-smoke.miz (or any mission with the bridge). Every formation and flight is
labelled on the F10 map where the engine has it: "[DCS]" when it's in DCS, "[engine]" when it's
data only; F10 > Other > Campaign > Status lists them.
"""
from __future__ import annotations

import asyncio
import math
from pathlib import Path

from campaign.bridge import BridgeError
from campaign.bubble import NM, strength
from campaign.model import (Flight, FlightState, GroundOrder, GroundRole, GroundUnit, Pos, Side, SpawnState, Squadron,
                            StartMode, Waypoint)
from campaign.server import Engine, Scenario

HOME = "Khalkhalah"
FAR_CANDIDATES = ["Damascus", "Mezzeh", "Marj Ruhayyil", "Rayak", "Al Qusayr", "Shayrat", "Tiyas", "Palmyra",
                  "King Hussein Air College", "Mafraq", "Prince Hassan", "H4", "Ramat David", "Haifa",
                  "Beirut-Rafic Hariri", "Kiryat Shmona", "Rosh Pina"]
FAR_NM = 100
FT = 0.3048
# (id, side, role, composition, nm north of HOME, nm east)
FORMATIONS = [
    ("blue1", Side.BLUE, GroundRole.ARMOR, {"M-1 Abrams": 4}, -2, 0),
    ("near1", Side.RED, GroundRole.MECH, {"BTR-80": 2, "Ural-375": 2}, 3, 0),
    ("armor1", Side.RED, GroundRole.ARMOR, {"T-72B": 4, "BMP-2": 4}, 13, 0),
    ("mech1", Side.RED, GroundRole.MECH, {"BTR-80": 6, "Ural-375": 2}, 24, 0),
    ("sa6", Side.RED, GroundRole.SAM, {"Kub 1S91 str": 1, "Kub 2P25 ln": 3}, 0, 32),
]
CAS_TARGET = "armor1"
CAS_LOADOUT = "AIM-9X*2, AIM-120C-5*1, AGM-65D*4, ATFLIR, FUEL"
COLOR = {Side.RED: [1, 0.2, 0.2, 1], Side.BLUE: [0.3, 0.55, 1, 1]}
GREY = [0.65, 0.65, 0.65, 1]
DRAW_BASE = 5000
DRAW_EVERY_S = 10.0   # campaign seconds between label refreshes


class Khalkhalah(Scenario):
    name = "khalkhalah"
    theater_map = "Syria"
    magvar = 5.0      # Syria, near Khalkhalah
    home = HOME
    announce = True   # a test scenario: show every flight and formation change in DCS

    def __init__(self) -> None:
        self.draw_ids: dict[str, int] = {}
        self.drawn: dict[str, tuple] = {}   # key -> what is on the map now, to redraw only changes
        self.last_draw = -math.inf

    async def setup(self, engine: Engine) -> None:
        home = engine.airbase_pos(HOME)
        if home is None:
            raise BridgeError(f"airbase {HOME} not found: is this a Syria mission?")
        st = engine.state
        for fid, side, role, comp, north, east in FORMATIONS:
            st.ground_units[fid] = GroundUnit(id=fid, side=side, name=fid, role=role, composition=dict(comp),
                                              pos=Pos(home.x + north * NM, home.y + east * NM), location_id=None,
                                              order=GroundOrder.HOLD)
        far = self.far_base(engine, home)
        hid = int(engine.airbases[HOME]["id"])
        land_home = Waypoint(home, float(engine.airbases[HOME].get("alt") or 0), action="LAND",
                             params={"airdrome_id": hid})
        squadrons = [
            Squadron(id="red", side=Side.RED, name="Red MiG-29s (off map)", aircraft_type="MiG-29A", base_id="offmap",
                     roles={"FERRY"}, aircraft_ready=8, aircraft_damaged=0, pilots=8, player_flyable=False),
            Squadron(id="blue_home", side=Side.BLUE, name=f"Blue Hornets ({HOME})", aircraft_type="FA-18C_hornet",
                     base_id=HOME, roles={"CAS"}, aircraft_ready=8, aircraft_damaged=0, pilots=8,
                     player_flyable=False),
        ]
        north = lambda d: Pos(home.x + d * NM, home.y)  # noqa: E731
        target = st.ground_units[CAS_TARGET].pos
        flights = [
            Flight(id="redferry", package_id="test", squadron_id="red", callsign="Red Ferry", mission="FERRY",
                   aircraft=2, loadout="", takeoff_time=st.clock, start=StartMode.AIR, waypoints=[
                       Waypoint(north(60), 20_000 * FT),
                       Waypoint(north(35), 20_000 * FT, action="ORBIT", params={"duration_s": 900}),
                       Waypoint(north(90), 25_000 * FT, action="EXIT")]),
            Flight(id="blucas", package_id="test", squadron_id="blue_home", callsign="Blue CAS", mission="CAS",
                   aircraft=2, loadout=CAS_LOADOUT, takeoff_time=st.clock + 300, start=StartMode.RAMP_HOT,
                   waypoints=[Waypoint(north(5), 12_000 * FT),
                              Waypoint(target, 12_000 * FT, action="CAS", params={"radius_m": 4 * NM}),
                              # egress well clear of the ingress point and the attack (9 km zone)
                              Waypoint(Pos(home.x + 2 * NM, home.y - 8 * NM), 10_000 * FT),
                              land_home]),
        ]
        if far:
            fp = engine.airbase_pos(far)
            d = math.dist((fp.x, fp.y), (home.x, home.y))
            toward = lambda k: Pos(fp.x + (home.x - fp.x) * k, fp.y + (home.y - fp.y) * k)  # noqa: E731
            squadrons.append(Squadron(id="blue_far", side=Side.BLUE, name=f"Blue Hornets ({far})",
                                      aircraft_type="FA-18C_hornet", base_id=far, roles={"FERRY"}, aircraft_ready=8,
                                      aircraft_damaged=0, pilots=8, player_flyable=False))
            flights.append(Flight(
                id="bluferry", package_id="test", squadron_id="blue_far", callsign="Blue Ferry", mission="FERRY",
                aircraft=2, loadout="", takeoff_time=st.clock + 60, start=StartMode.RAMP_HOT, waypoints=[
                    Waypoint(toward(5 * NM / d), 20_000 * FT),
                    Waypoint(toward(1 - 15 * NM / d), 12_000 * FT),
                    land_home]))
        for sq in squadrons:
            st.squadrons[sq.id] = sq
        for f in flights:
            st.flights[f.id] = f

    @staticmethod
    def far_base(engine: Engine, home: Pos) -> str | None:
        best = None
        for name in FAR_CANDIDATES:
            p = engine.airbase_pos(name)
            if p is not None:
                err = abs(math.dist((p.x, p.y), (home.x, home.y)) / NM - FAR_NM)
                if best is None or err < best[0]:
                    best = (err, name)
        return best[1] if best else None

    async def on_mission(self, engine: Engine, new_run: bool) -> None:
        if new_run:
            # A ramp start takes its aircraft from the airbase warehouse (P2).
            for sq in engine.state.squadrons.values():
                if sq.base_id in engine.airbases:
                    await engine.mission_call("warehouse_set", {"airbase": sq.base_id,
                                                                "aircraft": {sq.aircraft_type: 4}})
            self.drawn.clear()  # the old drawings went with the old mission
            self.last_draw = -math.inf
            engine.say(f"Campaign server: scenario {self.name}. The F10 map labels every formation and flight: "
                       "[DCS] = in DCS, [engine] = data only. F10 > Other > Campaign > Status for a list.",
                       duration=20)

    async def tick(self, engine: Engine) -> None:
        if engine.state.clock - self.last_draw < DRAW_EVERY_S:
            return
        self.last_draw = engine.state.clock
        st = engine.state
        for u in st.ground_units.values():
            alive = strength(u) > 0
            tag = "DCS" if u.spawn is SpawnState.SPAWNED else "engine"
            await self.label(engine, f"g:{u.id}", u.pos, f"{u.name}: {strength(u)} veh [{tag}]" if alive
                             else f"{u.name}: destroyed", COLOR[u.side] if u.spawn is SpawnState.SPAWNED else GREY)
        for f in st.flights.values():
            show = f.pos is not None and f.state not in (FlightState.PLANNED, FlightState.LANDED, FlightState.LOST)
            sq = st.squadrons[f.squadron_id]
            alt = f" {f.alt_m / FT / 1000:.0f}k ft" if f.alt_m is not None else ""
            tag = "DCS" if f.spawn is SpawnState.SPAWNED else "engine"
            await self.label(engine, f"f:{f.id}", f.pos if show else None, f"{f.callsign} [{tag}]{alt}",
                             COLOR[sq.side])

    async def label(self, engine: Engine, key: str, pos: Pos | None, text: str, color: list[float]) -> None:
        did = self.draw_ids.setdefault(key, DRAW_BASE + len(self.draw_ids))
        try:
            if pos is None:
                if key in self.drawn:
                    del self.drawn[key]
                    await engine.mission_call("undraw", {"id": did})
                return
            shown = (round(pos.x, -2), round(pos.y, -2), text, tuple(color))   # moves under 100 m don't count
            if self.drawn.get(key) == shown:
                return
            self.drawn[key] = shown
            await engine.mission_call("draw", {"id": did, "audience": "all", "shape": "text",
                                               "pos": {"x": pos.x, "y": pos.y}, "text": text, "font_size": 12,
                                               "color": color, "read_only": True})
        except (BridgeError, asyncio.TimeoutError):
            pass  # mission gone; the next connection redraws


def make() -> Scenario:
    return Khalkhalah()


HOME_XY = (-216085.3, 52697.5)   # Khalkhalah in DCS map coordinates (as in tools/build_test_missions.py)
# Test cartridge points: (label, nm north of HOME, nm east), for the steps in docs/testing-in-dcs.md (Run 15).
TEST_POINTS = [
    ("8 nm N: watch Blue CAS", 8, 0),
    ("mech1", 24, 0),
    ("Red Ferry orbit", 35, 0),
    ("Khalkhalah", 0, 0),
    ("12 nm E: 20 nm from the SA-6", 0, 12),
]


def write_cartridge(dtc_dir: Path) -> Path:
    """Write khalkhalah_test.dtc (FA-18C) with TEST_POINTS as WP1..5 (elevations 0)."""
    from campaign.dtc import DtcWaypoint, hornet_dtc, write_dtc
    wps = [DtcWaypoint(Pos(HOME_XY[0] + n * NM, HOME_XY[1] + e * NM), 0.0, label) for label, n, e in TEST_POINTS]
    return write_dtc(dtc_dir, hornet_dtc("khalkhalah_test", "Syria", wps))


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        sys.exit("usage: python3 -m campaign.scenarios.khalkhalah DTC_DIR   (writes the test cartridge)")
    print(f"wrote {write_cartridge(Path(sys.argv[1]))}")
    for i, (label, _, _) in enumerate(TEST_POINTS, start=1):
        print(f"  WP{i}: {label}")

