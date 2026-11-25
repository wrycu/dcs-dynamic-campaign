import asyncio
import unittest

from campaign.ground_war import (TOWN_EDGE_M, MARCH_MS, STANDOFF_M, GroundWar, SideOrders, garrison_for,
                                 garrison_unit, occupiers)
from campaign.model import (CampaignState, GroundOrder, GroundRole, GroundUnit, Link, ObjectiveDef, ObjectiveState,
                            ObjectiveType, Pos, Side, SpawnState, Squadron, Theater)
from campaign.persist import Snapshot, decode, encode

KM = 1000.0


def obj(oid, x, y=0.0, t=ObjectiveType.CITY, prio=2, country="Jordan", area_of=None):
    return ObjectiveDef(oid, oid.title(), t, Pos(x, y), [], prio, area_of=area_of, country=country)


def unit(uid, side, at, x, comp=None, role=GroundRole.ARMOR, order=GroundOrder.DEFEND):
    comp = comp if comp is not None else ({"M-1 Abrams": 10} if side is Side.BLUE else {"T-72B": 10})
    return GroundUnit(uid, side, uid, role, dict(comp), Pos(x, 0), at, order)


class World:
    """A line of objectives: home (blue) - mid (blue) - front (red) - deep (red), 20 km apart."""

    def __init__(self, garrisons=False):
        objs = [obj("home", 0), obj("mid", 20 * KM), obj("front", 40 * KM, country="Syria"),
                obj("deep", 60 * KM, t=ObjectiveType.AIRBASE, country="Syria"),
                obj("plant", 41 * KM, 500, t=ObjectiveType.POWER_PLANT, country="Syria", area_of="front"),
                obj("rear_ab", 80 * KM, t=ObjectiveType.AIRBASE, country="Syria")]
        self.theater = Theater("Syria", {o.id: o for o in objs}, [
            Link("home", "mid", "road", length_m=20 * KM), Link("mid", "front", "road", length_m=20 * KM),
            Link("front", "deep", "road", length_m=20 * KM), Link("deep", "rear_ab", "road", length_m=20 * KM)])
        owners = {"home": Side.BLUE, "mid": Side.BLUE}
        self.state = CampaignState("Syria", 0.0, {})
        for o in objs:
            side = owners.get(o.id, Side.RED)
            self.state.objectives[o.id] = ObjectiveState(o.id, side)
            g = garrison_for(o, side) if garrisons and o.area_of is None else {}
            if g:
                garrison_unit(self.state, o, side, g)
        self.news: list[str] = []
        self.calls: list[tuple[str, dict]] = []

    def war(self, blue="attack", red="defend", **kw):
        async def call(op, data):
            self.calls.append((op, data))
            return {}
        return GroundWar(self.state, self.theater, {Side.BLUE: SideOrders(blue, "deep", "Coalition"),
                                                    Side.RED: SideOrders(red, None, "Syria", frozenset({"Syria"}))},
                         notify=self.news.append, call=call, **kw)

    def run(self, war, hours, step=60.0):
        for _ in range(int(hours * 3600 / step)):
            t0 = self.state.clock
            self.state.clock += step
            war.advance(t0, self.state.clock)


class MovementTests(unittest.TestCase):
    def test_march_along_a_link_to_an_own_objective(self):
        w = World()
        u = unit("b1", Side.BLUE, "home", 0)
        w.state.ground_units["b1"] = u
        war = w.war(blue="defend")
        war.order(u, ["home", "mid"], GroundOrder.MOVE)
        w.run(war, 0.5)
        self.assertIsNone(u.location_id)
        self.assertAlmostEqual(u.pos.x, MARCH_MS * 1800, delta=1)
        w.run(war, 0.5)
        self.assertEqual(u.location_id, "mid")
        self.assertEqual(u.route, [])
        self.assertLess(abs(u.pos.x - 20 * KM), 3 * KM)   # spread out around it

    def test_spawned_progress_comes_from_its_position(self):
        w = World()
        u = unit("b1", Side.BLUE, "home", 0)
        w.state.ground_units["b1"] = u
        war = w.war(blue="defend")
        war.order(u, ["home", "mid"], GroundOrder.MOVE)
        u.spawn, u.dcs_group = SpawnState.SPAWNED, "gu:b1"
        u.pos = Pos(15 * KM, 200)   # DCS drove it fast
        w.run(war, 1 / 60)
        self.assertAlmostEqual(u.leg_m, 15 * KM, delta=1)
        self.assertEqual(u.pos, Pos(15 * KM, 200))   # left where DCS has it

    def test_spawned_moving_formation_gets_its_leg_as_a_route_once(self):
        w = World()
        u = unit("b1", Side.BLUE, "home", 0)
        w.state.ground_units["b1"] = u
        war = w.war(blue="defend")
        war.order(u, ["home", "mid", "front"], GroundOrder.ATTACK)
        u.spawn, u.dcs_group = SpawnState.SPAWNED, "gu:b1"
        asyncio.run(war.tick())
        asyncio.run(war.tick())
        routes = [d for op, d in w.calls if op == "set_route"]
        self.assertEqual(len(routes), 1)
        end = routes[0]["points"][-1]
        self.assertAlmostEqual(end["x"], 20 * KM, delta=1)
        self.assertEqual(routes[0]["points"][0]["action"], "On Road")


class BattleTests(unittest.TestCase):
    def test_empty_objective_is_taken_with_its_area_and_air_defence_overrun(self):
        w = World()
        b = unit("b1", Side.BLUE, "mid", 20 * KM)
        sam = unit("sam", Side.RED, "front", 40 * KM, {"Kub 2P25 ln": 4}, GroundRole.SAM)
        w.state.ground_units.update(b1=b, sam=sam)
        war = w.war(blue="defend")
        war.order(b, ["mid", "front"], GroundOrder.ATTACK)
        w.run(war, 1)
        self.assertIs(w.state.objectives["front"].owner, Side.BLUE)
        self.assertIs(w.state.objectives["plant"].owner, Side.BLUE)
        self.assertEqual(sam.composition, {})
        self.assertEqual(b.location_id, "front")
        self.assertEqual(war.garrison("front").composition, occupiers(Side.BLUE))
        self.assertTrue(any("takes Front (and 1 site in its area)" in n for n in w.news), w.news)

    def test_attackers_stop_short_and_fight_the_garrison(self):
        w = World(garrisons=True)
        b = unit("b1", Side.BLUE, "mid", 20 * KM, {"M-1 Abrams": 10, "M-2 Bradley": 4})
        w.state.ground_units["b1"] = b
        war = w.war(blue="defend")
        war.order(b, ["mid", "front"], GroundOrder.ATTACK)
        w.run(war, (20 * KM - STANDOFF_M) / MARCH_MS / 3600 + 0.05)
        self.assertEqual(b.battle, "front")
        self.assertIn("front", w.state.battles)
        self.assertAlmostEqual(b.pos.x, 40 * KM - STANDOFF_M, delta=5)
        w.run(war, 12)
        self.assertIs(w.state.objectives["front"].owner, Side.BLUE)
        self.assertNotIn("front", w.state.battles)

    def test_weak_attack_breaks_off_back_where_it_came_from(self):
        w = World()
        b = unit("b1", Side.BLUE, "mid", 20 * KM, {"M-2 Bradley": 4})
        reds = [unit(f"r{i}", Side.RED, "front", 40 * KM) for i in range(3)]
        w.state.ground_units.update({u.id: u for u in [b] + reds})
        war = w.war(blue="defend", red="defend")
        war.order(b, ["mid", "front"], GroundOrder.ATTACK)
        w.run(war, 14)
        self.assertIs(w.state.objectives["front"].owner, Side.RED)
        self.assertNotIn("front", w.state.battles)
        if sum(b.composition.values()):
            self.assertIn(b.location_id, ("mid", None))
            self.assertNotEqual(b.battle, "front")

    def test_formation_in_dcs_takes_no_abstract_losses(self):
        w = World(garrisons=True)
        b = unit("b1", Side.BLUE, "mid", 20 * KM, {"M-2 Bradley": 10})
        r = unit("r1", Side.RED, "front", 40 * KM)
        w.state.ground_units.update(b1=b, r1=r)
        war = w.war(blue="defend", red="defend")
        war.order(b, ["mid", "front"], GroundOrder.ATTACK)
        w.run(war, (20 * KM - STANDOFF_M) / MARCH_MS / 3600 + 0.05)
        r.spawn, r.dcs_group = SpawnState.SPAWNED, "gu:r1"
        w.run(war, 3)
        self.assertEqual(r.composition, {"T-72B": 10})
        g = war.garrison("front")
        self.assertLess(sum(g.composition.values()), g.full)

    def test_defence_killed_in_dcs_is_taken_at_once(self):
        w = World(garrisons=True)
        b = unit("b1", Side.BLUE, "mid", 20 * KM)
        w.state.ground_units["b1"] = b
        war = w.war(blue="defend", red="defend")
        war.order(b, ["mid", "front"], GroundOrder.ATTACK)
        w.run(war, (20 * KM - STANDOFF_M) / MARCH_MS / 3600 + 0.05)
        self.assertIn("front", w.state.battles)
        war.garrison("front").composition = {}     # DCS killed the last of them
        w.run(war, 6 / 60)                         # well before the next hour
        self.assertIs(w.state.objectives["front"].owner, Side.BLUE)
        self.assertEqual(b.location_id, "front")

    def test_captured_airbase_squadron_moves_back(self):
        w = World()
        w.state.squadrons["mig"] = Squadron("mig", Side.RED, "MiGs", "MiG-21Bis", "deep", {"CAP"}, 8, 0, 8, False)
        b = unit("b1", Side.BLUE, "front", 40 * KM)
        w.state.objectives["front"].owner = Side.BLUE
        w.state.ground_units["b1"] = b
        war = w.war(blue="defend")
        war.order(b, ["front", "deep"], GroundOrder.ATTACK)
        w.run(war, 1)
        self.assertIs(w.state.objectives["deep"].owner, Side.BLUE)
        self.assertEqual(w.state.squadrons["mig"].base_id, "rear_ab")


class CommanderTests(unittest.TestCase):
    def test_attacker_attacks_at_two_to_one_and_not_below(self):
        w = World(garrisons=True)
        b = unit("b1", Side.BLUE, "mid", 20 * KM)
        w.state.ground_units["b1"] = b
        war = w.war()
        war.command()
        self.assertEqual(b.route, ["front"])
        w2 = World(garrisons=True)
        weak = unit("b1", Side.BLUE, "mid", 20 * KM, {"Soldier M4": 2})
        w2.state.ground_units["b1"] = weak
        w2.war().command()
        self.assertEqual(weak.route, [])

    def test_hold_and_air_defence_never_move(self):
        w = World()
        held = unit("b1", Side.BLUE, "mid", 20 * KM, order=GroundOrder.HOLD)
        sam = unit("b2", Side.BLUE, "mid", 20 * KM, {"Hawk ln": 3}, GroundRole.SAM)
        w.state.ground_units.update(b1=held, b2=sam)
        w.war().command()
        self.assertEqual((held.route, sam.route), ([], []))

    def test_defender_retakes_its_own_ground_only(self):
        w = World()
        w.state.objectives["front"].owner = Side.BLUE          # Syrian ground the coalition holds
        reds = [unit(f"r{i}", Side.RED, "deep", 60 * KM) for i in range(2)]
        w.state.ground_units.update({u.id: u for u in reds})
        war = w.war(blue="defend")
        war.command()
        self.assertTrue(any(u.route == ["front"] for u in reds))
        # Jordan is not its to take.
        w2 = World()
        r = unit("r1", Side.RED, "front", 40 * KM)
        r2 = unit("r2", Side.RED, "front", 40 * KM)
        w2.state.ground_units.update(r1=r, r2=r2)
        w2.war(blue="defend").command()
        self.assertEqual((r.route, r2.route), ([], []))


class DefenceTests(unittest.TestCase):
    def test_defenders_deploy_on_the_edge_facing_the_threat(self):
        w = World()
        b = unit("b1", Side.BLUE, "mid", 20 * KM)
        reds = [unit(f"r{i}", Side.RED, "front", 40 * KM) for i in range(2)]
        w.state.ground_units.update({u.id: u for u in [b] + reds})
        war = w.war(blue="defend", red="defend")
        w.run(war, 0.1)
        for r in reds:   # 1 km out from Front, on the road towards Mid (x smaller), side by side
            self.assertAlmostEqual(r.pos.x, 40 * KM - TOWN_EDGE_M[2], delta=1)
            self.assertEqual(r.pos, r.post)
            self.assertEqual(r.location_id, "front")
        self.assertGreater(abs(reds[0].pos.y - reds[1].pos.y), 300)
        self.assertEqual(b.post, Pos(20 * KM + TOWN_EDGE_M[2], 0))   # and Mid's faces them
        w0 = World()
        alone = unit("b1", Side.BLUE, "home", 0)
        w0.state.ground_units["b1"] = alone
        w0.run(w0.war(blue="defend"), 0.1)
        self.assertIsNone(alone.post)   # no enemy next to Home: stays put
        # Formations in DCS drive there across country.
        w2 = World()
        r = unit("r1", Side.RED, "front", 40 * KM)
        r.spawn, r.dcs_group = SpawnState.SPAWNED, "gu:r1"
        w2.state.ground_units.update(b1=unit("b1", Side.BLUE, "mid", 20 * KM), r1=r)
        war2 = w2.war(blue="defend", red="defend")
        w2.run(war2, 0.1)
        asyncio.run(war2.tick())
        routes = [d for op, d in w2.calls if op == "set_route" and d["group"] == "gu:r1"]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["points"][-1]["action"], "Off Road")
        self.assertAlmostEqual(routes[0]["points"][-1]["x"], 40 * KM - TOWN_EDGE_M[2], delta=1)

    def test_nearest_reserves_go_and_only_enough(self):
        w = World()
        w.state.ground_units["b1"] = unit("b1", Side.BLUE, "mid", 20 * KM)   # 150 power next to Front
        reds = [unit("r_deep1", Side.RED, "deep", 60 * KM), unit("r_deep2", Side.RED, "deep", 60 * KM),
                unit("r_rear", Side.RED, "rear_ab", 80 * KM)]
        w.state.ground_units.update({u.id: u for u in reds})
        w.war(blue="defend", red="defend").command()
        # Each battalion is 50 power, 112 in town: two of them, the nearest.
        self.assertEqual([u.route for u in reds], [["front"], ["front"], []])

    def test_reserves_too_far_by_road_stay(self):
        import campaign.ground_war as gw
        w = World()
        w.state.ground_units["b1"] = unit("b1", Side.BLUE, "mid", 20 * KM, {"M-1 Abrams": 60})
        far = unit("r_rear", Side.RED, "rear_ab", 80 * KM)
        w.state.ground_units["r_rear"] = far
        old = gw.REINFORCE_MAX_M
        gw.REINFORCE_MAX_M = 30 * KM
        try:
            w.war(blue="defend", red="defend").command()
        finally:
            gw.REINFORCE_MAX_M = old
        self.assertEqual(far.route, [])

    def test_garrison_in_dcs_takes_only_dcs_losses_and_battles_report_hourly(self):
        w = World(garrisons=True)
        b = unit("b1", Side.BLUE, "mid", 20 * KM, {"M-2 Bradley": 10})
        w.state.ground_units["b1"] = b
        war = w.war(blue="defend", red="defend")
        g = war.garrison("front")
        g.spawn, g.dcs_group = SpawnState.SPAWNED, "gu:" + g.id
        full = dict(g.composition)
        war.order(b, ["mid", "front"], GroundOrder.ATTACK)
        w.run(war, (20 * KM - STANDOFF_M) / MARCH_MS / 3600 + 0.05)
        self.assertEqual(b.battle, "front")
        w.run(war, 2)
        self.assertEqual(g.composition, full)
        self.assertTrue(any(n.startswith("Front, hour 1: Coalition attackers at") for n in w.news), w.news)
        self.assertIn("defenders", war.status_lines()[0])


class SaveTests(unittest.TestCase):
    def test_battle_state_survives_a_save(self):
        w = World(garrisons=True)
        b = unit("b1", Side.BLUE, "mid", 20 * KM)
        w.state.ground_units["b1"] = b
        war = w.war(blue="defend")
        war.order(b, ["mid", "front"], GroundOrder.ATTACK)
        w.run(war, 1)
        raw = encode(Snapshot(w.state).state)
        st = decode(CampaignState, raw, "state")
        self.assertEqual(st.ground_units["b1"].route, b.route)
        self.assertEqual(st.ground_units["b1"].leg_from, "mid")
        self.assertEqual(st.ground_units["gar_front_1"].composition, w.state.ground_units["gar_front_1"].composition)
        w.run(war, 0.5)
        self.assertIn("front", w.state.battles)
        st = decode(CampaignState, encode(w.state), "state")
        self.assertIs(st.battles["front"].attacker, Side.BLUE)



class GarrisonLayoutTests(unittest.TestCase):
    def test_garrison_stands_round_the_edge_of_its_town(self):
        import math
        from campaign.ground_spawn import build_ground_spawn
        w = World(garrisons=True)
        g = w.war().garrison("front")          # a priority-2 town: 20 infantry, 2 APCs, 1 gun
        self.assertEqual(g.ring_m, TOWN_EDGE_M[2])
        units = build_ground_spawn(g).data["group"]["units"]
        c = w.theater.objectives["front"].pos
        dist = [math.hypot(u["x"] - c.x, u["y"] - c.y) for u in units]
        self.assertTrue(all(TOWN_EDGE_M[2] - 50 < d < TOWN_EDGE_M[2] + 50 for d in dist), dist)
        bearings = {round(math.degrees(math.atan2(u["y"] - c.y, u["x"] - c.x)) / 10) for u in units}
        self.assertGreaterEqual(len(bearings), 3)             # several strongpoints, all round
        # Each strongpoint has infantry with it, not all the vehicles in one place.
        apcs = [u for u in units if u["type"] == "BTR-80"]
        self.assertGreater(math.hypot(apcs[0]["x"] - apcs[1]["x"], apcs[0]["y"] - apcs[1]["y"]), 300)

    def test_old_save_garrison_is_moved_to_the_edge(self):
        w = World(garrisons=True)
        g = next(u for u in w.state.ground_units.values() if u.role is GroundRole.GARRISON)
        g.ring_m, g.sited, g.spots = 0.0, True, [Pos(0, 0)]
        w.war()
        self.assertEqual((g.ring_m, g.sited, g.spots), (TOWN_EDGE_M[2], False, []))


class GarrisonBubbleTests(unittest.TestCase):
    def test_garrisons_spawn_closer_than_formations(self):
        from campaign.bubble import NM, BubbleConfig, despawn_radius, spawn_radius
        w = World(garrisons=True)
        g = w.war().garrison("front")
        cfg = BubbleConfig()
        self.assertEqual((spawn_radius(g, cfg), despawn_radius(g, cfg)), (8 * NM, 10 * NM))
        self.assertEqual(spawn_radius(unit("b", Side.BLUE, "mid", 0), cfg), 20 * NM)


if __name__ == "__main__":
    unittest.main()
