import unittest

from campaign.bubble import NM
from campaign.model import Pos
from tools.ghosts import Ghosts, parse


class GhostTests(unittest.TestCase):
    def test_bearing_range_is_magnetic_from_the_reference(self):
        g = parse("085/10", magvar=5.0, ref=Pos(0, 0))  # 085 magnetic = 090 true: due east
        self.assertAlmostEqual(g.pos.x, 0, places=6)
        self.assertAlmostEqual(g.pos.y, 10 * NM)

    def test_bad_spec(self):
        with self.assertRaises(ValueError):
            parse("east/10", magvar=0)

    def test_airbase_name_waits_for_dcs(self):
        self.assertIsNone(parse("Damascus", magvar=5).pos)

    def test_inject_appends_placed_ghosts_only(self):
        ghosts = Ghosts(["000/10", "Damascus"], magvar=0)
        data = {"players": [{"name": "me"}], "groups": []}
        out = ghosts.inject(data)
        self.assertEqual([p["name"] for p in out["players"]], ["me", "GHOST 1"])
        self.assertEqual(len(data["players"]), 1)  # the original is untouched
        self.assertIs(Ghosts([], 0).inject(data), data)

    async def _setup(self, ghosts, answers):
        calls = []

        async def call(cmd, params):
            calls.append((cmd, params))
            return answers.get(params.get("airbase")) if cmd == "query" else None
        return await ghosts.setup(call), calls

    def test_setup_places_airbases_and_draws(self):
        import asyncio
        ghosts = Ghosts(["Damascus", "Nowhere"], magvar=0)
        lines, calls = asyncio.run(self._setup(ghosts, {"Damascus": {"exists": True, "pos": {"x": 1.0, "y": 2.0}}}))
        self.assertEqual(ghosts.ghosts[0].pos, Pos(1.0, 2.0))
        self.assertIn("not found", lines[1])
        self.assertEqual([c for c, _ in calls], ["query", "draw", "query"])


if __name__ == "__main__":
    unittest.main()
