import tempfile
import unittest
from pathlib import Path

from campaign.model import ObjectiveDef, ObjectiveType, Pos, Theater
from campaign.theater import dumps, load_theater, neighbours
from campaign.theater_build import (Edits, Place, Town, airbase_objective, apply_edits_to, bridges_on, candidate_links,
                                    classify, infra_objectives, is_building, make_link, merge_bridges, merge_places,
                                    pick_objectives, slug)


def obj(oid, x, y, t=ObjectiveType.CITY):
    return ObjectiveDef(id=oid, name=oid.upper(), type=t, pos=Pos(x, y), features=[], priority=1)


class PlaceTests(unittest.TestCase):
    def test_classify_whole_words(self):
        self.assertEqual(classify("Damascus"), "town")            # no "dam" in it
        self.assertEqual(classify("Umayyad Mosque"), "landmark")
        self.assertEqual(classify("SAA 121th Brigade"), "military")
        self.assertEqual(classify("Army Vehicle Training Ground"), "military")

    def test_buildings_only(self):
        self.assertTrue(is_building("SYRIA_BLOCK_BUILDING_04"))
        self.assertTrue(is_building("SYRIA_CITY_HOUSE_05"))
        self.assertFalse(is_building("TRASHCAN_EU_SMALL"))
        self.assertFalse(is_building("STREET_MARKET_2"))

    def test_slug(self):
        self.assertEqual(slug("Harran al'Awamid"), "harran_al_awamid")
        self.assertEqual(slug("Ṣaydnāyā"), "saydnaya")

    def test_merge(self):
        places = [Place("Damascus", Pos(0, 0)), Place("Old Town", Pos(1500, 0)),   # one town
                  Place("Umayyad Mosque", Pos(500, 500)),                         # joins it
                  Place("Lonely Hotel", Pos(50_000, 0)),                          # dropped
                  Place("Duma", Pos(10_000, 0)), Place("SAA 1st Division", Pos(20_000, 0))]
        towns, mil = merge_places(places)
        self.assertEqual(sorted(t.name for t in towns), ["Damascus", "Duma"])
        dam = next(t for t in towns if t.name == "Damascus")
        self.assertEqual(dam.members, ["Damascus", "Old Town", "Umayyad Mosque"])
        self.assertEqual(dam.id, "town_damascus")
        self.assertEqual([m.id for m in mil], ["mil_saa_1st_division"])


class ObjectiveTests(unittest.TestCase):
    def test_airbases_need_runways(self):
        ab = {"name": "Khalkhalah", "category": 0, "pos": {"x": 1, "y": 2}, "runways": [{"name": 15}, {"name": 33}]}
        o = airbase_objective(ab)
        self.assertEqual((o.id, o.dcs_airbase, [f.dcs_ref for f in o.features]), ("ab_khalkhalah", "Khalkhalah",
                                                                                   ["15", "33"]))
        for pad in ("HS27", "HMed04", "HOil01", "H_med_orig_02", "HStad05", "Sanliurfa Heliport"):
            self.assertIsNone(airbase_objective({**ab, "name": pad, "runways": []}), pad)
        strip = airbase_objective({**ab, "name": "At Tanf", "runways": []})      # an airfield without runways in DCS
        self.assertEqual([f.id for f in strip.features], ["strip"])

    def test_pick_with_edits(self):
        towns = [Town(f"town_{i}", f"T{i}", Pos(i * 1000, 0), buildings=1000 * i) for i in range(1, 6)]
        edits = Edits.from_dict({"objectives": {
            "rename": {"town_5": "Big Town"}, "drop": ["town_4"], "keep": ["town_1"],
            "add": [{"id": "depot_x", "name": "Depot X", "type": "DEPOT", "x": 5, "y": 6, "priority": 3}]}})
        objs = pick_objectives([], towns, [], 3500, edits)
        self.assertEqual(sorted(objs), ["depot_x", "town_1", "town_5"])      # 3,500+: 5, 4; 4 dropped; 1 kept
        self.assertEqual(sorted(pick_objectives([], towns, [], 0, Edits(), max_towns=2)), ["town_4", "town_5"])
        self.assertEqual((objs["town_5"].name, objs["town_5"].priority), ("Big Town", 3))

    def test_bad_edits(self):
        for bad in ({"objectivs": {}}, {"links": {"add": [["a"]]}}, {"objectives": {"add": [{"id": "x"}]}}):
            with self.assertRaises(ValueError, msg=bad):
                Edits.from_dict(bad)


class LinkTests(unittest.TestCase):
    def test_neighbourhood_graph(self):
        # a - b - c in a line: no a-c link (b is closer to both); d far away still connected.
        objs = {o.id: o for o in [obj("a", 0, 0), obj("b", 10_000, 0), obj("c", 20_000, 0), obj("d", 200_000, 0)]}
        self.assertEqual(candidate_links(objs, 50_000), [("a", "b"), ("b", "c"), ("c", "d")])
        edits = Edits.from_dict({"links": {"drop": [["a", "b"]], "add": [["a", "c"]]}})
        self.assertEqual(candidate_links(objs, 50_000, edits), [("a", "c"), ("b", "c"), ("c", "d")])

    def test_bridges_on_a_route(self):
        route = [Pos(0, 0), Pos(1000, 0), Pos(1000, 1000)]
        bridges = merge_bridges([{"id": 7, "type": "CAR_BRIDGE", "x": 1010, "y": 500},
                                 {"id": 8, "type": "CAR_BRIDGE", "x": 1030, "y": 520},     # twin span: merged
                                 {"id": 9, "type": "RAIL_BRIDGE", "x": 500, "y": 20},
                                 {"id": 10, "type": "CAR_BRIDGE", "x": 500, "y": 300}])    # off the road
        self.assertEqual([b["id"] for b in bridges_on(route, bridges)], [9, 7])
        objs = {"a": obj("a", 0, 0), "b": obj("b", 1000, 1000)}
        ln = make_link("a", "b", objs, route, bridges)
        self.assertEqual((ln.via, ln.length_m), (["br_9", "br_7"], 2000.0))
        self.assertEqual(objs["br_9"].name, "Rail bridge 9")
        self.assertEqual(objs["br_7"].features[0].dcs_ref, "7")

    def test_save_and_load(self):
        objs = {"a": obj("a", 0, 0), "b": obj("b", 1000, 1000)}
        t = Theater("Syria", objs, [make_link("a", "b", objs, [Pos(0, 0), Pos(1000, 1000)], [])])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "map.json"
            p.write_text(dumps(t, {"area": {"center": "X"}}))
            t2 = load_theater(p)
        self.assertEqual(t2, t)
        self.assertEqual([o for o, _ in neighbours(t2, "a")], ["b"])



class InfraTests(unittest.TestCase):
    def test_sites_from_the_survey(self):
        objs = {"town_homs": obj("town_homs", 0, 0), "ab_x": obj("ab_x", 50_000, 0, ObjectiveType.AIRBASE)}
        survey = ([{"id": i, "type": "OILGAS_REFINERY_01", "x": 2000 + i * 100, "y": 0} for i in range(5)]
                  + [{"id": 10 + i, "type": f"DAM_TISHRIN_0{i}", "x": 20_000 + i * 3000, "y": 0} for i in range(3)]
                  + [{"id": 20 + i, "type": "LIQ_TANK", "x": 50_500 + i * 50, "y": 0} for i in range(12)]   # at the airbase
                  + [{"id": 40 + i, "type": "LIQ_TANK", "x": 10_000 + i * 50, "y": 9000} for i in range(12)]
                  + [{"id": 60, "type": "POWER_PLANT_01", "x": 30_000, "y": 30_000}])                     # too small
        out = infra_objectives(survey, objs)
        names = sorted(o.name for o in out.values())
        self.assertEqual(names, ["Fuel depot near TOWN_HOMS", "Refinery near TOWN_HOMS", "Tishrin Dam"])
        ref = next(o for o in out.values() if o.type is ObjectiveType.REFINERY)
        self.assertEqual((ref.area_of, len(ref.features), ref.features[0].dcs_ref), ("town_homs", 5, "2"))
        dam = next(o for o in out.values() if o.type is ObjectiveType.DAM)
        self.assertEqual(len(dam.features), 3)          # three sections 3 km apart: still one dam
        edits = Edits.from_dict({"objectives": {"drop": [ref.id], "rename": {dam.id: "Lake Assad dam"}}})
        apply_edits_to(out, edits)
        self.assertEqual(sorted(o.name for o in out.values()), ["Fuel depot near TOWN_HOMS", "Lake Assad dam"])


if __name__ == "__main__":
    unittest.main()
