import unittest

from campaign.model import Pos
from campaign.parking import SHELTER, Size, Stand, fits, size_from, stands_from, usable_spots

HORNET = Size(span=12.3, length=17.1, height=4.7)
SMALL_SHELTER = Stand(Pos(0, 0), shelter=True, airplanes=True, width=10.5, length=20.0, height=6.0)
BIG_SHELTER = Stand(Pos(100, 0), shelter=True, airplanes=True, width=18.0, length=22.0, height=6.0)
OPEN = Stand(Pos(200, 0), shelter=False, airplanes=True, width=30.0, length=30.0)
HELI_PAD = Stand(Pos(300, 0), shelter=False, airplanes=False, width=30.0, length=30.0)


def spot(term: int, typ: int, x: float) -> dict:
    return {"term": term, "type": typ, "pos": {"x": x, "y": 0.0}}


SPOTS = [spot(1, SHELTER, 2), spot(2, SHELTER, 101), spot(3, 104, 198), spot(4, 104, 300), spot(5, 16, 400)]


class ParkingTests(unittest.TestCase):
    def test_fits_like_the_mission_editor(self):
        self.assertFalse(fits(SMALL_SHELTER, HORNET))   # Run 25: wing through the wall
        self.assertTrue(fits(BIG_SHELTER, HORNET))
        self.assertTrue(fits(OPEN, HORNET))            # no height limit
        self.assertFalse(fits(HELI_PAD, HORNET))
        self.assertFalse(fits(Stand(Pos(0, 0), True, True, 18.0, 22.0, height=4.0), HORNET))
        self.assertFalse(fits(Stand(Pos(0, 0), False, True, None, None), HORNET))

    def test_only_spots_it_fits(self):
        got = usable_spots(SPOTS, [SMALL_SHELTER, BIG_SHELTER, OPEN, HELI_PAD], HORNET)
        self.assertEqual([s["term"] for s in got], [2, 3])  # 4 is a helicopter pad, 5 not a fighter type

    def test_unmatched_spot_is_not_used(self):
        self.assertEqual(usable_spots([spot(9, 104, 150)], [OPEN], HORNET), [])

    def test_without_sizes_no_shelters(self):
        self.assertEqual([s["term"] for s in usable_spots(SPOTS, None, HORNET)], [3, 4])
        self.assertEqual([s["term"] for s in usable_spots(SPOTS, [OPEN], None)], [3, 4])

    def test_from_replies(self):
        stands = stands_from({"stands": [{"index": 7, "x": 1, "y": 2, "shelter": True, "airplanes": True,
                                          "helicopters": False, "width": 18, "length": 22}]})
        self.assertEqual(stands, [Stand(Pos(1.0, 2.0), True, True, 18.0, 22.0, None)])
        self.assertEqual(stands_from({}), [])
        self.assertEqual(size_from({"span": 12.3, "length": 17.1, "height": 4.7}), HORNET)


if __name__ == "__main__":
    unittest.main()
