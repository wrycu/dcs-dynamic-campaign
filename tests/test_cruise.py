import unittest

from campaign.air_bubble import fly
from campaign.catalog import DEFAULT_CRUISE_KT, KT, cruise_ms, flight_cruise_ms
from campaign.flight_spawn import ORBIT_SPEED, build_flight_spawn
from campaign.model import CampaignState, Flight, Package, Pos, Side, Squadron, Waypoint


def squadron(sid, actype):
    return Squadron(id=sid, side=Side.BLUE, name=sid, aircraft_type=actype, base_id="x", roles=set(),
                    aircraft_ready=8, aircraft_damaged=0, pilots=8, player_flyable=False)


def flight(fid, sid, package="p1", wps=None):
    return Flight(id=fid, package_id=package, squadron_id=sid, callsign=fid, mission="CAS", aircraft=2,
                  loadout="", takeoff_time=0, waypoints=wps or [Waypoint(Pos(0, 0), 3000)])


class CruiseTests(unittest.TestCase):
    def test_per_type_with_a_default(self):
        self.assertAlmostEqual(cruise_ms("A-10C_2"), 300 * KT)
        self.assertAlmostEqual(cruise_ms("F-16C_50"), 480 * KT)
        self.assertAlmostEqual(cruise_ms("No Such Jet"), DEFAULT_CRUISE_KT * KT)

    def test_a_package_flies_at_its_slowest_flight(self):
        st = CampaignState(theater_map="Syria", clock=0.0, sides={})
        st.squadrons = {"vipers": squadron("vipers", "F-16C_50"), "hogs": squadron("hogs", "A-10C_2")}
        st.flights = {"v": flight("v", "vipers"), "h": flight("h", "hogs"), "lone": flight("lone", "vipers", "p2")}
        st.packages = {"p1": Package(id="p1", side=Side.BLUE, target_id="t", tot=0, flight_ids=["v", "h"])}
        self.assertAlmostEqual(flight_cruise_ms(st, st.flights["v"]), 300 * KT)
        self.assertAlmostEqual(flight_cruise_ms(st, st.flights["lone"]), 480 * KT)   # no such package

    def test_a_leg_can_set_its_own_speed(self):
        f = flight("f", "s", wps=[Waypoint(Pos(10_000, 0), 3000),
                                  Waypoint(Pos(20_000, 0), 3000, speed_ms=50)])
        f.pos = Pos(0, 0)
        fly(f, 0, 100, cruise=200)    # 10 km at 200 m/s takes 50 s; then 50 s at 50 m/s
        self.assertAlmostEqual(f.pos.x, 12_500)
        self.assertEqual(f.speed_ms, 50)

    def test_dcs_route_speeds(self):
        f = flight("f", "s", wps=[Waypoint(Pos(10_000, 0), 3000),
                                  Waypoint(Pos(20_000, 0), 3000, action="ORBIT"),
                                  Waypoint(Pos(30_000, 0), 3000, speed_ms=100)])
        g = build_flight_spawn(f, "A-10C_2", Side.BLUE).data["group"]
        pts = g["route"]["points"]
        self.assertAlmostEqual(pts[0]["speed"], 300 * KT)
        self.assertEqual(pts[2]["speed"], 100)
        orbit = pts[1]["task"]["params"]["tasks"][0]["params"]["task"]["params"]
        self.assertAlmostEqual(orbit["speed"], 300 * KT * ORBIT_SPEED)
        self.assertAlmostEqual(g["units"][0]["speed"], 300 * KT)


if __name__ == "__main__":
    unittest.main()
