import unittest
from unittest import mock

from campaign import combat
from campaign.air_war import AirWar
from campaign.ground_war import GroundWar, SideOrders
from campaign.model import (Battle, CampaignState, Feature, Flight, FlightState, GroundOrder, GroundRole, GroundUnit,
                            Link, ObjectiveDef, ObjectiveState, ObjectiveType, Pos, Side, SpawnState, Squadron,
                            StartMode, Theater, Waypoint)
from campaign.persist import decode, encode
from campaign.planner import LEAD_S, Planner

KM = 1000.0


def obj(oid, x, t=ObjectiveType.CITY, country="Jordan", feats=()):
    return ObjectiveDef(oid, oid.title(), t, Pos(x, 0), list(feats), 2, country=country)


class Sky:
    """Blue airbase (bab) - home (blue town) - front (red town) - red airbase (rab), along x."""

    def __init__(self):
        rwy = [Feature("rwy_0", "1", 1.0)]
        objs = [obj("bab", -60 * KM, ObjectiveType.AIRBASE, feats=rwy), obj("home", 0),
                obj("front", 40 * KM, country="Syria"), obj("rab", 100 * KM, ObjectiveType.AIRBASE, "Syria", rwy)]
        self.theater = Theater("Syria", {o.id: o for o in objs}, [
            Link("bab", "home", "road", length_m=60 * KM), Link("home", "front", "road", length_m=40 * KM),
            Link("front", "rab", "road", length_m=60 * KM)])
        self.state = CampaignState("Syria", 0.0, {})
        for o in objs:
            self.state.objectives[o.id] = ObjectiveState(o.id, Side.BLUE if o.id in ("bab", "home") else Side.RED)
        sq = self.state.squadrons
        sq["vipers"] = Squadron("vipers", Side.BLUE, "Vipers", "F-16C_50", "bab", {"CAP", "SEAD", "CAS", "STRIKE"},
                                12, 0, 12, True)
        sq["hogs"] = Squadron("hogs", Side.BLUE, "Hogs", "A-10C_2", "bab", {"CAS"}, 8, 0, 8, True)
        sq["eagles"] = Squadron("eagles", Side.BLUE, "Eagles", "F-15C", "bab", {"CAP"}, 8, 0, 8, False)
        sq["fulcrums"] = Squadron("fulcrums", Side.RED, "Fulcrums", "MiG-29A", "rab", {"CAP"}, 8, 0, 8, False)
        self.air = AirWar(self.state, self.theater, {Side.BLUE: "offensive", Side.RED: "defensive"})

    def unit(self, uid, side, x, comp, role=GroundRole.ARMOR, at=None):
        u = GroundUnit(uid, side, uid, role, dict(comp), Pos(x, 0), at, GroundOrder.DEFEND)
        self.state.ground_units[uid] = u
        return u

    def flight(self, fid, squadron, mission, wps, n=2, pos=None, pkg="p1"):
        f = Flight(fid, pkg, squadron, fid.upper(), mission, n, "", wps, 0.0, start=StartMode.AIR)
        f.state, f.pos = FlightState.ENROUTE, pos or wps[0].pos
        self.state.flights[fid] = f
        return f


class AtTargetTests(unittest.TestCase):
    def test_sam_on_the_way_shoots_once_per_site(self):
        s = Sky()
        s.unit("sa6", Side.RED, 40 * KM, {"Kub 2P25 ln": 4, "Kub 1S91 str": 1}, GroundRole.SAM)
        f = s.flight("cas1", "vipers", "CAS", [Waypoint(Pos(0, 0), 5000), Waypoint(Pos(80 * KM, 0), 5000),
                                               Waypoint(Pos(-60 * KM, 0), 0, action="LAND")])
        with mock.patch.dict(combat.SAM_TYPES, {"SA-6": combat.SamType(1.0, 22_000)}):
            s.air.on_waypoint(f, 1)      # flew through the SA-6's range
        self.assertLess(f.aircraft, 2)
        n = f.aircraft
        with mock.patch.dict(combat.SAM_TYPES, {"SA-6": combat.SamType(1.0, 22_000)}):
            s.air.on_waypoint(f, 2)      # back through it: already faced
        self.assertEqual(f.aircraft, n)

    def test_a_site_without_its_radar_doesnt_shoot(self):
        s = Sky()
        s.unit("sa6", Side.RED, 40 * KM, {"Kub 2P25 ln": 4}, GroundRole.SAM)
        self.assertEqual(s.air.sam_sites(Side.RED), [])

    def test_cas_kills_the_biggest_formation_in_the_zone(self):
        s = Sky()
        small = s.unit("r1", Side.RED, 41 * KM, {"BTR-80": 2})
        big = s.unit("r2", Side.RED, 39 * KM, {"T-72B": 10})
        f = s.flight("cas1", "hogs", "CAS", [Waypoint(Pos(40 * KM, 0), 5000, action="CAS")])
        with mock.patch.object(combat, "cas_kills", return_value={"T-72B": 3}):
            s.air.on_waypoint(f, 0)
        self.assertEqual(big.composition, {"T-72B": 7})
        self.assertEqual(small.composition, {"BTR-80": 2})
        self.assertEqual(s.air.killed[Side.BLUE], 3)

    def test_sead_takes_out_a_radar_and_covers_the_package(self):
        s = Sky()
        site = s.unit("sa6", Side.RED, 40 * KM, {"Kub 2P25 ln": 4, "Kub 1S91 str": 1}, GroundRole.SAM)
        f = s.flight("sead1", "vipers", "SEAD", [Waypoint(Pos(40 * KM, 0), 6000, action="SEAD")], pkg="pk")
        with mock.patch.object(combat, "sead_kills", return_value=True), \
                mock.patch.object(combat, "sam_losses", return_value=[]):
            s.air.on_waypoint(f, 0)
        self.assertNotIn("Kub 1S91 str", site.composition)
        self.assertGreater(s.air.suppressed["pk"], s.state.clock)
        self.assertEqual(s.air.sam_sites(Side.RED), [])

    def test_strike_on_an_airbase_closes_the_runway_for_a_while(self):
        s = Sky()
        f = s.flight("str1", "vipers", "STRIKE", [Waypoint(Pos(100 * KM, 0), 7000, action="BOMB")], n=4)
        s.air.on_waypoint(f, 0)
        rab = s.state.objectives["rab"]
        self.assertAlmostEqual(rab.function(s.theater.objectives["rab"]), 0.4)
        for h in range(1, 4):
            s.state.clock = h * 3600.0
            s.air.advance(s.state.clock - 3600, s.state.clock)
        self.assertGreater(rab.function(s.theater.objectives["rab"]), 0.5)


class AirToAirTests(unittest.TestCase):
    def test_cap_jumps_a_strike_flight_that_then_goes_home(self):
        s = Sky()
        cap = s.flight("cap", "eagles", "CAP", [Waypoint(Pos(20 * KM, 0), 8000, action="ORBIT")], pos=Pos(20 * KM, 0))
        red = s.flight("rbai", "fulcrums", "BAI", [Waypoint(Pos(0, 0), 5000, action="CAS"),
                                                   Waypoint(Pos(100 * KM, 0), 0, action="LAND")], pos=Pos(25 * KM, 0))
        with mock.patch.object(combat, "air_battle", return_value=([], [0])):
            s.air.advance(0, 60)
        self.assertEqual(red.aircraft, 1)
        self.assertEqual(red.next_wp, 1)            # jettisoned, on its way home
        self.assertEqual(cap.aircraft, 2)

    def test_defensive_side_leaves_a_stronger_escort_alone(self):
        s = Sky()
        s.flight("rcap", "fulcrums", "CAP", [Waypoint(Pos(30 * KM, 0), 8000, action="ORBIT")], pos=Pos(30 * KM, 0))
        s.flight("hog", "hogs", "CAS", [Waypoint(Pos(40 * KM, 0), 5000, action="CAS")], pos=Pos(35 * KM, 0))
        s.flight("esc", "eagles", "ESCORT", [Waypoint(Pos(40 * KM, 0), 8000)], pos=Pos(36 * KM, 0))
        with mock.patch.object(combat, "air_battle", return_value=([], [])) as fight:
            with mock.patch("campaign.air_war.EVADE_CHANCE", 0.0):
                s.air._engagements(60)
        # Only the Eagles' own attack on the MiGs: the MiGs didn't go for the Hogs past the escort.
        for call in fight.call_args_list:
            self.assertNotIn("A-10C_2", call.args[1].aircraft + call.args[0].aircraft)

    def test_red_scrambles_at_unescorted_flights_over_its_ground_only(self):
        s = Sky()
        hog = s.flight("hog", "hogs", "CAS", [Waypoint(Pos(40 * KM, 0), 5000, action="CAS")], pos=Pos(45 * KM, 0))
        s.air.advance(0, 60)
        ints = [f for f in s.state.flights.values() if f.mission == "INTERCEPT"]
        self.assertEqual(len(ints), 1)
        self.assertEqual(ints[0].waypoints[0].pos, hog.waypoints[0].pos)
        # Covered by an F-15 CAP: left alone.
        s2 = Sky()
        s2.flight("hog", "hogs", "CAS", [Waypoint(Pos(40 * KM, 0), 5000, action="CAS")], pos=Pos(45 * KM, 0))
        s2.flight("cap", "eagles", "CAP", [Waypoint(Pos(40 * KM, 0), 8000, action="ORBIT")], pos=Pos(40 * KM, 0))
        s2.air.advance(0, 60)
        self.assertFalse([f for f in s2.state.flights.values() if f.mission == "INTERCEPT"])

    def test_state_survives_a_save(self):
        s = Sky()
        s.air.suppressed["p"] = 99.0
        s.air.losses.append((10.0, Side.RED, "SAM"))
        data = decode(dict, encode(s.air.export()), "x")
        a2 = AirWar(s.state, s.theater, {})
        a2.restore(data)
        self.assertEqual((a2.suppressed, a2.losses), ({"p": 99.0}, [(10.0, Side.RED, "SAM")]))


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.s = Sky()
        st = self.s.state
        self.blue = self.s.unit("b1", Side.BLUE, 37 * KM, {"M-1 Abrams": 10}, at=None)
        self.red = self.s.unit("r1", Side.RED, 40 * KM, {"T-72B": 10}, at="front")
        self.blue.battle = "front"
        st.battles["front"] = Battle("front", Side.BLUE, 0.0)
        self.gw = GroundWar(st, self.s.theater, {Side.BLUE: SideOrders("attack", "rab", "Coalition"),
                                                 Side.RED: SideOrders("defend", None, "Syria", frozenset({"Syria"}))})
        self.planner = Planner(st, self.s.theater, {Side.BLUE: "offensive", Side.RED: "defensive"}, self.gw, self.s.air)

    def test_plans_three_hours_of_flights_within_the_sortie_rate(self):
        self.planner.advance(0, 60)
        blue = [f for f in self.s.state.flights.values() if self.s.state.squadrons[f.squadron_id].side is Side.BLUE]
        self.assertTrue(blue)
        self.assertTrue(all(f.takeoff_time >= LEAD_S / 3 for f in blue))
        self.assertTrue(all(f.takeoff_time < 60 + 3 * 3600 + 3600 for f in blue))
        # 28 aircraft at 2.5 sorties a day: about 3 an hour, so at most about 10 over 3 hours (+ carry).
        self.assertLessEqual(sum(f.aircraft for f in blue), 14)
        self.assertIn("CAS", {f.mission for f in blue})
        cas = next(f for f in blue if f.mission == "CAS")
        self.assertEqual(cas.waypoints[-1].action, "LAND")
        self.assertTrue(cas.player_preferred)
        self.assertTrue(cas.loadout)

    def test_sead_goes_with_a_package_a_sam_covers(self):
        self.s.unit("sa6", Side.RED, 45 * KM, {"Kub 2P25 ln": 4, "Kub 1S91 str": 1}, GroundRole.SAM)
        self.planner.advance(0, 60)
        pkgs = [p for p in self.s.state.packages.values() if p.target_id == "front"]
        missions = {self.s.state.flights[i].mission for p in pkgs for i in p.flight_ids}
        self.assertIn("SEAD", missions)
        p = next(p for p in pkgs if len(p.flight_ids) > 1)
        main, sead = (self.s.state.flights[i] for i in p.flight_ids[:2])
        self.assertLess(sead.takeoff_time, main.takeoff_time + 1)   # SEAD gets there first

    def test_defender_keeps_cap_at_home_and_fighters_back(self):
        self.planner.advance(0, 60)
        red = [f for f in self.s.state.flights.values() if self.s.state.squadrons[f.squadron_id].side is Side.RED]
        self.assertTrue(any(f.mission == "CAP" for f in red))
        self.assertLessEqual(sum(f.aircraft for f in red), 8 - 2)   # 2 kept for intercepts

    def test_plan_and_flights_survive_a_save(self):
        self.planner.advance(0, 60)
        st = decode(CampaignState, encode(self.s.state), "state")
        self.assertEqual(set(st.flights), set(self.s.state.flights))
        p2 = Planner(st, self.s.theater, {}, self.gw, self.s.air)
        p2.restore(decode(dict, encode(self.planner.export()), "x"))
        self.assertEqual(p2.planned_until, self.planner.planned_until)


class WaypointHookTests(unittest.TestCase):
    def test_air_bubble_reports_each_waypoint_reached(self):
        from campaign.air import AirSpawner
        from campaign.air_bubble import AirBubble
        s = Sky()
        f = s.flight("f", "vipers", "CAS", [Waypoint(Pos(10 * KM, 0), 5000), Waypoint(Pos(20 * KM, 0), 5000),
                                             Waypoint(Pos(-60 * KM, 0), 0, action="LAND")], pos=Pos(0, 0))

        async def call(op, data):
            return {}
        bubble = AirBubble(s.state, AirSpawner(s.state, call), base_pos=lambda sq: None, takeoff_for=call)
        seen = []
        bubble.on_waypoint = lambda fl, i: seen.append(i)
        bubble.advance(20 * KM / 200)
        self.assertEqual(seen[:2], [0, 1])


class ReproducibleTests(unittest.TestCase):
    def test_same_seed_same_war_whatever_the_hash_seed(self):
        """Iterating a set of strings changes order with PYTHONHASHSEED; the war mustn't."""
        import os
        import subprocess
        import sys
        code = ("from campaign.scenario_file import *; from campaign.war import build\n"
                "sd = load_scenario_def(scenario_path('southern_syria')); b = build_state(sd); w = build(sd, b)\n"
                "for _ in range(5 * 60): w.step(60.0)\n"
                "print(sorted((f.id, f.aircraft, f.state.name, round(f.takeoff_time)) for f in b.state.flights.values()))\n"
                "print(sorted((u.id, sum(u.composition.values())) for u in b.state.ground_units.values()))")
        outs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                               env={**os.environ, "PYTHONHASHSEED": h}).stdout for h in ("1", "2")}
        self.assertEqual(len(outs), 1)


if __name__ == "__main__":
    unittest.main()
