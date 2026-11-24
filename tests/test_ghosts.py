import asyncio
import unittest

from campaign.bubble import NM
from campaign.ghosts import Ghosts, offset, parse
from campaign.model import Pos


class GhostTests(unittest.TestCase):
    def test_bearing_range_is_magnetic(self):
        p = offset(Pos(0, 0), 85, 10, magvar=5.0)   # 085 magnetic = 090 true: due east
        self.assertAlmostEqual(p.x, 0, places=6)
        self.assertAlmostEqual(p.y, 10 * NM)

    def test_bad_spec(self):
        with self.assertRaises(ValueError):
            parse("east/10")

    def test_place(self):
        bases = {"Home": Pos(0, 0), "Damascus": Pos(1.0, 2.0)}
        g = Ghosts(["000/10", "Damascus", "Nowhere"])
        lines = g.place(bases.get, "Home", magvar=0)
        self.assertAlmostEqual(g.ghosts[0].pos.x, 10 * NM)
        self.assertEqual(g.ghosts[1].pos, Pos(1.0, 2.0))
        self.assertIsNone(g.ghosts[2].pos)
        self.assertIn("ignored", lines[2])

    def test_inject_appends_placed_ghosts_only(self):
        g = Ghosts(["000/10", "Nowhere"])
        g.place({"Home": Pos(0, 0)}.get, "Home", magvar=0)
        out = g.inject({"players": [{"pos": {"x": 5, "y": 5}}]})
        self.assertEqual([p.get("name") for p in out["players"]], [None, "GHOST 1"])
        self.assertEqual(Ghosts([]).inject({"players": []}), {"players": []})

    def test_draw(self):
        calls = []

        async def call(op, data):
            calls.append((op, data["text"]))

        g = Ghosts(["Damascus"])
        g.place({"Damascus": Pos(1, 2)}.get, "", magvar=0)
        asyncio.run(g.draw(call))
        self.assertEqual(calls, [("draw", "GHOST 1 (Damascus)")])


if __name__ == "__main__":
    unittest.main()
