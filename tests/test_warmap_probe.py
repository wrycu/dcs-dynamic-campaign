import asyncio
import re
import unittest
from pathlib import Path

from campaign.protocol import Src
from tools.warmap_probe import ROUTE_NM, Probe, along, at, war_picture

CLIENT = (Path(__file__).resolve().parent.parent / "lua" / "client" / "campaign_client.lua").read_text()


def lua_table_keys(name: str) -> set[str]:
    body = re.search(rf"local {name} = \{{(.*?)\n\}}", CLIENT, re.S).group(1)
    return set(re.findall(r"(\w+) = ", body))


class FakeConn:
    def __init__(self, src):
        self.src = src
        self.calls = []

    async def call(self, op, data, **kw):
        self.calls.append((op, data))
        return {}


class WarMapProbeTests(unittest.TestCase):
    def test_picture_uses_only_what_the_client_draws(self):
        kinds = set(re.findall(r'k == "(\w+)"', CLIENT))
        symbols, styles = lua_table_keys("SYMBOLS"), lua_table_keys("LINE_STYLES")
        spec = war_picture()
        for o in spec["objects"]:
            self.assertIn(o["kind"], kinds)
            if "sym" in o:
                self.assertIn(o["sym"], symbols)
            if "style" in o:
                self.assertIn(o["style"], styles)
            for p in o.get("points", []) + ([o] if "x" in o else []):
                self.assertIsInstance(p["x"], float)
                self.assertIsInstance(p["y"], float)
        self.assertEqual({o["kind"] for o in spec["objects"]}, kinds)   # every kind gets tried

    def test_the_war_moves_on(self):
        line = lambda s: next(o for o in s["objects"] if o["kind"] == "line")["points"]
        before, after = line(war_picture(0)), line(war_picture(2))
        self.assertLess(after[0]["y"], before[0]["y"])                   # the front moved west
        self.assertTrue(war_picture(1, keep_view=True)["keep_view"])

    def test_along_the_route(self):
        self.assertEqual(along(ROUTE_NM, 0.0)[0], at(0, 0))
        pos, hdg = along(ROUTE_NM, 0.01)
        self.assertGreater(pos["x"], at(0, 0)["x"])
        self.assertAlmostEqual(hdg, 50.2, delta=0.1)                     # first leg: 10 nm north, 12 east

    def test_map_command_goes_to_the_client_script(self):
        async def run():
            probe = Probe()
            hooks = FakeConn(Src.HOOKS)
            probe.on_hello(hooks, None, True)
            await probe.command({"player_id": 1, "name": "host", "text": "map"})
            await probe.command({"player_id": 1, "name": "host", "text": "map move"})
            rpcs = [d for op, d in hooks.calls if op == "client_rpc"]
            self.assertEqual([d["method"] for d in rpcs], ["campaignMap", "campaignMap"])
            self.assertFalse(rpcs[0]["args"][0]["keep_view"])
            self.assertTrue(rpcs[1]["args"][0]["keep_view"])
            self.assertEqual(probe.viewers, {1})
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
