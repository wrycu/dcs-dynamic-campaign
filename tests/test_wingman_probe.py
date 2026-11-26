import argparse
import asyncio
import unittest
from pathlib import Path
from unittest import mock

from campaign.protocol import Src
from tools.wingman_probe import Probe


class FakeConn:
    def __init__(self, src):
        self.src = src
        self.calls = []

    async def call(self, op, data, **kw):
        self.calls.append(op)
        return {}


class WingmanProbeTests(unittest.TestCase):
    def test_menu_goes_to_the_mission_not_the_hooks(self):
        async def run():
            probe = Probe(argparse.Namespace(template=Path("x"), missions_dir=Path("x"), cold=False))
            hooks, mission = FakeConn(Src.HOOKS), FakeConn(Src.MISSION)
            hello = mock.Mock()
            probe.on_hello(hooks, hello, True)
            probe.on_hello(mission, hello, True)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertIs(probe.hooks, hooks)
            self.assertIs(probe.mission, mission)
            self.assertIn("menu", mission.calls)
            self.assertEqual(hooks.calls, [])
        asyncio.run(run())

    def test_parked_wingman_starts_when_the_lead_taxis(self):
        class Conn(FakeConn):
            def __init__(self):
                super().__init__(Src.MISSION)
                self.speeds = [0.0, 0.5, 3.0]
                self.sent = []

            async def call(self, op, data, **kw):
                self.sent.append((op, data))
                what = data.get("what")
                if op == "query" and what == "group":
                    spd = self.speeds.pop(0) if self.speeds else 3.0
                    return {"exists": True, "id": 4, "units": [{"pos": {"x": -216000.0, "y": 52700.0},
                                                                "alt": 708.0, "hdg": 90.0, "spd": spd}]}
                if op == "query" and what == "airbase":
                    return {"id": 18, "pos": {"x": -216085.3, "y": 52697.5}, "alt": 708.0}
                if op == "query" and what == "parking":
                    return {"spots": [{"term": 24, "type": 104, "pos": {"x": -216100.0, "y": 52700.0}}]}
                if op == "query" and what == "type_size":
                    return {"span": 11.8, "length": 17.1, "height": 5.5}
                return {}

        async def run():
            probe = Probe(argparse.Namespace(template=Path("x"), missions_dir=Path("x"), cold=False))
            conn = Conn()
            with mock.patch("tools.wingman_probe.WATCH_S", 0):
                await probe.wingman(conn, "parked_follow")
                for _ in range(20):
                    await asyncio.sleep(0)
            spawn = next(d for op, d in conn.sent if op == "spawn_group")
            group = spawn["group"]
            self.assertTrue(group["uncontrolled"])
            self.assertEqual(group["route"]["points"][0]["type"], "TakeOffParking")
            self.assertEqual(group["route"]["points"][0]["task"]["params"]["tasks"][0]["id"], "Follow")
            starts = [d for op, d in conn.sent if op == "set_command"]
            self.assertEqual(starts, [{"group": "P20 Wingman", "command": {"id": "Start", "params": {}}}])
            self.assertEqual(sum(1 for op, d in conn.sent if op == "query" and d.get("what") == "group"), 3)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
