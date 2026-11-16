import math
import unittest

from campaign.bubble import (BubbleConfig, Observer, despawn_radius, observer_from_telemetry, plan_ground,
                             spawn_radius)
from campaign.ground_spawn import build_ground_spawn
from campaign.model import GroundOrder, GroundRole, GroundUnit, Pos, Side, SpawnState
from campaign.protocol import parse_dcs_name

CFG = BubbleConfig(ground_spawn=10_000, ground_despawn=15_000, threat_spawn_margin=5_000,
                   threat_despawn_margin=8_000, min_dwell=60, max_units=20, max_spawns_per_tick=10, lookahead_s=0)


def gu(uid, x, y=0.0, comp=None, role=GroundRole.ARMOR, spawned=False, side=Side.RED):
    return GroundUnit(id=uid, side=side, name=uid, role=role, composition={"T-72B": 4} if comp is None else comp,
                      pos=Pos(x, y), location_id=None, order=GroundOrder.HOLD,
                      spawn=SpawnState.SPAWNED if spawned else SpawnState.AGGREGATED,
                      dcs_group=f"gu:{uid}" if spawned else None)


def at(x, y=0.0):
    return [Observer(Pos(x, y))]


class PlanTests(unittest.TestCase):
    def test_spawns_inside_radius_only(self):
        units = [gu("in", 9_000), gu("out", 11_000)]
        self.assertEqual(plan_ground(units, at(0), 0, CFG).spawn, ["in"])

    def test_hysteresis(self):
        u = gu("a", 12_000, spawned=True)  # beyond spawn distance, inside despawn distance
        self.assertEqual(plan_ground([u], at(0), 1000, CFG, spawned_at={"a": 0}).despawn, [])
        u.pos = Pos(16_000, 0)
        self.assertEqual(plan_ground([u], at(0), 1000, CFG, spawned_at={"a": 0}).despawn, ["a"])

    def test_dwell_and_pin_keep_spawned(self):
        u = gu("a", 50_000, spawned=True)
        self.assertEqual(plan_ground([u], [], 30, CFG, spawned_at={"a": 0}).despawn, [])
        self.assertEqual(plan_ground([u], [], 100, CFG, spawned_at={"a": 0}, pinned={"a"}).despawn, [])
        self.assertEqual(plan_ground([u], [], 100, CFG, spawned_at={"a": 0}).despawn, ["a"])

    def test_no_players_despawns_everything(self):
        units = [gu("a", 0, spawned=True), gu("b", 0)]
        plan = plan_ground(units, [], 100, CFG, spawned_at={"a": 0})
        self.assertEqual((plan.spawn, plan.despawn), ([], ["a"]))

    def test_sam_radius_covers_its_envelope(self):
        sam = gu("sa6", 0, comp={"Kub 2P25 ln": 3, "Kub 1S91 str": 1}, role=GroundRole.SAM)
        self.assertEqual(spawn_radius(sam, CFG), 22_000 + 5_000)
        self.assertEqual(spawn_radius(gu("t", 0), CFG), 10_000)
        self.assertEqual(plan_ground([sam], at(25_000), 0, CFG).spawn, ["sa6"])
        self.assertEqual(despawn_radius(sam, CFG), 22_000 + 8_000)
        self.assertEqual(despawn_radius(gu("t", 0), CFG), 15_000)
        sam.spawn = SpawnState.SPAWNED
        self.assertEqual(plan_ground([sam], at(29_000), 1000, CFG, spawned_at={"sa6": 0}).despawn, [])
        self.assertEqual(plan_ground([sam], at(31_000), 1000, CFG, spawned_at={"sa6": 0}).despawn, ["sa6"])

    def test_config_rejects_despawn_inside_spawn(self):
        with self.assertRaises(ValueError):
            BubbleConfig(ground_spawn=10_000, ground_despawn=9_000)
        with self.assertRaises(ValueError):
            BubbleConfig(threat_spawn_margin=5_000, threat_despawn_margin=4_000)

    def test_budget_prefers_closest_and_skips_to_what_fits(self):
        units = [gu("far", 9_000, comp={"T-72B": 4}), gu("near", 1_000, comp={"T-72B": 12}),
                 gu("mid", 5_000, comp={"T-72B": 6}), gu("small", 8_000, comp={"T-72B": 2})]
        # 12 + 6 = 18; "far" (4) no longer fits under 20, "small" (2) does.
        self.assertEqual(plan_ground(units, at(0), 0, CFG).spawn, ["near", "mid", "small"])

    def test_spawn_rate_limit(self):
        cfg = BubbleConfig(ground_spawn=10_000, ground_despawn=10_000, max_spawns_per_tick=2, max_units=100, lookahead_s=0)
        units = [gu(str(i), 1_000 * i) for i in range(5)]
        self.assertEqual(plan_ground(units, at(0), 0, cfg).spawn, ["0", "1"])

    def test_over_budget_sheds_least_relevant_unpinned(self):
        units = [gu("a", 1_000, comp={"T-72B": 10}, spawned=True),
                 gu("b", 8_000, comp={"T-72B": 10}, spawned=True),
                 gu("c", 9_000, comp={"T-72B": 10}, spawned=True)]
        spawned_at = {"a": 0, "b": 0, "c": 0}
        self.assertEqual(plan_ground(units, at(0), 100, CFG, spawned_at=spawned_at).despawn, ["c"])
        self.assertEqual(plan_ground(units, at(0), 100, CFG, spawned_at=spawned_at, pinned={"c"}).despawn,
                         ["b"])

    def test_busy_blocked_and_destroyed(self):
        units = [gu("busy", 0), gu("blocked", 0), gu("dead", 0, comp={}, spawned=True)]
        plan = plan_ground(units, at(0), 0, CFG, busy={"busy"}, blocked={"blocked"})
        self.assertEqual((plan.spawn, plan.despawn), ([], ["dead"]))

    def test_busy_units_hold_budget(self):
        units = [gu("busy", 0, comp={"T-72B": 18}), gu("next", 0, comp={"T-72B": 4})]
        self.assertEqual(plan_ground(units, at(0), 0, CFG, busy={"busy"}).spawn, [])

    def test_lookahead(self):
        # Heading east (90 deg) at 200 m/s for 60 s: 12 km ahead.
        o = observer_from_telemetry({"pos": {"x": 0, "y": 0}, "hdg": 90, "spd": 200}, 60)
        self.assertAlmostEqual(o.ahead.x, 0, places=6)
        self.assertAlmostEqual(o.ahead.y, 12_000, places=6)
        self.assertEqual(plan_ground([gu("east", 0, 21_000)], [o], 0, CFG).spawn, ["east"])
        self.assertIsNone(observer_from_telemetry({"pos": {"x": 1, "y": 2}, "spd": 0.2, "hdg": 0}, 60).ahead)
        self.assertIsNone(observer_from_telemetry({"name": "no pos"}, 60))


class GroundSpawnTests(unittest.TestCase):
    def test_group_table(self):
        u = gu("2f9a", 1000, 2000, comp={"BMP-2": 3, "T-72B": 2})
        s = build_ground_spawn(u)
        g = s.data["group"]
        self.assertEqual(g["name"], "gu:2f9a")
        self.assertEqual(s.data["category"], "ground")
        self.assertEqual(s.data["country"], 81)
        self.assertEqual(len(g["units"]), 5)
        self.assertEqual(sorted(s.unit_types.values()), ["BMP-2"] * 3 + ["T-72B"] * 2)
        for i, unit in enumerate(g["units"], start=1):
            self.assertEqual(parse_dcs_name(unit["name"]).unit_index, i)
            self.assertEqual(s.unit_types[unit["name"]], unit["type"])
            self.assertLess(math.hypot(unit["x"] - 1000, unit["y"] - 2000), 200)
            self.assertTrue(0 <= unit["heading"] < 2 * math.pi)
        self.assertEqual(build_ground_spawn(u).data, s.data)  # deterministic

    def test_sam_layout(self):
        u = gu("sa6", 0, 0, comp={"Kub 1S91 str": 1, "Kub 2P25 ln": 3}, role=GroundRole.SAM)
        units = {x["type"]: x for x in build_ground_spawn(u).data["group"]["units"]}
        radar = units["Kub 1S91 str"]
        self.assertEqual((radar["x"], radar["y"]), (0, 0))
        for x in build_ground_spawn(u).data["group"]["units"]:
            if x["type"] == "Kub 2P25 ln":
                self.assertAlmostEqual(math.hypot(x["x"], x["y"]), 150, delta=1)

    def test_empty_formation_rejected(self):
        with self.assertRaises(ValueError):
            build_ground_spawn(gu("x", 0, comp={"T-72B": 0}))


if __name__ == "__main__":
    unittest.main()
