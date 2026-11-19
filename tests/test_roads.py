import math
import unittest

from campaign.model import Pos
from campaign.roads import Path, simplify

L = Path([Pos(0, 0), Pos(1000, 0), Pos(1000, 1000)])  # north 1 km, then east 1 km


class PathTests(unittest.TestCase):
    def test_length_and_points(self):
        self.assertEqual(L.length, 2000)
        self.assertEqual(L.point_at(500), Pos(500, 0))
        self.assertEqual(L.point_at(1500), Pos(1000, 500))
        self.assertEqual(L.point_at(-5), Pos(0, 0))
        self.assertEqual(L.point_at(9999), Pos(1000, 1000))

    def test_heading(self):
        self.assertAlmostEqual(L.heading_at(10), 0.0)               # north
        self.assertAlmostEqual(L.heading_at(1500), math.pi / 2)     # east
        self.assertAlmostEqual(Path([Pos(0, 0), Pos(0, 0), Pos(0, 10)]).heading_at(0), math.pi / 2)

    def test_project(self):
        p = L.project(Pos(1100, 400))  # 100 m north of the second leg
        self.assertAlmostEqual(p.along, 1400)
        self.assertAlmostEqual(p.offset, 100)
        self.assertEqual(L.project(Pos(-50, 0)).along, 0)

    def test_after_and_reversed(self):
        self.assertEqual(L.after(500), [Pos(500, 0), Pos(1000, 0), Pos(1000, 1000)])
        self.assertEqual(L.after(1500), [Pos(1000, 500), Pos(1000, 1000)])
        self.assertEqual(L.reversed().points[0], Pos(1000, 1000))

    def test_single_point(self):
        p = Path([Pos(5, 5)])
        self.assertEqual((p.length, p.point_at(3), p.project(Pos(5, 8)).offset), (0, Pos(5, 5), 3))

    def test_simplify(self):
        wiggle = [Pos(0, 0), Pos(100, 1), Pos(200, -1), Pos(300, 0), Pos(300, 100), Pos(300, 200)]
        self.assertEqual(simplify(wiggle, 5), [Pos(0, 0), Pos(300, 0), Pos(300, 200)])
        self.assertEqual(simplify(wiggle, 0.5), wiggle[:4] + wiggle[5:])  # (300, 100) is on a straight leg
        self.assertEqual(simplify(wiggle[:2], 5), wiggle[:2])


if __name__ == "__main__":
    unittest.main()
