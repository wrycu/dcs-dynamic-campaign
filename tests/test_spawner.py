import unittest

from campaign.bubble import BubbleConfig
from campaign.bridge import Disconnected
from campaign.model import CampaignState, SpawnState
from campaign.protocol import Hello
from campaign.spawner import GroundSpawner
from tests.test_bubble import gu

CFG = BubbleConfig(ground_spawn=10_000, ground_despawn=12_500, threat_spawn_margin=5_000,
                  threat_despawn_margin=8_000, min_dwell=60, max_units=50, lookahead_s=0)


class FakeDCS:
    """Plays the mission bridge's spawn_group / despawn_group."""

    def __init__(self):
        self.groups: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.fail: Exception | None = None
        self.clear = None   # clear_spots: points -> spots; None: every point is clear

    async def call(self, op, data):
        self.calls.append((op, data))
        if self.fail:
            raise self.fail
        if op == "spawn_group":
            name = data["group"]["name"]
            if name in self.groups:
                raise RuntimeError(f"group {name} already exists")
            self.groups[name] = [dict(u) for u in data["group"]["units"]]
            return {"group": name, "units": [u["name"] for u in data["group"]["units"]]}
        if op == "despawn_group":
            units = self.groups.pop(data["group"], None)
            if units is None:
                return {"survivors": {}, "gone": True}
            survivors = {}
            for u in units:
                survivors[u["type"]] = survivors.get(u["type"], 0) + 1
            return {"survivors": survivors or {}, "alive": [u["name"] for u in units],
                    "pos": {"x": units[0]["x"], "y": units[0]["y"]} if units else None}
        if op == "query" and data["what"] == "clear_spots":
            pts = data["points"]
            return {"spots": self.clear(pts) if self.clear else list(pts)}
        raise AssertionError(op)

    def kill(self, group, index):
        """Kill a unit in DCS and return the unit_killed event the bridge would send."""
        name = f"{group}#{index}"
        unit = next(u for u in self.groups[group] if u["name"] == name)
        self.groups[group].remove(unit)
        if not self.groups[group]:
            del self.groups[group]
        return {"kind": "unit_killed", "unit": {"name": name, "type": unit["type"]}}

    def telemetry(self, players):
        return {"players": [{"pos": {"x": x, "y": y}} for x, y in players],
                "groups": [{"id": n, "pos": {"x": u[0]["x"], "y": u[0]["y"]} if u else None, "alive": len(u)}
                           for n, u in self.groups.items()] or {}}


class SpawnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0.0
        self.dcs = FakeDCS()
        self.state = CampaignState(theater_map="Syria", clock=0, sides={})
        self.state.ground_units["a"] = gu("a", 5_000, comp={"T-72B": 2, "BMP-2": 2})
        self.state.ground_units["b"] = gu("b", 50_000)
        self.changes = []
        self.sp = GroundSpawner(self.state, self.dcs.call, CFG, clock=lambda: self.now,
                                on_change=lambda u: self.changes.append((u.id, u.spawn)))

    async def step(self, *players):
        self.sp.on_telemetry(self.dcs.telemetry(players))
        return await self.sp.tick()

    async def test_spawn_then_despawn_when_player_leaves(self):
        await self.step((0, 0))
        a = self.state.ground_units["a"]
        self.assertIs(a.spawn, SpawnState.SPAWNED)
        self.assertEqual(a.dcs_group, "gu:a")
        self.assertEqual(set(self.dcs.groups), {"gu:a"})
        self.now = 100
        await self.step((-100_000, 0))
        self.assertIs(a.spawn, SpawnState.AGGREGATED)
        self.assertEqual(self.dcs.groups, {})
        self.assertEqual(a.composition, {"T-72B": 2, "BMP-2": 2})
        self.assertEqual(self.changes, [("a", SpawnState.SPAWNED), ("a", SpawnState.AGGREGATED)])

    async def test_kills_persist_and_are_not_double_counted(self):
        await self.step((0, 0))
        ev = self.dcs.kill("gu:a", 1)
        self.sp.on_event(ev)
        self.sp.on_event(ev)  # replayed duplicate
        a = self.state.ground_units["a"]
        self.assertEqual(sum(a.composition.values()), 3)
        late = self.dcs.kill("gu:a", 2)
        self.now = 1000  # past dwell and the combat pin
        await self.step((-100_000, 0))
        self.assertEqual(sum(a.composition.values()), 2)  # survivors are authoritative
        self.sp.on_event(late)  # arrived after the despawn ack: already counted
        self.assertEqual(sum(a.composition.values()), 2)
        await self.step((0, 0))
        self.assertEqual(len(self.dcs.groups["gu:a"]), 2)

    async def test_combat_pins_formation(self):
        await self.step((0, 0))
        self.sp.on_event({"kind": "kill", "killer": {"name": "gu:a#1"}, "victim": {"name": "Player"}})
        self.now = 100
        await self.step((-100_000, 0))
        self.assertIs(self.state.ground_units["a"].spawn, SpawnState.SPAWNED)

    async def test_destroyed_formation(self):
        await self.step((0, 0))
        for i in range(1, 5):
            self.sp.on_event(self.dcs.kill("gu:a", i))
        a = self.state.ground_units["a"]
        self.assertEqual(a.composition, {})
        self.assertIs(a.spawn, SpawnState.AGGREGATED)
        self.now = 1000
        await self.step((0, 0))
        self.assertEqual(self.dcs.groups, {})

    async def test_new_run_aggregates_and_respawns(self):
        await self.step((0, 0))
        self.dcs.groups.clear()  # mission restarted
        self.sp.on_hello(Hello(run_id="r2", spawned=[]), new_run=True)
        self.assertIs(self.state.ground_units["a"].spawn, SpawnState.AGGREGATED)
        await self.step((0, 0))
        self.assertIn("gu:a", self.dcs.groups)

    async def test_reconnect_reconciles(self):
        await self.step((0, 0))
        self.dcs.groups["gu:stray"] = [{"name": "gu:stray#1", "type": "T-72B", "x": 0, "y": 0}]
        self.dcs.groups.pop("gu:a")  # destroyed while disconnected
        self.sp.on_hello(Hello(run_id="r1", spawned=["gu:stray", "Aerial-1"]), new_run=False)
        self.assertIs(self.state.ground_units["a"].spawn, SpawnState.AGGREGATED)
        self.assertEqual(self.sp.orphans, {"gu:stray"})
        await self.step((0, 0))
        self.assertNotIn("gu:stray", self.dcs.groups)
        self.assertEqual(self.sp.orphans, set())

    async def test_vanished_group_is_aggregated(self):
        await self.step((0, 0))
        self.dcs.groups.clear()
        self.sp.on_telemetry(self.dcs.telemetry([(0, 0)]))
        self.assertIs(self.state.ground_units["a"].spawn, SpawnState.SPAWNED)  # one miss is tolerated
        self.sp.on_telemetry(self.dcs.telemetry([(0, 0)]))
        self.assertIs(self.state.ground_units["a"].spawn, SpawnState.AGGREGATED)

    async def test_failed_spawn_backs_off_and_adopts_existing_group(self):
        self.dcs.groups["gu:a"] = []
        await self.step((0, 0))
        self.assertIs(self.state.ground_units["a"].spawn, SpawnState.AGGREGATED)
        self.assertEqual(self.sp.orphans, {"gu:a"})
        await self.step((0, 0))  # orphan removed; spawn still backing off
        self.assertNotIn("gu:a", self.dcs.groups)
        self.now = 61
        await self.step((0, 0))
        self.assertIn("gu:a", self.dcs.groups)

    async def test_disconnect_during_despawn_keeps_it_spawned(self):
        await self.step((0, 0))
        self.now = 100
        self.dcs.fail = Disconnected("gone")
        await self.step((-100_000, 0))
        self.assertIs(self.state.ground_units["a"].spawn, SpawnState.SPAWNED)
        self.assertEqual(self.sp.busy, set())

    async def test_positions_follow_telemetry(self):
        await self.step((0, 0))
        self.dcs.groups["gu:a"][0].update(x=6_000, y=1_000)
        self.sp.on_telemetry(self.dcs.telemetry([(0, 0)]))
        a = self.state.ground_units["a"]
        self.assertEqual((a.pos.x, a.pos.y), (6_000, 1_000))

    async def test_sited_once_before_first_spawn(self):
        self.dcs.clear = lambda pts: [{"x": p["x"] + 100, "y": p["y"]} for p in pts]
        await self.step((0, 0))
        a = self.state.ground_units["a"]
        self.assertTrue(a.sited)
        self.assertEqual(len(a.spots), 4)
        q = [d for op, d in self.dcs.calls if op == "query"]
        self.assertEqual(len(q), 1)
        want = q[0]["points"]
        got = [(u["x"], u["y"]) for u in self.dcs.groups["gu:a"]]
        self.assertEqual(got, [(round(p["x"] + 100, 1), round(p["y"], 1)) for p in want])
        # Despawn and respawn on the same spots, without asking again.
        self.now = 100
        await self.step((-100_000, 0))
        self.now = 200
        await self.step((0, 0))
        self.assertEqual(len([1 for op, _ in self.dcs.calls if op == "query"]), 1)
        self.assertEqual([(u["x"], u["y"]) for u in self.dcs.groups["gu:a"]], got)

    async def test_small_drift_keeps_the_spots_a_move_resites(self):
        self.dcs.clear = lambda pts: [{"x": p["x"] + 300, "y": p["y"]} for p in pts]
        await self.step((0, 0))
        a = self.state.ground_units["a"]
        lead = self.dcs.groups["gu:a"][0]
        self.sp.on_telemetry(self.dcs.telemetry([(0, 0)]))   # the lead vehicle, on its spot
        self.assertTrue(a.sited)
        lead.update(x=lead["x"] + 30)
        self.sp.on_telemetry(self.dcs.telemetry([(0, 0)]))
        self.assertTrue(a.sited)
        lead.update(x=9_000, y=1_000)
        self.sp.on_telemetry(self.dcs.telemetry([(0, 0)]))
        self.assertFalse(a.sited)
        self.assertEqual((a.pos.x, a.pos.y), (9_000, 1_000))
        self.assertEqual(a.spots, [])

    async def test_vehicles_without_clear_ground_keep_their_layout_spot(self):
        self.dcs.clear = lambda pts: [None] + [{"x": p["x"], "y": p["y"] + 50} for p in pts[1:]]
        await self.step((0, 0))
        a = self.state.ground_units["a"]
        self.assertIs(a.spawn, SpawnState.SPAWNED)
        want = [d for op, d in self.dcs.calls if op == "query"][0]["points"]
        self.assertEqual((a.spots[0].x, a.spots[0].y), (want[0]["x"], want[0]["y"]))
        self.assertEqual(a.spots[1].y, want[1]["y"] + 50)

    async def test_losses_use_the_first_spots(self):
        await self.step((0, 0))
        a = self.state.ground_units["a"]
        self.sp.on_event(self.dcs.kill("gu:a", 4))
        self.now = 100
        await self.step((-100_000, 0))
        self.now = 200
        await self.step((0, 0))
        self.assertEqual([(u["x"], u["y"]) for u in self.dcs.groups["gu:a"]],
                         [(round(p.x, 1), round(p.y, 1)) for p in a.spots[:3]])

if __name__ == "__main__":
    unittest.main()
