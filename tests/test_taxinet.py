import math
import struct
import unittest

from campaign import ride, taxinet
from campaign.model import Pos

TAXI, RUNWAY = 1, taxinet.RUNWAY


def record(eid: int, etype: int, a: int, b: int, pts: list[tuple[float, float]]) -> bytes:
    """One edge as DCS writes it (see campaign/taxinet.py), with some trailing data."""
    head = struct.pack("<9i", 0, eid, etype, a, 0, b, 1, 0, len(pts))
    body = b"".join(struct.pack("<ddd", x, 0.0, z) for x, z in pts)
    return head + body + struct.pack("<5i", 5, 7, 0, 3, 1) + b"\x00" * 13


def rn5(edges: list[tuple[int, int, int, int, list[tuple[float, float]]]]) -> bytes:
    names = b"".join(struct.pack("<i", len(n)) + n for n in (b"t5::rn5file", b"taxiway_15m", b"taxiway_24m",
                                                              b"taxiway_34m", b"runway_45m"))
    return struct.pack("<4i", 2, 48, 0, 0) + b"\x00" * 24 + names + struct.pack("<i", len(edges)) + b"".join(
        record(*e) for e in edges)


#   A square of taxiways with a runway as the short cut across it. Nodes: 0 (0,0), 1 (0,1000),
#   2 (1000,1000), 3 (1000,0). Edges stop 10 m short of their junctions, as DCS's do.
X0 = -200_000.0
Y0 = 50_000.0


def p(x: float, y: float) -> tuple[float, float]:
    return (X0 + x, Y0 + y)


SQUARE = [
    (0, TAXI, 0, 1, [p(0, 10), p(0, 500), p(0, 990)]),
    (1, TAXI, 1, 2, [p(10, 1000), p(500, 1000), p(990, 1000)]),
    (2, TAXI, 2, 3, [p(1000, 990), p(1000, 500), p(1000, 10)]),
    (3, TAXI, 3, 0, [p(990, 0), p(500, 0), p(10, 0)]),
    (4, RUNWAY, 0, 2, [p(7, 7), p(500, 500), p(993, 993)]),
]


def P(x: float, y: float) -> Pos:
    return Pos(X0 + x, Y0 + y)


class TaxiNetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.net = taxinet.parse(rn5(SQUARE))

    def test_parse(self) -> None:
        self.assertEqual([e.id for e in self.net.edges], [0, 1, 2, 3, 4])
        self.assertEqual(self.net.edges[4].type, RUNWAY)
        self.assertAlmostEqual(self.net.edges[0].points[1].x, X0)
        self.assertAlmostEqual(self.net.edges[0].points[1].y, Y0 + 500)
        for n, (x, y) in {0: (0, 0), 1: (0, 1000), 2: (1000, 1000), 3: (1000, 0)}.items():
            self.assertLess(math.dist((self.net.nodes[n].x, self.net.nodes[n].y), (X0 + x, Y0 + y)), 15, n)

    def test_route_keeps_off_the_runway(self) -> None:
        # From near node 0 to near node 2: round the square (2,000 m), not along the runway (1,414 m).
        r = self.net.route(P(-20, 20), P(1020, 980))
        self.assertEqual((r[0].x, r[0].y), (X0 - 20, Y0 + 20))
        self.assertEqual((r[-1].x, r[-1].y), (X0 + 1020, Y0 + 980))
        self.assertGreater(taxinet.route_length(r), 1900)
        for q in r[1:-1]:   # every point on the taxiways, none in the middle of the square
            self.assertTrue(min(abs(q.x - X0), abs(q.x - X0 - 1000), abs(q.y - Y0), abs(q.y - Y0 - 1000)) < 15, q)

    def test_route_along_one_edge(self) -> None:
        r = self.net.route(P(-5, 100), P(-5, 800))
        self.assertLess(taxinet.route_length(r), 720)
        self.assertTrue(all(abs(q.x - X0) < 6 for q in r))

    def test_route_is_continuous(self) -> None:
        r = self.net.route(P(300, -30), P(1030, 700))
        steps = [math.dist((a.x, a.y), (b.x, b.y)) for a, b in zip(r, r[1:])]
        self.assertLess(max(steps), 1000)   # no jump across the square
        self.assertLess(taxinet.route_length(r), 1500)

    def test_garbage_is_not_a_network(self) -> None:
        with self.assertRaises(ValueError):
            taxinet.parse(b"\x00" * 500)


class RideTest(unittest.TestCase):
    def setUp(self) -> None:
        self.net = taxinet.parse(rn5(SQUARE))

    def test_stop_beside_a_jet(self) -> None:
        jet = P(500, -40)   # parked 40 m off the bottom taxiway
        stop = ride.stop_beside(self.net, jet)
        self.assertAlmostEqual(stop.x, X0 + 500, places=3)
        self.assertAlmostEqual(stop.y, Y0 - 40 + ride.STOP_M, places=3)
        self.assertAlmostEqual(ride.parked_heading(self.net, jet), math.pi / 2, places=3)   # nose east, to the taxiway

    def test_jet_on_its_stand_spoke(self) -> None:
        # A stand as DCS has them: the end of a spoke (edge 5) off junction 0, heading south-west.
        net = taxinet.parse(rn5(SQUARE + [(5, 0, 9, 0, [p(-60, -60), p(-30, -30), p(-7, -7)])]))
        jet = P(-60, -60)
        stop = ride.stop_beside(net, jet)
        k = ride.STOP_M / math.sqrt(2)
        self.assertAlmostEqual(stop.x, X0 - 60 + k, places=3)    # out along the spoke, not on the jet
        self.assertAlmostEqual(stop.y, Y0 - 60 + k, places=3)
        self.assertAlmostEqual(ride.parked_heading(net, jet), math.pi / 4, places=3)   # nose up the spoke
        legs = ride.plan(net, P(-30, 500), [jet])
        self.assertEqual((legs[0].to.x, legs[0].to.y), (stop.x, stop.y))
        self.assertLess(taxinet.route_length(legs[0].points), 700)   # down the left side

    def test_plan_visits_the_nearest_jet_first_then_drives_off(self) -> None:
        start = P(-30, 500)
        jets = [P(1040, 500), P(500, -40)]
        legs = ride.plan(self.net, start, jets)
        self.assertEqual([leg.jet for leg in legs], [1, 0, None])
        self.assertGreater(math.dist((legs[-1].to.x, legs[-1].to.y), (legs[1].to.x, legs[1].to.y)), 100)   # drives away
        self.assertEqual(legs[1].points[0], legs[0].to)   # each leg starts where the last ended
        pts = ride.waypoints(legs[0].points, 5.5)
        self.assertEqual(pts[0]["action"], "Off Road")
        self.assertEqual(pts[-1]["x"], round(legs[0].to.x, 1))

    def test_without_a_network_it_drives_straight(self) -> None:
        legs = ride.plan(None, P(0, 0), [P(100, 0)])
        self.assertTrue(all(abs(q.y - Y0) < 1e-6 for q in legs[0].points))   # one straight line
        self.assertAlmostEqual(legs[0].to.x, X0 + 100 - ride.STOP_M)
        self.assertAlmostEqual(legs[-1].to.x, X0 + 100 - ride.STOP_M + ride.AWAY_M)   # and straight on

    def test_down_the_row_and_away(self) -> None:
        # Run 30's sketch: two jets side by side, facing the bottom taxiway (east); the Humvee comes in
        # along the row in front of their noses, stops beside each, and carries on the same way.
        jets = [P(300, -60), P(360, -60)]
        legs = ride.plan(self.net, P(200, -300), jets, [math.pi / 2, math.pi / 2])
        self.assertEqual([leg.jet for leg in legs], [0, 1, None])
        s0, s1 = legs[0].to, legs[1].to
        self.assertAlmostEqual(s0.x, X0 + 300); self.assertAlmostEqual(s0.y, Y0 - 60 + ride.STOP_M)
        self.assertAlmostEqual(s1.x, X0 + 360); self.assertAlmostEqual(s1.y, Y0 - 60 + ride.STOP_M)
        self.assertEqual(legs[0].points[-2], ride._along(s0, (1.0, 0.0), -ride.IN_M))   # joins the row before it
        self.assertLess(abs(legs[0].points[-3].y - Y0), 1)            # straight off the bottom taxiway
        self.assertEqual(legs[1].points, [s0, s1])                     # straight down the row
        out = legs[2].points[1]                                        # and on, the same way
        self.assertAlmostEqual(out.x, X0 + 360 + ride.APPROACH_M); self.assertAlmostEqual(out.y, s1.y)
        self.assertAlmostEqual(taxinet.route_length(legs[2].points), ride.AWAY_M, places=3)   # gone after 250 m
        self.assertGreater(legs[2].to.x, s1.x + 100)                  # along the bottom taxiway, ahead
        self.assertLess(abs(legs[2].to.y - Y0), 15)

    def test_straight_on_when_the_taxiways_lie_behind(self) -> None:
        # Two jets on a ramp past the end of a taxiway spoke: no way on ahead, so straight on, not back.
        net = taxinet.parse(rn5(SQUARE + [(5, TAXI, 0, 9, [p(-7, -7), p(-100, -100), p(-200, -200)])]))
        jets = [P(-210, -260), P(-250, -300)]
        h = [math.radians(315)] * 2
        legs = ride.plan(net, P(-30, 500), jets, h)
        u = ride._unit(legs[0].to, legs[1].to)
        off = legs[2].points
        self.assertAlmostEqual(taxinet.route_length(off), ride.AWAY_M, places=3)
        for a, b in zip(off, off[1:]):   # every step forward along the row
            v = ride._unit(a, b)
            self.assertGreater(v[0] * u[0] + v[1] * u[1], 0.99)

    def box(self, cx: float, cy: float, hx: float, hy: float, t: str = "hangar") -> dict:
        return {"type": t, "pos": {"x": X0 + cx, "y": Y0 + cy},
                "box": [{"x": X0 + cx + dx, "y": Y0 + cy + dy} for dx, dy in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy))]}

    def test_a_clear_start_outside_the_building(self):
        # A 60 x 40 m terminal 100 m off the bottom taxiway (y 0): the start is clear of it, on the
        # taxiway side, not 20 m from its centre (Run 31: inside the terminal).
        obs = ride.footprints([self.box(500, -100, 30, 20, "AIRPORT_BUILDING1_TOWER")])
        p, d = ride.clear_spot(P(500, -100), self.net, obs)
        self.assertGreaterEqual(ride.clearance(p, obs), ride.CLEAR_M)
        self.assertLess(ride.clearance(p, obs), ride.CLEAR_M + ride.SEARCH_STEP_M)   # but right beside it
        self.assertGreater(p.y, Y0 - 100 + 20)                                        # the taxiway side
        self.assertAlmostEqual(p.x, X0 + 500, delta=1)

    def test_a_clear_start_round_a_blocked_side(self):
        # A wall of hangars between the terminal and the taxiway: start on a side with a clear way out.
        obs = ride.footprints([self.box(500, -100, 30, 20), self.box(500, -60, 200, 10)])
        p, _ = ride.clear_spot(P(500, -100), self.net, obs)
        self.assertGreaterEqual(ride.clearance(p, obs), ride.CLEAR_M)
        self.assertTrue(ride._line_clear(p, ride.taxi_entry(self.net, p), obs, ride.PATH_CLEAR_M))

    def test_objects_without_boxes_get_a_radius(self):
        fp = ride.footprints([{"type": "SYRIA_BLOCK_BUILDING_04", "pos": {"x": 0, "y": 0}},
                              {"type": "TRASHCAN_EU_SMALL", "pos": {"x": 50, "y": 0}}])
        self.assertEqual([f.radius for f in fp], [15.0, ride.NO_BOX_DEFAULT_M])
        self.assertEqual(fp[0].distance(Pos(20, 0)), 5.0)

    def test_footprint_from_the_model_box_turned_as_it_stands(self) -> None:
        # A 10 x 4 m model (x forward), facing east: 10 m east-west, 4 m north-south.
        boxes = {"office": [-5.0, 0.0, -2.0, 5.0, 6.0, 2.0]}
        o = {"type": "OFFICE", "pos": {"x": 0.0, "y": 0.0}, "fwd": {"x": 0.0, "y": 1.0}, "right": {"x": -1.0, "y": 0.0}}
        fp = ride.footprints([o], boxes)[0]
        self.assertEqual(fp.distance(Pos(0.0, 4.9)), 0.0)        # inside, along its length
        self.assertAlmostEqual(fp.distance(Pos(3.0, 0.0)), 1.0)   # 2 m half-width
        unturned = ride.footprints([{"type": "office", "pos": {"x": 0.0, "y": 0.0}}], boxes)[0]
        self.assertAlmostEqual(unturned.radius, math.hypot(5, 2))  # facing unknown: a circle round it

    def test_admin_building_by_model_and_distance(self) -> None:
        jets = [P(500, -40)]
        objs = [{"type": "AIRFIELDS_BUILDING_24", "pos": {"x": X0 + 500, "y": Y0 - 80}},   # a shed, nearest
                {"type": "AIRFIELDS_BUILDING_7", "pos": {"x": X0 + 600, "y": Y0 - 100}},   # office, ~117 m
                {"type": "AIRFIELDS_BUILDING_21", "pos": {"x": X0 + 800, "y": Y0 - 100}},  # HQ, ~306 m
                {"type": "AIRFIELDS_BUILDING_21", "pos": {"x": X0 + 500, "y": Y0 - 3000}}]  # HQ, off the grounds
        self.assertEqual(ride.pick_admin(objs, self.net, jets)["pos"]["x"], X0 + 600)   # 117 + 150 < 306 + 0
        objs[1]["pos"]["x"] = X0 + 900   # the office farther (~402 + 150): the HQ now
        self.assertEqual(ride.pick_admin(objs, self.net, jets)["type"], "AIRFIELDS_BUILDING_21")
        self.assertIsNone(ride.pick_admin(objs[:1], self.net, jets))

    def test_briefing_point_is_the_building_nearest_the_jets(self) -> None:
        objs = [{"type": "hangar_1", "pos": {"x": X0 + 520, "y": Y0 - 100}},
                {"type": "KDP_tower", "pos": {"x": X0 + 200, "y": Y0 - 150}},
                {"type": "tree", "pos": {"x": X0 + 500, "y": Y0 - 30}},
                {"type": "far_building", "pos": {"x": X0 + 500, "y": Y0 - 5000}}]
        p, what = ride.briefing_point(objs, self.net, [P(500, -40)])
        self.assertEqual(what, "hangar_1")   # not the tower (Run 31)
        self.assertAlmostEqual(p.x, X0 + 520)
        self.assertAlmostEqual(p.y, Y0 - 100 + ride.BESIDE_M)
        p, what = ride.briefing_point(objs[1:], self.net, [P(500, -40)])
        self.assertEqual(what, "KDP_tower")
        self.assertIsNone(ride.briefing_point(objs[2:3], self.net, []))


if __name__ == "__main__":
    unittest.main()
