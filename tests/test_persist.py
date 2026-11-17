import json
import tempfile
import unittest
from pathlib import Path

from campaign.air import AirConfig, AirSpawner
from campaign.model import (ATO, CampaignState, Event, Flight, FlightState, GroundOrder, Pilot, Pos, Side, SideState,
                            SpawnState, Squadron, StartMode, Waypoint)
from campaign.persist import Autosave, SaveError, Snapshot, decode, dumps, encode, load, loads, save
from campaign.protocol import Hello
from campaign.spawner import GroundSpawner
from tests import test_air
from tests.test_bubble import gu
from tests.test_spawner import CFG, FakeDCS as GroundDCS


def full_state() -> CampaignState:
    s = CampaignState(theater_map="Syria", clock=3600.5,
                      sides={Side.BLUE: SideState(Side.BLUE, {"sam1": 2.0}, supply_pool=10)})
    s.squadrons["vfa"] = Squadron(id="vfa", side=Side.BLUE, name="VFA-1", aircraft_type="FA-18C_hornet",
                                  base_id="khal", roles={"CAP", "CAS"}, aircraft_ready=10, aircraft_damaged=2,
                                  pilots=12, player_flyable=True)
    s.flights["f1"] = Flight(id="f1", package_id="p1", squadron_id="vfa", callsign="Uzi 1", mission="CAP",
                             aircraft=2, loadout="AIM-9X*2, AIM-120C-5*6, FUEL*3", takeoff_time=0,
                             waypoints=[Waypoint(Pos(1, 2), 6000), Waypoint(Pos(3, 4), 6000, action="ORBIT",
                                                                           params={"duration_s": 600})],
                             state=FlightState.ENROUTE, start=StartMode.RAMP_HOT, pos=Pos(5.5, 6.5), alt_m=6000,
                             hdg=90.0, fuel=0.7, next_wp=1, player_seats={1: "ucid", 2: None})
    s.ground_units["a"] = gu("a", 5_000, comp={"T-72B": 3})
    s.ground_units["a"].order = GroundOrder.ATTACK
    s.atos[Side.RED] = ATO(Side.RED, 0.0, ["p2"])
    s.pilots["ucid"] = Pilot(id="ucid", name="Viper", squadron_id="vfa", kills={"MiG-29A": 1})
    s.event_log.append(Event(t=12.0, kind="unit_killed", data={"unit": {"name": "gu:a#1"}}))
    return s


class CodecTests(unittest.TestCase):
    def test_round_trip(self):
        s = full_state()
        back = loads(dumps(Snapshot(s, runs={"mission": ("123", 45)}, extra={"x": {"y": [1]}})))
        self.assertEqual(back.state, s)
        self.assertEqual(back.runs, {"mission": ("123", 45)})
        self.assertEqual(back.extra, {"x": {"y": [1]}})
        self.assertIsInstance(next(iter(back.state.sides)), Side)
        self.assertEqual(back.state.flights["f1"].player_seats, {1: "ucid", 2: None})

    def test_readable_json(self):
        doc = json.loads(dumps(Snapshot(full_state())))
        f = doc["state"]["flights"]["f1"]
        self.assertEqual((f["state"], f["pos"], f["player_seats"]), ("ENROUTE", {"x": 5.5, "y": 6.5}, {"1": "ucid", "2": None}))
        self.assertEqual(doc["state"]["squadrons"]["vfa"]["roles"], ["CAP", "CAS"])

    def test_missing_field_takes_default(self):
        raw = encode(full_state().flights["f1"])
        del raw["next_wp"], raw["fuel"]
        f = decode(Flight, raw)
        self.assertEqual((f.next_wp, f.fuel), (0, 1.0))

    def test_errors(self):
        raw = encode(full_state().flights["f1"])
        with self.assertRaisesRegex(SaveError, "not a FlightState"):
            decode(Flight, dict(raw, state="FLYING"))
        with self.assertRaisesRegex(SaveError, "missing id"):
            decode(Flight, {k: v for k, v in raw.items() if k != "id"})
        with self.assertRaisesRegex(SaveError, "aircraft: expected int"):
            decode(Flight, dict(raw, aircraft="two"))
        with self.assertRaisesRegex(SaveError, "fuel: expected a number"):
            decode(Flight, dict(raw, fuel="full"))
        with self.assertRaisesRegex(SaveError, "unsupported save format"):
            loads('{"format": 99}')
        with self.assertRaisesRegex(SaveError, "can't save"):
            encode(object())


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "saves" / "camp.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_rotation_keeps_previous_saves(self):
        s = full_state()
        for clock in range(4):
            s.clock = clock
            save(self.path, Snapshot(s), keep=2)
        self.assertEqual(load(self.path).state.clock, 3)
        self.assertEqual(load(self.path.with_name("camp.json.1")).state.clock, 2)
        self.assertEqual(load(self.path.with_name("camp.json.2")).state.clock, 1)
        self.assertFalse(self.path.with_name("camp.json.3").exists())
        self.assertFalse(self.path.with_name("camp.json.tmp").exists())

    def test_bad_state_leaves_the_old_save(self):
        s = full_state()
        save(self.path, Snapshot(s))
        s.flights["f1"].waypoints[0].params = {"bad": object()}
        with self.assertRaises(SaveError):
            save(self.path, Snapshot(s))
        self.assertEqual(load(self.path).state.clock, 3600.5)

    def test_autosave_only_on_change(self):
        s = full_state()
        auto = Autosave(self.path, lambda: Snapshot(s))
        self.assertTrue(auto.maybe_save())
        self.assertFalse(auto.maybe_save())
        s.clock += 1
        self.assertTrue(auto.maybe_save())
        self.assertTrue(self.path.with_name("camp.json.1").exists())


class EngineRestartTests(unittest.IsolatedAsyncioTestCase):
    """The engine stops and starts again while the same DCS mission keeps running."""

    def hello(self, spawned):
        return Hello(run_id="run1", map="Syria", spawned=list(spawned))

    async def test_ground_resumes(self):
        now = [0.0]
        dcs = GroundDCS()
        state = CampaignState(theater_map="Syria", clock=0, sides={})
        state.ground_units["a"] = gu("a", 5_000, comp={"T-72B": 2, "BMP-2": 2})
        sp = GroundSpawner(state, dcs.call, CFG, clock=lambda: now[0])
        sp.on_telemetry(dcs.telemetry([(0, 0)]))
        await sp.tick()
        kill = dcs.kill("gu:a", 1)
        sp.on_event(kill)
        text = dumps(Snapshot(state, extra={"ground": sp.export()}))

        # New process: clock restarts.
        now[0] = 50.0
        snap = loads(text)
        sp2 = GroundSpawner(snap.state, dcs.call, CFG, clock=lambda: now[0])
        sp2.restore(snap.extra["ground"])
        sp2.on_hello(self.hello(dcs.groups), new_run=False)
        a = snap.state.ground_units["a"]
        self.assertIs(a.spawn, SpawnState.SPAWNED)  # adopted, not respawned
        sp2.on_event(kill)  # a replay of an event the save already has: not counted again
        self.assertEqual(sum(a.composition.values()), 3)
        self.assertIn("a", sp2.engaged_until)  # combat pin survives
        sp2.on_event(dcs.kill("gu:a", 2))  # new kill, type from the restored unit list
        self.assertEqual(sum(a.composition.values()), 2)

    async def test_mission_restarted_too(self):
        dcs = GroundDCS()
        state = CampaignState(theater_map="Syria", clock=0, sides={})
        state.ground_units["a"] = gu("a", 5_000)
        sp = GroundSpawner(state, dcs.call, CFG, clock=lambda: 0.0)
        sp.on_telemetry(dcs.telemetry([(0, 0)]))
        await sp.tick()
        snap = loads(dumps(Snapshot(state, extra={"ground": sp.export()})))
        sp2 = GroundSpawner(snap.state, dcs.call, CFG, clock=lambda: 0.0)
        sp2.restore(snap.extra["ground"])
        sp2.on_hello(Hello(run_id="run2", map="Syria", spawned=[]), new_run=True)
        self.assertIs(snap.state.ground_units["a"].spawn, SpawnState.AGGREGATED)

    async def test_flight_resumes(self):
        now = [0.0]
        dcs = test_air.FakeDCS()
        state = CampaignState(theater_map="Syria", clock=0, sides={})
        state.squadrons["sq"] = Squadron(id="sq", side=Side.RED, name="1st", aircraft_type="MiG-29A", base_id="b",
                                         roles={"CAP"}, aircraft_ready=6, aircraft_damaged=0, pilots=6,
                                         player_flyable=False)
        f = test_air.flight(start=StartMode.RAMP_HOT)
        state.flights[f.id] = f
        air = AirSpawner(state, dcs.call, AirConfig(), clock=lambda: now[0])
        await air.spawn(f, takeoff=test_air.TAKEOFF)
        air.on_event({"kind": "takeoff", "unit": {"name": "fl:f1#1"}})
        air.on_event(dcs.kill("fl:f1", "fl:f1#2"))
        text = dumps(Snapshot(state, extra={"air": air.export()}))

        now[0] = 7.0
        snap = loads(text)
        air2 = AirSpawner(snap.state, dcs.call, AirConfig(), clock=lambda: now[0])
        air2.restore(snap.extra["air"])
        air2.on_hello(self.hello(dcs.groups), new_run=False)
        f2 = snap.state.flights["f1"]
        self.assertEqual((f2.spawn, f2.state, f2.aircraft), (SpawnState.SPAWNED, FlightState.ENROUTE, 1))
        air2.on_event({"kind": "unit_killed", "unit": {"name": "fl:f1#2"}})  # replayed: ignored
        self.assertEqual(f2.aircraft, 1)
        air2.on_event({"kind": "land", "unit": {"name": "fl:f1#1"}})  # the dead wingman doesn't block landing
        air2.on_event({"kind": "engine_shutdown", "unit": {"name": "fl:f1#1"}})
        await air2.tick()
        self.assertEqual((f2.spawn, snap.state.squadrons["sq"].aircraft_ready), (SpawnState.AGGREGATED, 5))


if __name__ == "__main__":
    unittest.main()
