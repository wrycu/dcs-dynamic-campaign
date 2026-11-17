import unittest

from campaign.air import AirConfig, AirSpawner
from campaign.air_bubble import AirBubble, fly
from campaign.bubble import NM, BubbleConfig
from campaign.model import CampaignState, Flight, FlightState, Pos, Side, SpawnState, Squadron, StartMode, Waypoint
from tests.test_air import TAKEOFF, FakeDCS

CFG = BubbleConfig(air_spawn=30 * NM, air_despawn=40 * NM, air_min_dwell=60, max_flights=2, lookahead_s=0)
SPEED = 200.0


def route_flight(fid="f1", start=StartMode.AIR, takeoff_time=0.0, wps=None):
    wps = wps or [Waypoint(Pos(0, 0), 6000), Waypoint(Pos(100_000, 0), 6000, action="ORBIT", params={"duration_s": 600}),
                  Waypoint(Pos(100_000, 100_000), 7000, action="EXIT")]
    return Flight(id=fid, package_id="p", squadron_id="sq", callsign=fid, mission="CAP", aircraft=2,
                  loadout="R-73*4,R-27R*2,Fuel-1500", waypoints=wps, takeoff_time=takeoff_time, start=start)


class FlyTests(unittest.TestCase):
    def test_leg_orbit_exit(self):
        f = route_flight()
        f.pos = Pos(0, 0)
        f.next_wp = 1
        self.assertIsNone(fly(f, 0, 250, SPEED))
        self.assertEqual((f.pos, f.hdg), (Pos(50_000, 0), 0.0))
        self.assertIsNone(fly(f, 250, 600, SPEED))  # arrives at 500 s, orbits until 1100 s
        self.assertEqual((f.pos, f.next_wp, f.hold_until), (Pos(100_000, 0), 1, 1100))
        self.assertIsNone(fly(f, 600, 1100, SPEED))
        self.assertIsNone(fly(f, 1100, 1350, SPEED))  # orbit over: 50 km east
        self.assertEqual((f.pos, f.next_wp, f.hold_until, f.hdg), (Pos(100_000, 50_000), 2, None, 90.0))
        self.assertEqual(f.alt_m, 6500)
        self.assertEqual(fly(f, 1350, 2000, SPEED), "EXIT")
        self.assertEqual(f.next_wp, 3)

    def test_several_waypoints_in_one_step(self):
        f = route_flight(wps=[Waypoint(Pos(1000, 0), 1000), Waypoint(Pos(2000, 0), 1000),
                              Waypoint(Pos(3000, 0), 1000, action="LAND", params={"airdrome_id": 1})])
        f.pos = Pos(0, 0)
        self.assertEqual(fly(f, 0, 100, SPEED), "LAND")


class AirBubbleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dcs = FakeDCS()
        self.state = CampaignState(theater_map="Syria", clock=0, sides={})
        self.state.squadrons["sq"] = Squadron(id="sq", side=Side.RED, name="1st", aircraft_type="MiG-29A",
                                              base_id="b", roles={"CAP"}, aircraft_ready=8, aircraft_damaged=0,
                                              pilots=8, player_flyable=False)
        self.sq = self.state.squadrons["sq"]
        self.base = Pos(0, 0)
        self.air = AirSpawner(self.state, self.dcs.call, AirConfig(), clock=lambda: self.state.clock)
        self.takeoffs = []

        async def takeoff_for(f):
            self.takeoffs.append(f.id)
            return TAKEOFF

        self.bubble = AirBubble(self.state, self.air, CFG, base_pos=lambda sq: self.base, takeoff_for=takeoff_for)

    def add(self, f):
        self.state.flights[f.id] = f
        return f

    def players(self, *pos, t=None):
        self.bubble.on_telemetry(t, {"players": [{"pos": {"x": x, "y": y}} for x, y in pos], "groups": []})

    async def test_due_flight_takes_off_without_dcs_when_nobody_is_near(self):
        f = self.add(route_flight(start=StartMode.RAMP_HOT, takeoff_time=100))
        self.players((0, 200 * NM), t=0)
        await self.bubble.tick()
        self.assertIs(f.state, FlightState.PLANNED)  # not due yet
        self.players((0, 200 * NM), t=100)
        plan = await self.bubble.tick()
        self.assertEqual(plan.launch_virtual, ["f1"])
        self.assertEqual((f.state, f.spawn, f.pos, self.sq.aircraft_ready), (FlightState.ENROUTE, SpawnState.AGGREGATED,
                                                                              Pos(0, 0), 6))
        self.assertEqual(self.dcs.calls, [])

    async def test_due_flight_starts_on_the_ramp_when_a_player_is_at_its_base(self):
        f = self.add(route_flight(start=StartMode.RAMP_HOT))
        self.players((1000, 0), t=0)
        plan = await self.bubble.tick()
        self.assertEqual((plan.launch_dcs, self.takeoffs), (["f1"], ["f1"]))
        self.assertEqual((f.spawn, f.state), (SpawnState.SPAWNED, FlightState.TAXI))

    async def test_spawns_when_a_player_comes_close_and_leaves_when_they_go(self):
        f = self.add(route_flight())
        self.players((0, 200 * NM), t=0)
        await self.bubble.tick()  # air start, virtual: at its first waypoint
        self.assertEqual((f.spawn, f.pos), (SpawnState.AGGREGATED, Pos(0, 0)))
        self.players((0, 200 * NM), t=100)  # flies 22 km north
        self.assertEqual(f.pos, Pos(22_000, 0))
        self.players((22_000, 25 * NM), t=103)
        plan = await self.bubble.tick()
        self.assertEqual(plan.spawn, ["f1"])
        unit = self.dcs.calls[-1][1]["group"]["units"][0]
        self.assertAlmostEqual(unit["x"], 22_660)  # spawned where the engine had it
        self.assertEqual(self.state.squadrons["sq"].aircraft_ready, 6)  # taken once, at launch

        self.air.on_telemetry(self.dcs.telemetry())  # the fake reports it at 0,0; good enough
        self.players((0, 50 * NM), t=130)  # player beyond despawn, but dwell not done
        self.assertEqual((await self.bubble.tick()).aggregate, [])
        self.bubble.on_event({"kind": "hit", "target": {"name": "fl:f1#1"}})
        self.players((0, 50 * NM), t=200)
        self.assertEqual((await self.bubble.tick()).aggregate, [])  # pinned by the fight
        for t in (300, 400, 500):  # pinned until clock 430
            self.players((0, 50 * NM), t=t)
        self.assertEqual((await self.bubble.tick()).aggregate, ["f1"])
        self.assertIs(f.spawn, SpawnState.AGGREGATED)

    async def test_flight_cap(self):
        for i in range(3):
            self.add(route_flight(fid=f"f{i}", wps=[Waypoint(Pos(i * 1000, 0), 6000), Waypoint(Pos(500_000, 0), 6000)]))
        self.players((0, 0), t=0)
        await self.bubble.tick()  # launch all three (air starts)
        plan = await self.bubble.tick()
        self.assertEqual(plan.spawn, ["f0", "f1"])  # the two closest

    async def test_virtual_flight_comes_home(self):
        f = self.add(route_flight(wps=[Waypoint(Pos(0, 0), 6000),
                                       Waypoint(Pos(50_000, 0), 6000, action="LAND", params={"airdrome_id": 1})]))
        self.players((0, 200 * NM), t=0)
        await self.bubble.tick()
        self.assertEqual(self.sq.aircraft_ready, 6)
        for t in range(0, 400, 100):
            self.players((0, 200 * NM), t=t)
        self.assertEqual((f.state, self.sq.aircraft_ready), (FlightState.LANDED, 8))
        self.assertEqual((await self.bubble.tick()).spawn, [])

    def test_clock_follows_model_time(self):
        self.players(t=50)
        self.players(t=80)
        self.assertEqual(self.state.clock, 30)
        self.players(t=10)  # mission restarted: model time went back; no jump
        self.players(t=20)
        self.assertEqual(self.state.clock, 40)
        self.players(t=2000)  # long stall: capped
        self.assertEqual(self.state.clock, 160)

    def test_save_round_trip(self):
        self.bubble.spawned_at["f1"] = 5.0
        self.bubble.pinned_until["f1"] = 50.0
        other = AirBubble(self.state, self.air, CFG, base_pos=lambda sq: None, takeoff_for=None)
        other.restore(self.bubble.export())
        self.assertEqual((other.spawned_at, other.pinned_until), ({"f1": 5.0}, {"f1": 50.0}))


if __name__ == "__main__":
    unittest.main()
