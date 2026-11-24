import tempfile
import unittest
from pathlib import Path

from campaign.geography import Area, Geography
from campaign.model import GroundOrder, Link, ObjectiveDef, ObjectiveType, Pos, Side, Theater
from campaign.scenario_file import (ScenarioError, build_state, front_links, load_scenario_def, scenario_path)

NM = 1852.0


def obj(oid, x_nm, y_nm, t, country, name=None, priority=1):
    return ObjectiveDef(id=oid, name=name or oid, type=t, pos=Pos(x_nm * NM, y_nm * NM), features=[],
                        priority=priority, dcs_airbase=oid if t is ObjectiveType.AIRBASE else None, country=country)


THEATER = Theater("Syria", {o.id: o for o in [
    obj("ab_red", 0, 0, ObjectiveType.AIRBASE, "Syria"),
    obj("ab_blue", -40, 0, ObjectiveType.AIRBASE, "Jordan"),
    obj("town_big", 10, 0, ObjectiveType.CITY, "Syria", priority=3),
    obj("town_border", -15, 0, ObjectiveType.CITY, "Syria"),
    obj("town_jordan", -25, 0, ObjectiveType.CITY, "Jordan"),
    obj("town_leb", 0, -20, ObjectiveType.CITY, "Lebanon"),
    obj("mil_div", 5, 5, ObjectiveType.DEPOT, "Syria", "SAA 1st Armoured Division"),
    obj("mil_adf", 6, -5, ObjectiveType.DEPOT, "Syria", "SyADF 150th Regiment"),
    obj("far_away", 300, 0, ObjectiveType.CITY, "Syria"),
]}, [Link("town_border", "town_jordan", "road"), Link("town_big", "ab_red", "road"),
     Link("town_jordan", "ab_blue", "road")])

TOML = '''
name = "t"
map = "Syria"
[area]
center = "ab_red"
radius_nm = 60
[sides.blue]
countries = ["Jordan"]
[sides.red]
countries = ["Syria"]
[owners]
town_border = "blue"
[[squadrons]]
id = "vipers"
side = "blue"
type = "F-16C_50"
base = "ab_blue"
aircraft = 12
roles = ["CAP"]
player = true
[[formations]]
side = "blue"
at = "town_jordan"
brg_nm = [0, 2]
templates = ["blue_tank_bn", "blue_mech_bn"]
order = "RESERVE"
[rules]
airbase_defence = { red = ["red_sa8"] }
city_defence = { red = { min_priority = 3, templates = ["red_aaa"] } }
'''


def load(text=TOML):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.toml"
        p.write_text(text)
        return load_scenario_def(p)


class ScenarioFileTests(unittest.TestCase):
    def build(self, text=TOML, geo=None):
        return build_state(load(text), THEATER, geo or Geography())

    def test_ownership(self):
        st = self.build().state
        own = {k: v.owner for k, v in st.objectives.items()}
        self.assertEqual(own["ab_red"], Side.RED)
        self.assertEqual(own["town_jordan"], Side.BLUE)
        self.assertEqual(own["town_border"], Side.BLUE)        # hand override
        self.assertEqual(own["town_leb"], Side.NEUTRAL)        # nobody's country
        self.assertNotIn("far_away", st.objectives)            # outside the area

    def test_order_of_battle(self):
        st = self.build().state
        self.assertEqual(st.squadrons["vipers"].base_id, "ab_blue")
        units = st.ground_units.values()
        names = sorted(u.name for u in units)
        self.assertIn("SAA 1st Armoured Division: tank battalion (T-72)", names)
        self.assertIn("SyADF 150th Regiment: SA-6 battery", names)
        self.assertIn("ab_red air defence: SA-8 platoon", names)
        self.assertIn("town_big defence: air defence platoon", names)
        blue = [u for u in units if u.side is Side.BLUE]
        self.assertEqual(len(blue), 2)
        self.assertTrue(all(u.order is GroundOrder.RESERVE for u in blue))
        a, b = (u.pos for u in blue)
        self.assertGreater(abs(a.x - b.x) + abs(a.y - b.y), 1000)    # spread out, not stacked
        self.assertAlmostEqual(a.x, -23 * NM, delta=10)               # 2 nm north of town_jordan

    def test_front(self):
        b = self.build()
        self.assertEqual(front_links(b), [])          # town_border is blue; its only link goes to Jordan
        b2 = self.build(TOML.replace('town_border = "blue"', ""))
        self.assertEqual(front_links(b2), [("town_border", "town_jordan")])

    def test_formations_avoid_water(self):
        lake = Geography(lakes=[Area("lake", [[[(-23.5 * NM, -1 * NM), (-22.5 * NM, -1 * NM),
                                                (-22.5 * NM, 1 * NM), (-23.5 * NM, 1 * NM)]]])])
        st = self.build(geo=lake).state
        for u in st.ground_units.values():
            self.assertFalse(lake.is_water(u.pos), u.name)

    def test_errors(self):
        for bad, msg in [(TOML.replace('base = "ab_blue"', 'base = "ab_red"'), "belongs to red"),
                         (TOML.replace('base = "ab_blue"', 'base = "town_big"'), "isn't an airbase"),
                         (TOML.replace('"blue_mech_bn"', '"nope"'), "no template"),
                         (TOML.replace('side = "blue"\nat', 'side = "green"\nat'), "blue or red"),
                         (TOML + "\nbogus = 1\n", "unknown keys")]:
            with self.assertRaises(ScenarioError, msg=msg) as cm:
                build_state(load(bad), THEATER, Geography())
            self.assertIn(msg, str(cm.exception))

    def test_shipped_scenario(self):
        sd = load_scenario_def(scenario_path("southern_syria"))
        b = build_state(sd)
        self.assertEqual(b.warnings, [])
        st = b.state
        self.assertEqual(st.objectives["ab_khalkhalah"].owner, Side.RED)
        self.assertEqual(st.objectives["ab_muwaffaq_salti"].owner, Side.BLUE)
        self.assertGreater(len(front_links(b)), 0)
        self.assertGreater(len(st.ground_units), 50)


if __name__ == "__main__":
    unittest.main()
