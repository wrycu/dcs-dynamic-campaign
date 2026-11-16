import json
import tempfile
import unittest
from pathlib import Path

from campaign.dtc import DtcWaypoint, hornet_dtc, write_dtc
from campaign.model import Pos


class HornetDtcTests(unittest.TestCase):
    def test_structure(self):
        dtc = hornet_dtc("t", "Syria", [DtcWaypoint(Pos(1, 2), 300, "a"), DtcWaypoint(Pos(3, 4), 10)])
        self.assertEqual(dtc["type"], "FA-18C_hornet")
        wypt = dtc["data"]["WYPT"]
        self.assertFalse(wypt["mirror_NAV_PTS"])
        self.assertEqual(dtc["data"]["terrain"], "Syria")
        # Partitions present replace the jet's defaults; stubs crash the importer.
        self.assertEqual(set(dtc["data"]), {"type", "name", "terrain", "WYPT"})
        p1, p2 = wypt["NAV_PTS"]
        self.assertEqual((p1["wypt_num"], p1["x"], p1["y"], p1["alt"], p1["note"]), (1, 1, 2, 300, "a"))
        self.assertEqual((p2["R1"], p2["id"]), (False, "STPT2"))  # no sequence, like the editor
        self.assertNotIn("R1_order", p2)
        self.assertNotIn("NAV_ROUTE", wypt)

    def test_route_sequence(self):
        dtc = hornet_dtc("t", "Syria", [DtcWaypoint(Pos(1, 2), 300), DtcWaypoint(Pos(3, 4), 10)], route=2)
        wypt = dtc["data"]["WYPT"]
        p2 = wypt["NAV_PTS"][1]
        self.assertEqual((p2["R2"], p2["R2_order"], p2["R1"]), (True, 2, False))
        r1, r2, r3 = wypt["NAV_ROUTE"]
        self.assertEqual((r1, r3), ({}, {}))
        self.assertEqual(r2["STPT2"]["wypt_num"], 2)
        self.assertEqual(r2["STPT2"]["route_num"], 2)

    def test_limits(self):
        with self.assertRaises(ValueError):
            hornet_dtc("t", "Syria", [])

    def test_write(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_dtc(Path(d) / "DTC", hornet_dtc("x", "Syria", [DtcWaypoint(Pos(0, 0), 0)]))
            self.assertEqual(path.name, "x.dtc")
            self.assertEqual(json.loads(path.read_text())["name"], "x")


if __name__ == "__main__":
    unittest.main()
