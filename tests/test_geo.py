import math
import unittest

from campaign.geo import ll_to_xy


class ProjectionTests(unittest.TestCase):
    def test_matches_dcs(self):
        # What DCS's coord.LLtoLO gave for these places on Syria (Run 17). All 1,182 matched within 3 cm.
        for lat, lon, x, y in [(34.403706, 38.198771, -72655.484375, 209160.234375),
                               (35.227483, 33.513494, 32224.994140625, -216719.171875),
                               (36.703521, 37.891848, 182689.953125, 183818.71875),
                               (37.008674, 35.358075, 222173.015625, -41266.93359375)]:
            p = ll_to_xy("Syria", lat, lon)
            self.assertLess(math.dist((p.x, p.y), (x, y)), 0.1, (lat, lon))


if __name__ == "__main__":
    unittest.main()
