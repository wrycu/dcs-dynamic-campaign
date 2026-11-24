import asyncio
import tempfile
import unittest
from pathlib import Path

from campaign.bubble import NM
from campaign.model import FlightState, Pos, SpawnState
from campaign.persist import dumps, loads
from campaign.protocol import Hello
from campaign.scenarios.khalkhalah import Khalkhalah
from campaign.server import Engine, Scenario, System, WarClock, load_scenario
from campaign.settings import SettingsError, load_settings, server_config
from tests.test_spawner import FakeDCS

ROOT = Path(__file__).resolve().parent.parent
HOME = {"x": -216085.3, "y": 52697.5}
FAR = {"x": HOME["x"] - 100 * NM, "y": HOME["y"]}   # exactly 100 nm south: the far base


class FakeMission(FakeDCS):
    """The mission bridge as the server uses it: spawning, plus queries, drawings and messages."""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    async def call(self, op, data):
        if op in ("spawn_group", "despawn_group"):
            return await super().call(op, data)
        self.calls.append((op, data))
        if op == "query" and data["what"] == "airbases":
            return {"airbases": [{"name": "Khalkhalah", "id": 18, "pos": HOME, "alt": 700},
                                 {"name": "Mafraq", "id": 30, "pos": FAR, "alt": 600}]}
        if op == "query" and data["what"] == "parking":
            return {"spots": [{"term": i, "type": 68, "pos": HOME, "dist_rw": i} for i in range(1, 5)]}
        if op == "message":
            self.messages.append(data["text"])
        return None


def settings():
    return load_settings(ROOT / "config" / "campaign.toml")


def player_at(pos, t):
    return {"players": [{"pos": dict(pos)}], "groups": []}


class Recorder(System):
    name = "recorder"

    def __init__(self, log):
        self.log = log

    def advance(self, t0, t1):
        self.log.append(("advance", t0, t1))

    async def tick(self):
        self.log.append(("tick",))


class Broken(System):
    name = "broken"

    async def tick(self):
        raise RuntimeError("boom")


class TestScenario(Khalkhalah):
    def __init__(self, extra=()):
        super().__init__()
        self.extra = list(extra)
        self.setups = 0

    def systems(self, engine):
        return self.extra

    async def setup(self, engine):
        self.setups += 1
        await super().setup(engine)


class SettingsTests(unittest.TestCase):
    def test_shipped_config(self):
        s = settings()
        self.assertEqual((s.server.port, s.server.slot_sides), (7341, [0, 2]))
        self.assertEqual(s.bubble.ground_spawn, 20 * NM)

    def test_defaults_without_a_file(self):
        self.assertEqual(load_settings(None).server.save, "build/saves/campaign.json")

    def test_errors(self):
        for bad in ({"prot": 1}, {"port": "7341"}, {"port": 0}, {"slot_sides": [3]}, {"autosave_s": True}):
            with self.assertRaises(SettingsError, msg=bad):
                server_config(bad)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text("[sever]\nport = 1\n")
            with self.assertRaises(SettingsError):
                load_settings(p)


class ClockTests(unittest.TestCase):
    def test_steps(self):
        c = WarClock()
        self.assertEqual(c.step(100), 0)    # first report: nothing to measure from
        self.assertEqual(c.step(103), 3)
        self.assertEqual(c.step(1000), 120)  # a stall or pause is capped
        self.assertEqual(c.step(5), 0)      # a new mission run restarts model time
        c.reset()
        self.assertEqual(c.step(50), 0)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def make(self, scenario=None, snap=None):
        self.dcs = FakeMission()
        self.hooks = []

        async def hooks_call(op, data):
            self.hooks.append((op, data))

        return Engine(settings(), scenario or TestScenario(), snap, mission_call=self.dcs.call, hooks_call=hooks_call)

    async def connect(self, e, new_run=True, spawned=()):
        await e.mission_connected(Hello(run_id="r1", map="Syria", spawned=list(spawned)), new_run)

    async def report(self, e, t, players=(HOME,)):
        data = self.dcs.telemetry([(p["x"], p["y"]) for p in players])
        e.on_telemetry(t, data)
        await e.tick()

    async def test_first_connection_sets_up_the_scenario_once(self):
        e = self.make()
        await self.connect(e)
        self.assertTrue(e.ready)
        self.assertEqual(sorted(e.state.ground_units), ["armor1", "blue1", "mech1", "near1", "sa6"])
        self.assertEqual(sorted(e.state.flights), ["blucas", "bluferry", "redferry"])
        self.assertEqual(e.state.squadrons["blue_far"].base_id, "Mafraq")
        await self.connect(e, new_run=True)
        self.assertEqual(e.scenario.setups, 1)
        ops = [op for op, _ in self.dcs.calls]
        self.assertIn("warehouse_set", ops)
        self.assertIn("menu", ops)

    async def test_clock_follows_model_time(self):
        e = self.make()
        await self.connect(e)
        await self.report(e, 1000, players=({"x": 0, "y": 0},))
        self.assertEqual(e.state.clock, 0)
        await self.report(e, 1003, players=({"x": 0, "y": 0},))
        await self.report(e, 1063, players=({"x": 0, "y": 0},))
        self.assertEqual(e.state.clock, 63)

    async def test_ground_bubble_spawns_around_the_player(self):
        e = self.make()
        await self.connect(e)
        await self.report(e, 0)
        spawned = sorted(u.id for u in e.state.ground_units.values() if u.spawn is SpawnState.SPAWNED)
        self.assertEqual(spawned, ["armor1", "blue1", "near1"])  # within 20 nm; the SA-6 is 32 nm out

    async def test_flights_fly_without_dcs_when_nobody_is_near(self):
        e = self.make()
        await self.connect(e)
        far_away = ({"x": HOME["x"], "y": HOME["y"] + 300 * NM},)
        for t in range(0, 400, 3):
            await self.report(e, t, players=far_away)
        cas = e.state.flights["blucas"]
        self.assertEqual((cas.state, cas.spawn), (FlightState.ENROUTE, SpawnState.AGGREGATED))
        self.assertEqual(e.state.squadrons["blue_home"].aircraft_ready, 6)
        self.assertNotIn("spawn_group", [op for op, _ in self.dcs.calls])

    async def test_tick_order_and_a_failing_system(self):
        log = []
        e = self.make(TestScenario([Broken(), Recorder(log)]))
        await self.connect(e)
        with self.assertLogs("campaign", "ERROR") as logs:
            await self.report(e, 0)
            await self.report(e, 3)
        self.assertIn("broken failed this tick", logs.output[0])
        self.assertEqual(log, [("tick",), ("advance", 0.0, 3.0), ("tick",)])
        self.assertTrue(any(u.spawn is SpawnState.SPAWNED for u in e.state.ground_units.values()))

    async def test_save_and_resume(self):
        e = self.make()
        await self.connect(e)
        await self.report(e, 0)
        await self.report(e, 30)
        text = dumps(e.snapshot())
        snap = loads(text)
        self.assertEqual((snap.extra["scenario"], snap.extra["scenario_ready"]), ("khalkhalah", True))
        scenario = TestScenario()
        e2 = self.make(scenario, snap)
        self.assertTrue(e2.ready)
        self.assertEqual(e2.state.clock, 30)
        # The same mission run still has the groups: they are kept, not respawned.
        groups = [u.dcs_group for u in e2.state.ground_units.values() if u.spawn is SpawnState.SPAWNED]
        self.dcs.groups = {g: [{"name": f"{g}#1", "type": "T-72B", "x": 0, "y": 0}] for g in groups}
        await self.connect(e2, new_run=False, spawned=groups)
        self.assertEqual(scenario.setups, 0)
        self.assertEqual(sorted(g for g in groups), sorted(u.dcs_group for u in e2.state.ground_units.values()
                                                           if u.spawn is SpawnState.SPAWNED))

    def cas_in_dcs(self, e, north_nm):
        """Put Blue CAS in DCS, north_nm north of the field (it attacks armor1, 13 nm north)."""
        cas = e.state.flights["blucas"]
        cas.spawn, cas.state, cas.dcs_group = SpawnState.SPAWNED, FlightState.ENROUTE, "fl:blucas"
        self.move_cas(north_nm)
        return cas

    def move_cas(self, north_nm):
        self.dcs.groups["fl:blucas"] = [{"name": "fl:blucas#1", "type": "FA-18C_hornet",
                                         "x": HOME["x"] + north_nm * NM, "y": HOME["y"]}]

    # The player is 40 nm south: too far for armor1 (53 nm) and blue1 (38 nm) to be in DCS, close
    # enough for the CAS flight (45 nm) to stay in DCS.
    SOUTH = ({"x": HOME["x"] - 40 * NM, "y": HOME["y"]},)

    async def test_an_ai_flights_target_stays_in_dcs(self):
        # Run 15: a target left DCS under an inbound AI flight, which found nothing and went home.
        e = self.make()
        await self.connect(e)
        await self.report(e, 0)                          # the player at the field: armor1 spawns
        armor = e.state.ground_units["armor1"]
        self.assertIs(armor.spawn, SpawnState.SPAWNED)
        cas = self.cas_in_dcs(e, 5)
        for t in range(3, 400, 3):                       # the player leaves; the CAS is inbound
            await self.report(e, t, players=self.SOUTH)
        self.assertIs(cas.spawn, SpawnState.SPAWNED)
        self.assertIs(armor.spawn, SpawnState.SPAWNED)
        self.assertIs(e.state.ground_units["blue1"].spawn, SpawnState.AGGREGATED)   # only the target stays
        cas.next_wp = 3                                  # attack over, heading home
        for t in range(400, 600, 3):
            await self.report(e, t, players=self.SOUTH)
        self.assertIs(armor.spawn, SpawnState.AGGREGATED)

    async def test_an_ai_flights_target_spawns_as_it_arrives(self):
        e = self.make()
        await self.connect(e)
        self.cas_in_dcs(e, -20)                          # 33 nm from armor1
        await self.report(e, 0, players=self.SOUTH)
        self.assertIs(e.state.ground_units["armor1"].spawn, SpawnState.AGGREGATED)
        self.move_cas(-5)                                # 18 nm
        await self.report(e, 3, players=self.SOUTH)
        self.assertIs(e.state.ground_units["armor1"].spawn, SpawnState.SPAWNED)

    async def test_ghosts_count_for_the_bubbles_only(self):
        self.dcs = FakeMission()
        e = Engine(settings(), TestScenario(), mission_call=self.dcs.call, ghosts=["000/13"])   # on armor1
        await self.connect(e)
        await self.report(e, 0, players=self.SOUTH)
        self.assertIs(e.state.ground_units["armor1"].spawn, SpawnState.SPAWNED)
        self.assertIs(e.state.ground_units["sa6"].spawn, SpawnState.AGGREGATED)   # 34 nm from the ghost
        self.assertEqual(len(e.players), 1)              # Status still sees only the real player
        self.assertIn(("draw", "GHOST 1 (000/13)"), [(op, d.get("text")) for op, d in self.dcs.calls])

    async def test_hooks_get_slot_rules(self):
        e = self.make()

        class Conn:
            src = "hooks"

        await e.on_hello(Conn(), Hello(run_id="h1"), True)
        self.assertEqual(self.hooks, [("slot_rules", {"allowed_sides": [0, 2]})])

    async def test_status(self):
        e = self.make()
        await self.connect(e)
        text = e.status_text()
        self.assertIn("CAMPAIGN khalkhalah", text)
        self.assertIn("Blue CAS: 2x CAS planned at T+5 min", text)
        await self.report(e, 0)
        await asyncio.gather(*e.tasks)
        text = e.status_text()
        self.assertIn("armor1: 8 veh [DCS] 355M 13 nm", text)   # true north, 5° E variation
        self.assertIn("Red Ferry: 2x FERRY enroute [engine] 355M 60 nm 20,000 ft", text)
        self.assertIn("armor1: spawned, 8 vehicles", self.dcs.messages)       # the test scenario announces
        self.assertIn("Red Ferry: took off (not in DCS)", self.dcs.messages)
        e.on_event({"kind": "menu_select", "key": "status"})
        await asyncio.gather(*e.tasks)
        self.assertTrue(any(m.startswith("CAMPAIGN") for m in self.dcs.messages))

    async def test_save_file_written(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.json"
            e = Engine(settings(), TestScenario(), mission_call=FakeMission().call, save_path=path)
            await self.connect(e)
            self.assertTrue(path.exists())
            self.assertIn('"scenario": "khalkhalah"', path.read_text())

    def test_load_scenario(self):
        self.assertIsInstance(load_scenario("khalkhalah"), Scenario)
        with self.assertRaises(SystemExit):
            load_scenario("nope")


if __name__ == "__main__":
    unittest.main()
