import math
import unittest

from campaign.air import AirConfig, AirSpawner
from campaign.air_bubble import fly
from campaign.flight_spawn import Takeoff, build_flight_spawn
from campaign.model import (CampaignState, Flight, FlightState, Pos, Side, SpawnState, Squadron, StartMode,
                            Waypoint)
from campaign.payloads import UnknownPayload, dcs_payload
from campaign.protocol import Hello


def flight(fid="f1", start=StartMode.AIR, n=2, mission="CAP", loadout="R-73*4,R-27R*2,Fuel-1500", wps=None):
    wps = wps or [Waypoint(Pos(0, 0), 6_000), Waypoint(Pos(40_000, 0), 6_000, action="ORBIT",
                                                         params={"duration_s": 600}),
                  Waypoint(Pos(100_000, 0), 6_000, action="EXIT")]
    return Flight(id=fid, package_id="p", squadron_id="sq", callsign="Ivan 1", mission=mission, aircraft=n,
                  loadout=loadout, waypoints=wps, takeoff_time=0, start=start)


TAKEOFF = Takeoff(airdrome_id=18, pos=Pos(0, 0), elev_m=700,
                  parking=[(11, Pos(10, 10)), (12, Pos(20, 10)), (13, Pos(30, 10))])


class BuildTests(unittest.TestCase):
    def test_air_start(self):
        spec = build_flight_spawn(flight(), "MiG-29A", Side.RED)
        g = spec.data["group"]
        self.assertEqual(spec.data["category"], "airplane")
        self.assertEqual(g["name"], "fl:f1")
        self.assertEqual(spec.unit_names, ["fl:f1#1", "fl:f1#2"])
        self.assertEqual(g["task"], "CAP")
        u1, u2 = g["units"]
        self.assertEqual((u1["x"], u1["y"], u1["alt"]), (0, 0, 6_000))
        self.assertAlmostEqual(u1["heading"], 0.0)  # toward the orbit point, due north
        self.assertLess(u2["x"], u1["x"])           # wingman behind
        self.assertEqual(u1["payload"]["pylons"]["4"]["CLSID"], "{2BEC576B-CDF5-4B7F-961F-B0FA4312B841}")
        p1, p2, p3 = g["route"]["points"]
        ids = [t["id"] for t in p1["task"]["params"]["tasks"]]
        self.assertEqual(ids, ["WrappedAction", "WrappedAction", "EngageTargets"])
        orbit = p2["task"]["params"]["tasks"][0]
        self.assertEqual(orbit["id"], "ControlledTask")
        self.assertEqual(orbit["params"]["stopCondition"]["duration"], 600)
        self.assertNotIn("task", p3)

    def test_resume_in_the_air(self):
        f = flight()
        f.pos, f.alt_m, f.hdg, f.speed_ms, f.fuel, f.next_wp = Pos(20_000, 5_000), 5_500, 90.0, 200.0, 0.4, 1
        g = build_flight_spawn(f, "MiG-29A", Side.RED).data["group"]
        u1 = g["units"][0]
        self.assertEqual((u1["x"], u1["y"], u1["alt"], u1["speed"]), (20_000, 5_000, 5_500, 200.0))
        self.assertAlmostEqual(u1["heading"], math.pi / 2)
        self.assertEqual(u1["payload"]["fuel_fraction"], 0.4)
        pts = g["route"]["points"]
        self.assertEqual(len(pts), 3)  # where it is, then the orbit and the exit
        self.assertEqual((pts[0]["x"], pts[1]["x"]), (20_000, 40_000))

    def test_ramp_start_and_land(self):
        wps = [Waypoint(Pos(30_000, 0), 4_500, action="CAS"), Waypoint(Pos(0, 0), 700, action="LAND",
                                                                       params={"airdrome_id": 18})]
        f = flight(start=StartMode.RAMP_COLD, mission="CAS", loadout="", wps=wps)
        g = build_flight_spawn(f, "FA-18C_hornet", Side.BLUE, takeoff=TAKEOFF, uncontrolled=True).data["group"]
        self.assertTrue(g["uncontrolled"])
        first, cas, land = g["route"]["points"]
        self.assertEqual((first["type"], first["action"], first["airdromeId"]),
                         ("TakeOffParking", "From Parking Area", 18))
        self.assertEqual([u["parking"] for u in g["units"]], [11, 12])
        self.assertEqual((g["units"][1]["x"], g["units"][1]["y"]), (20, 10))
        self.assertEqual(cas["task"]["params"]["tasks"][0]["id"], "EngageTargetsInZone")
        self.assertEqual((land["type"], land["airdromeId"]), ("Land", 18))
        self.assertEqual(g["units"][0]["payload"]["pylons"], {})

    def test_ferry_holds_fire(self):
        g = build_flight_spawn(flight(mission="FERRY", loadout=""), "MiG-29A", Side.RED).data["group"]
        first = g["route"]["points"][0]["task"]["params"]["tasks"]
        roe = [t["params"]["action"]["params"]["value"] for t in first
               if t["id"] == "WrappedAction" and t["params"]["action"]["params"]["name"] == 0]
        self.assertEqual((roe, g["units"][0]["payload"]["pylons"]), ([4], {}))

    def test_roe_by_mission(self):
        def roe(mission):
            g = build_flight_spawn(flight(mission=mission, loadout=""), "MiG-29A", Side.RED).data["group"]
            return [t["params"]["action"]["params"]["value"] for t in g["route"]["points"][0]["task"]["params"]["tasks"]
                    if t["id"] == "WrappedAction" and t["params"]["action"]["params"]["name"] == 0]
        # Fighters weapons free; CAS etc. only what their tasks name (Run 15: CAS went after an SA-6).
        self.assertEqual((roe("BARCAP"), roe("CAS"), roe("SEAD"), roe("FERRY")), ([0], [2], [2], [4]))

    def test_errors(self):
        with self.assertRaises(ValueError):
            build_flight_spawn(flight(start=StartMode.RAMP_HOT), "MiG-29A", Side.RED)  # no airbase
        with self.assertRaises(ValueError):
            build_flight_spawn(flight(start=StartMode.RAMP_HOT, n=4), "MiG-29A", Side.RED, takeoff=TAKEOFF)
        with self.assertRaises(ValueError):
            build_flight_spawn(flight(), "MiG-29A", Side.RED, uncontrolled=True)
        with self.assertRaises(UnknownPayload):
            dcs_payload("MiG-29A", "no such loadout")


class IdentityTests(unittest.TestCase):
    def test_blue_callsigns_and_board_numbers(self):
        f = flight(loadout="AIM-9X*2, AIM-120C-5*6, FUEL*3")
        g = build_flight_spawn(f, "FA-18C_hornet", Side.BLUE, number=13).data["group"]
        self.assertEqual([u["callsign"] for u in g["units"]],
                         [{1: 2, 2: 4, 3: 1, "name": "Springfield41"}, {1: 2, 2: 4, 3: 2, "name": "Springfield42"}])
        self.assertEqual([u["onboard_num"] for u in g["units"]], ["131", "132"])

    def test_red_numeric_callsigns(self):
        g = build_flight_spawn(flight(), "MiG-29A", Side.RED, number=1).data["group"]
        self.assertEqual([(u["callsign"], u["onboard_num"]) for u in g["units"]], [(111, "011"), (112, "012")])


class FakeDCS:
    def __init__(self):
        self.groups: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []

    async def call(self, op, data):
        self.calls.append((op, data))
        if op == "spawn_group":
            g = data["group"]
            self.groups[g["name"]] = {"units": [u["name"] for u in g["units"]], "pos": g["units"][0]}
            return {"group": g["name"], "units": self.groups[g["name"]]["units"]}
        if op == "despawn_group":
            g = self.groups.pop(data["group"], None)
            if g is None:
                return {"survivors": {}, "gone": True}
            return {"survivors": {"MiG-29A": len(g["units"])}, "alive": g["units"],
                    "pos": {"x": 1_000.0, "y": 2_000.0}, "alt": 5_000.0, "hdg": 45.0, "spd": 210.0, "fuel": 0.55}
        if op == "set_command":
            return None
        raise AssertionError(op)

    def kill(self, group, name):
        self.groups[group]["units"].remove(name)
        if not self.groups[group]["units"]:
            del self.groups[group]
        return {"kind": "unit_killed", "unit": {"name": name, "type": "MiG-29A"}}

    def telemetry(self, **extra):
        return {"players": [], "groups": [dict({"id": n, "pos": {"x": 0, "y": 0}, "alive": len(g["units"])}, **extra)
                                          for n, g in self.groups.items()]}


class AirSpawnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0.0
        self.dcs = FakeDCS()
        self.state = CampaignState(theater_map="Syria", clock=0, sides={})
        self.state.squadrons["sq"] = Squadron(id="sq", side=Side.RED, name="1st", aircraft_type="MiG-29A",
                                              base_id="b", roles={"CAP"}, aircraft_ready=6, aircraft_damaged=0,
                                              pilots=6, player_flyable=False)
        self.notes = []
        self.air = AirSpawner(self.state, self.dcs.call, AirConfig(taxi_timeout_s=600, shutdown_timeout_s=300),
                              clock=lambda: self.now, notify=lambda f, w: self.notes.append(w))
        self.sq = self.state.squadrons["sq"]

    def add(self, f):
        self.state.flights[f.id] = f
        return f

    async def test_ramp_flight_full_cycle(self):
        f = self.add(flight(start=StartMode.RAMP_HOT))
        await self.air.spawn(f, takeoff=TAKEOFF)
        self.assertEqual((f.state, f.spawn, self.sq.aircraft_ready), (FlightState.TAXI, SpawnState.SPAWNED, 4))
        self.air.on_event({"kind": "takeoff", "unit": {"name": "fl:f1#1"}, "place": "Khalkhalah"})
        self.air.on_event({"kind": "takeoff", "unit": {"name": "fl:f1#2"}})
        self.assertIs(f.state, FlightState.ENROUTE)
        self.air.on_event(self.dcs.kill("fl:f1", "fl:f1#2"))
        self.air.on_event({"kind": "unit_killed", "unit": {"name": "fl:f1#2"}})  # duplicate
        self.assertEqual(f.aircraft, 1)
        self.air.on_event({"kind": "land", "unit": {"name": "fl:f1#1"}, "place": "Khalkhalah"})
        self.assertIs(f.state, FlightState.LANDED)
        self.assertEqual(self.air.due(), [])  # still taxiing in
        self.air.on_event({"kind": "engine_shutdown", "unit": {"name": "fl:f1#1"}})
        await self.air.tick()
        self.assertEqual((f.spawn, f.state, self.sq.aircraft_ready), (SpawnState.AGGREGATED, FlightState.LANDED, 5))
        self.assertNotIn("fl:f1", self.dcs.groups)

    async def test_landed_without_shutdown_is_recovered_after_timeout(self):
        f = self.add(flight())
        await self.air.spawn(f)
        for n in ("fl:f1#1", "fl:f1#2"):
            self.air.on_event({"kind": "land", "unit": {"name": n}})
        self.now = 299
        self.assertEqual(self.air.due(), [])
        self.now = 300
        await self.air.tick()
        self.assertEqual((f.spawn, self.sq.aircraft_ready), (SpawnState.AGGREGATED, 6))

    async def test_stuck_on_ramp_is_aborted(self):
        f = self.add(flight(start=StartMode.RAMP_COLD))
        await self.air.spawn(f, takeoff=TAKEOFF, uncontrolled=True)
        self.now = 10_000
        self.assertEqual(self.air.due(), [])  # held uncontrolled: no watchdog yet
        await self.air.start(f)
        self.assertEqual(self.dcs.calls[-1], ("set_command", {"group": "fl:f1", "command": {"id": "Start", "params": {}}}))
        self.now += 600
        await self.air.tick()
        self.assertEqual((f.spawn, self.sq.aircraft_ready), (SpawnState.AGGREGATED, 6))
        self.assertIn("aborted, never took off: 2 aircraft back to the squadron", self.notes)

    async def test_removed_by_dcs_right_after_spawn(self):
        # DCS 2.9: a ramp start at a warehouse without the type makes an empty group that never shows up.
        f = self.add(flight(start=StartMode.RAMP_HOT))
        await self.air.spawn(f, takeoff=TAKEOFF)
        self.dcs.groups.clear()
        self.now = 6
        self.air.on_telemetry(self.dcs.telemetry())
        self.air.on_telemetry(self.dcs.telemetry())  # used to raise "dictionary changed size during iteration"
        self.assertEqual((f.spawn, f.state, self.sq.aircraft_ready), (SpawnState.AGGREGATED, FlightState.PLANNED, 6))
        self.assertIn("removed by DCS right after spawning; check the airbase warehouse has 2 MiG-29A", self.notes)

    async def test_spawned_flights_get_distinct_numbers(self):
        a = self.add(flight(fid="a"))
        b = self.add(flight(fid="b"))
        await self.air.spawn(a)
        await self.air.spawn(b)
        boards = [u["onboard_num"] for op, d in self.dcs.calls if op == "spawn_group" for u in d["group"]["units"]]
        self.assertEqual(len(set(boards)), 4)

    async def test_aggregate_and_resume_in_the_air(self):
        f = self.add(flight())
        await self.air.spawn(f)
        await self.air.aggregate(f)
        self.assertEqual((f.spawn, f.state, f.start), (SpawnState.AGGREGATED, FlightState.ENROUTE, StartMode.AIR))
        self.assertEqual((f.pos, f.alt_m, f.hdg, f.speed_ms, f.fuel), (Pos(1_000, 2_000), 5_000, 45.0, 210.0, 0.55))
        await self.air.spawn(f)
        self.assertEqual(self.sq.aircraft_ready, 4)  # not taken from the squadron twice
        unit = self.dcs.calls[-1][1]["group"]["units"][0]
        self.assertEqual((unit["x"], unit["alt"], unit["payload"]["fuel_fraction"]), (1_000, 5_000, 0.55))

    async def test_exit_waypoint_recovers(self):
        f = self.add(flight())
        await self.air.spawn(f)
        self.air.on_telemetry({"groups": [{"id": "fl:f1", "pos": {"x": 40_000, "y": 0}, "in_air": True}]})
        self.assertEqual((f.next_wp, f.hold_until), (1, 600))  # in the orbit
        exits = self.air.on_telemetry({"groups": [{"id": "fl:f1", "pos": {"x": 99_000, "y": 0}, "in_air": True}]})
        self.assertEqual(exits, [f])
        await self.air.tick(exits)
        self.assertEqual((f.spawn, f.state, self.sq.aircraft_ready), (SpawnState.AGGREGATED, FlightState.LANDED, 6))

    def tele(self, x):
        return self.air.on_telemetry({"groups": [{"id": "fl:f1", "pos": {"x": x, "y": 0}, "in_air": True}]})

    async def test_passing_over_the_landing_field_is_not_landing(self):
        land = Waypoint(Pos(60_000, 0), 500, action="LAND", params={"airdrome_id": 18})
        f = self.add(flight(wps=[Waypoint(Pos(0, 0), 6_000), Waypoint(Pos(30_000, 0), 6_000), land]))
        await self.air.spawn(f)
        self.tele(58_000)  # overhead the field, still airborne: earlier waypoints passed, LAND still ahead
        self.assertEqual(f.next_wp, 2)
        await self.air.aggregate(f)
        self.assertEqual(fly(f, 0, 1), None)  # flies on to the field
        self.assertEqual(fly(f, 1, 600), "LAND")

    async def test_an_attack_waypoint_is_never_skipped(self):
        # Run 15: Blue CAS's egress point was near its ingress, so the attack was marked done early.
        attack = Waypoint(Pos(30_000, 0), 4_000, action="CAS")
        f = self.add(flight(wps=[Waypoint(Pos(0, 0), 4_000), attack, Waypoint(Pos(0, 5_000), 4_000)]))
        await self.air.spawn(f)
        self.tele(1_000)            # near the ingress and the egress point, attack still ahead
        self.assertEqual(f.next_wp, 1)
        self.tele(29_000)           # at the target
        self.assertEqual(f.next_wp, 2)

    async def test_orbit_tracked_on_campaign_clock(self):
        f = self.add(flight())
        await self.air.spawn(f)
        self.tele(36_000)
        self.assertEqual((f.next_wp, f.hold_until), (1, 600))
        self.state.clock = 300
        self.tele(44_000)  # still circling
        self.assertEqual((f.next_wp, f.hold_until), (1, 600))
        self.state.clock = 600
        self.tele(41_000)
        self.assertEqual((f.next_wp, f.hold_until), (2, None))

    async def test_orbit_done_while_leaving_the_area(self):
        f = self.add(flight())
        await self.air.spawn(f)
        self.tele(40_000)
        self.state.clock = 700
        self.tele(60_000)  # between waypoints, orbit time is up
        self.assertEqual((f.next_wp, f.hold_until), (2, None))

    async def test_respawn_mid_orbit_gets_the_rest_of_it(self):
        f = self.add(flight())
        await self.air.spawn(f)
        self.tele(40_000)
        self.state.clock = 400
        await self.air.aggregate(f)
        await self.air.spawn(f)
        points = self.dcs.calls[-1][1]["group"]["route"]["points"]
        orbit = [t for p in points for t in (p.get("task") or {}).get("params", {}).get("tasks", [])
                 if t["id"] == "ControlledTask"]
        self.assertEqual([t["params"]["stopCondition"]["duration"] for t in orbit], [200])

    async def test_launch_and_arrive_without_dcs(self):
        f = self.add(flight())
        self.air.launch_aggregated(f, Pos(5, 5))
        self.assertEqual((f.state, f.spawn, f.pos, f.start, self.sq.aircraft_ready),
                         (FlightState.ENROUTE, SpawnState.AGGREGATED, Pos(5, 5), StartMode.AIR, 4))
        with self.assertRaises(ValueError):
            self.air.launch_aggregated(f, None)
        self.air.arrive_aggregated(f, "EXIT")
        self.assertEqual((f.state, self.sq.aircraft_ready), (FlightState.LANDED, 6))
        self.assertIn("left the map (not in DCS): 2 aircraft back to the squadron", self.notes)

    async def test_destroyed(self):
        f = self.add(flight())
        await self.air.spawn(f)
        self.air.on_event(self.dcs.kill("fl:f1", "fl:f1#1"))
        self.air.on_event(self.dcs.kill("fl:f1", "fl:f1#2"))
        self.assertEqual((f.state, f.spawn, f.aircraft, self.sq.aircraft_ready),
                         (FlightState.LOST, SpawnState.AGGREGATED, 0, 4))

    async def test_restart_resumes_airborne_and_returns_parked(self):
        a = self.add(flight("a"))
        b = self.add(flight("b", start=StartMode.RAMP_HOT))
        await self.air.spawn(a)
        await self.air.spawn(b, takeoff=TAKEOFF)
        self.air.on_telemetry(self.dcs.telemetry(alt=4_000.0, hdg=10.0))
        self.air.on_hello(Hello(run_id="r2", spawned=[]), new_run=True)
        self.assertEqual((a.spawn, a.start, a.alt_m), (SpawnState.AGGREGATED, StartMode.AIR, 4_000.0))
        self.assertEqual((b.spawn, b.state), (SpawnState.AGGREGATED, FlightState.PLANNED))
        self.assertEqual(self.sq.aircraft_ready, 4)  # b's two are back; a's are still out

    async def test_orphans_removed(self):
        self.air.on_hello(Hello(run_id="r1", spawned=["fl:ghost", "gu:x", "Player"]), new_run=False)
        self.assertEqual(self.air.orphans, {"fl:ghost"})
        self.dcs.groups["fl:ghost"] = {"units": ["fl:ghost#1"], "pos": {}}
        await self.air.tick()
        self.assertEqual(self.air.orphans, set())


if __name__ == "__main__":
    unittest.main()
