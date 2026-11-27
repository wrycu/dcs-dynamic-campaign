import asyncio
import re
import unittest
from pathlib import Path

from campaign import warmap
from campaign.catalog import cruise_ms
from campaign.model import (CampaignState, Battle, Flight, FlightState, GroundOrder, GroundRole, GroundUnit,
                            ObjectiveDef, ObjectiveState, ObjectiveType, Package, Pos, Side, Squadron, Theater,
                            Waypoint)
from campaign.settings import SettingsError, server_config
from campaign.warmap import NM, Picture, WarMap, territory

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "lua" / "client" / "campaign_client.lua"


def area(poly):
    return abs(sum(p["x"] * q["y"] - q["x"] * p["y"] for p, q in zip(poly, poly[1:] + poly[:1]))) / 2


def inside(x, y, poly):
    ring = [(p["x"], p["y"]) for p in poly]
    return warmap._inside(x, y, ring)


class TerritoryTests(unittest.TestCase):
    def test_a_straight_front_halfway_between(self):
        # Blue to the west (small y), red to the east: the front runs north-south at y = 50 km.
        blue = [Pos(x, 0) for x in (0, 40_000, 80_000)]
        red = [Pos(x, 100_000) for x in (0, 40_000, 80_000)]
        t = territory(blue, red)
        self.assertEqual((len(t.blue), len(t.red), len(t.front)), (1, 1, 1))
        for p in t.front[0][1:-1]:
            self.assertAlmostEqual(p["y"], 50_000, delta=200)
        # The two sides tile the box: nothing missing, nothing twice.
        xs = [p["x"] for p in t.blue[0] + t.red[0]]
        ys = [p["y"] for p in t.blue[0] + t.red[0]]
        box = (max(xs) - min(xs)) * (max(ys) - min(ys))
        self.assertAlmostEqual(area(t.blue[0]) + area(t.red[0]), box, delta=box * 1e-6)
        self.assertTrue(inside(40_000, 0, t.blue[0]))
        self.assertTrue(inside(40_000, 100_000, t.red[0]))

    def test_an_east_west_front_is_still_one_polygon_a_side(self):
        # The southern Syria front runs along the grid rows: it must not break into cells.
        blue = [Pos(0, y) for y in range(0, 200_001, 25_000)]
        red = [Pos(60_000 + (y % 50_000) / 5, y) for y in range(0, 200_001, 25_000)]
        t = territory(blue, red)
        self.assertEqual((len(t.blue), len(t.red)), (1, 1))
        self.assertLess(sum(len(p) for p in t.blue + t.red), 120)

    def test_an_enclave_is_a_ring(self):
        blue = [Pos(x, y) for x in (0, 50_000, 100_000) for y in (0, 50_000, 100_000) if (x, y) != (50_000, 50_000)]
        red = [Pos(50_000, 50_000), Pos(200_000, 200_000)]
        t = territory(blue, red)
        rings = [ln for ln in t.front if ln[0] == ln[-1]]
        self.assertEqual(len(rings), 1)
        self.assertTrue(any(inside(50_000, 50_000, p) and area(p) < 60_000 ** 2 for p in t.red))   # its own cell, 50 km a side

    def test_one_side_only(self):
        self.assertEqual(territory([Pos(0, 0)], []).front, [])


def obj(oid, x, y, t=ObjectiveType.CITY):
    return ObjectiveDef(oid, oid.replace("_", " ").title(), t, Pos(x, y), [], 2)


class World:
    """Blue in the west (y < 0), red in the east; the front at y = 0. km."""

    def __init__(self):
        k = 1000
        objs = [obj("blue_ab", 0, -60 * k, ObjectiveType.AIRBASE), obj("west_town", 0, -15 * k),
                obj("east_town", 0, 15 * k), obj("red_ab", 0, 120 * k, ObjectiveType.AIRBASE),
                obj("red_depot", 20 * k, 60 * k, ObjectiveType.DEPOT)]
        self.theater = Theater("Syria", {o.id: o for o in objs}, [])
        self.state = CampaignState("Syria", 3600 * 26 + 60 * 5, {})
        for o in objs:
            self.state.objectives[o.id] = ObjectiveState(o.id, Side.BLUE if o.pos.y < 0 else Side.RED)
        self.state.squadrons = {
            "blue_sq": Squadron("blue_sq", Side.BLUE, "VFA-1", "FA-18C_hornet", "blue_ab", {"CAP"}, 10, 2, 12, True),
            "red_sq": Squadron("red_sq", Side.RED, "1 Sqn", "MiG-29A", "red_ab", {"CAP"}, 8, 0, 8, False)}

    def unit(self, uid, side, x, y, comp, role, **kw):
        u = GroundUnit(uid, side, uid.replace("_", " "), role, comp, Pos(x, y), None, kw.pop("order", GroundOrder.DEFEND),
                       **kw)
        self.state.ground_units[uid] = u
        return u

    def flight(self, fid, sq, state, pos=None, mission="CAP", takeoff=0.0, player=False, aircraft=2):
        f = Flight(fid, f"p_{fid}", sq, fid.upper(), mission, aircraft, "", [Waypoint(Pos(0, -20_000), 7000)],
                   takeoff, state=state, player_preferred=player, pos=pos, alt_m=7900 if pos else None,
                   hdg=90 if pos else None)
        self.state.flights[fid] = f
        self.state.packages[f.package_id] = Package(f.package_id, Side.BLUE, "east_town", 0, [fid])
        return f

    def picture(self, **kw):
        return Picture(self.state, self.theater, Side.BLUE, magvar=5.0, side_names={Side.BLUE: "Coalition"},
                       **kw).build()


def infos(spec):
    return [o.get("info") or "" for o in spec["objects"]]


class IntelTests(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_what_blue_knows_of_red_ground(self):
        w, k = self.w, 1000
        w.unit("far_tanks", Side.RED, 0, 90 * k, {"T-72B": 30}, GroundRole.ARMOR)
        w.unit("near_tanks", Side.RED, 5 * k, 6 * k, {"T-72B": 23}, GroundRole.ARMOR)
        w.unit("our_mech", Side.BLUE, 0, -5 * k, {"M-2 Bradley": 12}, GroundRole.MECH, full=15)
        text = "\n".join(infos(w.picture()))
        self.assertEqual(text.count("Enemy armour"), 1)
        self.assertIn("about 25 vehicles", text)           # a rough count, not 23
        self.assertNotIn("far tanks", text)                # nor its name
        self.assertIn("our mech\nDefending\n12 of 15 vehicles", text)

    def air_defence(self):
        w, k = self.w, 1000
        w.unit("far_sa10", Side.RED, 0, 110 * k, {"S-300PS 5P85C ln": 4, "S-300PS 40B6M tr": 1}, GroundRole.SAM,
               full=8)
        w.unit("far_sa6", Side.RED, 25 * k, 100 * k, {"Kub 2P25 ln": 4, "Kub 1S91 str": 1}, GroundRole.SAM)
        w.unit("far_shilka", Side.RED, 0, 100 * k, {"ZSU-23-4 Shilka": 2}, GroundRole.AAA)
        w.unit("near_shilka", Side.RED, 0, 3 * k, {"ZSU-23-4 Shilka": 2}, GroundRole.AAA)
        w.unit("our_hawk", Side.BLUE, 0, -50 * k, {"Hawk ln": 3, "Hawk sr": 1, "Hawk tr": 1}, GroundRole.SAM)
        w.unit("our_avenger", Side.BLUE, 0, -10 * k, {"M1097 Avenger": 2}, GroundRole.SAM)

    @staticmethod
    def sites(spec):
        """Each air defence icon's (label, y in km) -> its layer fields."""
        return {(o["label"], round(o["y"] / 1000)): {key: o[key] for key in ("detail", "groups") if key in o}
                for o in spec["objects"] if o["kind"] == "icon" and o["sym"].startswith(("sam", "aaa"))}

    def test_air_defence_layers(self):
        self.air_defence()
        spec = self.w.picture()
        sites = self.sites(spec)
        self.assertEqual(sites[("SA-10", 110)], {})                 # long range: always
        self.assertEqual(sites[("SA-6", 100)], {"detail": True})    # radiates: known, zoomed in
        self.assertEqual(sites[("ZSU-23-4", 3)], {"detail": True})  # the one near our ground
        self.assertEqual(sites[("Hawk", -50)], {"detail": True})    # ours, not long range
        self.assertEqual(sites[("Avenger", -10)], {"detail": True})
        self.assertEqual(len(sites), 5)     # the far Shilka is on nobody's route: not sent
        self.assertEqual(spec["detail_scale"], round(40 * warmap.NM))
        text = "\n".join(infos(spec))
        self.assertIn("SA-10 (red)\n3 or 4 launchers\nReported damaged\nRadar active", text)
        self.assertIn("our hawk\nHawk: 3 launchers, 5 of 5 vehicles\nRadar active", text)
        self.assertIn("our avenger\nAvenger: 2 launchers, 2 of 2 vehicles", text)
        self.assertNotIn("Avenger, 2 of 2 vehicles\nRadar", text)   # no radar we model
        rings = [o for o in spec["objects"] if o["kind"] == "ring"]
        self.assertEqual(len(rings), 5)
        self.assertFalse(any("info" in o for o in rings))  # clicks go to what's inside
        self.assertEqual([o for o in rings if "detail" not in o][0]["r"], 75_000)
        self.assertIn("  SA-10", spec["panel"])

    def test_a_selected_flight_shows_the_air_defence_on_its_route(self):
        self.air_defence()
        w, k = self.w, 1000
        f = w.flight("strike", "blue_sq", FlightState.PLANNED, mission="STRIKE", takeoff=w.state.clock + 600,
                     player=True)
        f.waypoints = [Waypoint(Pos(0, 50 * k), 7000), Waypoint(Pos(0, 101 * k), 7000)]
        spec = w.picture()
        sites = self.sites(spec)
        self.assertEqual(sites[("ZSU-23-4", 100)], {"groups": ["f:strike"]})  # 1 km off the route: sent now
        self.assertEqual(sites[("SA-6", 100)], {"detail": True, "groups": ["f:strike"]})  # 25 km off, reaches 22 + 3 nm
        self.assertEqual(sites[("SA-10", 110)], {})        # shown anyway
        ring = next(o for o in spec["objects"] if o["kind"] == "ring" and o["r"] == 2500 and o.get("groups") == ["f:strike"])
        self.assertEqual(ring["groups"], ["f:strike"])     # the ring goes with its site
        route = next(o for o in spec["objects"] if o["kind"] == "line" and o.get("select") == "f:strike")
        self.assertIn("Air defence on the route: ZSU-23-4 x2, SA-10, SA-6", route["info"])  # and the near one

    def test_airbases_and_targets(self):
        text = "\n".join(infos(self.w.picture()))
        self.assertIn("Blue Ab (Coalition)\nVFA-1: 10 FA-18C_hornet ready, 2 damaged", text)
        self.assertIn("Red Ab (red)\nReported here: MiG-29A", text)
        self.assertNotIn("1 Sqn", text)          # the enemy's squadron details aren't known
        self.assertIn("Red Depot: depot (red)", text)

    def test_our_infrastructure_too(self):
        w = self.w
        o = obj("blue_depot", 10_000, -40_000, ObjectiveType.DEPOT)
        w.theater.objectives[o.id] = o
        w.state.objectives[o.id] = ObjectiveState(o.id, Side.BLUE)
        self.assertIn("Blue Depot: depot (Coalition)", "\n".join(infos(w.picture())))

    def test_flights(self):
        w, k = self.w, 1000
        w.flight("cap1", "blue_sq", FlightState.ENROUTE, Pos(0, -30 * k), player=True)
        w.flight("bai2", "blue_sq", FlightState.PLANNED, mission="BAI", takeoff=w.state.clock + 1800, player=True)
        w.flight("later", "blue_sq", FlightState.PLANNED, mission="CAS", takeoff=w.state.clock + 4 * 3600, player=True)
        w.flight("seen", "red_sq", FlightState.ENROUTE, Pos(0, 60 * k))       # 65 km from our airbase
        w.flight("unseen", "red_sq", FlightState.ENROUTE, Pos(0, 170 * k))    # 230 km
        spec = w.picture()
        text = "\n".join(infos(spec))
        self.assertIn("CAP1: 2x FA-18C_hornet\nCAP at East Town\nenroute, 25,919 ft, heading 085M", text)
        self.assertIn("Player flight: 0 of 2 seats taken", text)
        self.assertEqual(text.count("Enemy fighters"), 1)
        self.assertIn("Heading 085M at 26,000 ft", text)
        routes = [o for o in spec["objects"] if o["kind"] == "line" and o["style"] != "boundary" and "groups" not in o]
        self.assertEqual(routes, [])        # routes only while their flight is selected
        self.assertIn("Day 2  02:05", spec["panel"])
        self.assertEqual([b["label"] for b in spec["flights"]],   # the panel's buttons; "later" is 4 h out
                         ["CAP1  CAP  enroute  2 seats", "BAI2  BAI  T-0:30:00  2 seats"])

    def test_a_flight_button_selects_its_plan(self):
        w, k = self.w, 1000
        f = w.flight("cas3", "blue_sq", FlightState.PLANNED, mission="CAS", takeoff=w.state.clock + 2 * 3600,
                     player=True)
        f.waypoints = [Waypoint(Pos(0, 50 * k), 7000)]
        w.unit("shilka", Side.RED, 0, 40 * k, {"ZSU-23-4 Shilka": 2}, GroundRole.AAA)
        spec = w.picture()
        [button] = spec["flights"]
        self.assertEqual(button["label"], "CAS3  CAS  T-2:00:00  2 seats")
        self.assertEqual((button["name"], button["takeoff_in"], button["tail"]), ("CAS3  CAS", 7200, "2 seats"))
        self.assertEqual(warmap.t_minus(-75), "T+0:01:15")
        self.assertEqual(button["select"], "f:cas3")
        self.assertIn("Air defence on the route: ZSU-23-4", button["info"])
        # The whole plan in view: from the airbase (y -60 km) to its waypoint (y 50).
        self.assertEqual(button["fit"], [0, -60_000, 0, 50_000])
        in_group = [o for o in spec["objects"] if o.get("groups") == ["f:cas3"]]
        kinds = sorted(o["kind"] + ":" + o.get("sym", o.get("style", "")) for o in in_group)
        # Only while selected: its route (white), its waypoints' times, its target and the Shilka.
        self.assertEqual(kinds, ["icon:aaa", "icon:target", "line:solid2", "ring:dash", "text:"])
        route = next(o for o in in_group if o["kind"] == "line")
        self.assertEqual((route["color"], route["select"]), (warmap.WHITE, "f:cas3"))
        target = next(o for o in in_group if o.get("sym") == "target")
        self.assertEqual((target["y"], target["info"]), (15_000, "CAS3's target: East Town"))
        # 110 km at the Hornet's cruise speed after take-off at 04:05.
        leg = 110_000 / cruise_ms("FA-18C_hornet")
        eta = w.state.clock + 2 * 3600 + leg
        text = next(o for o in in_group if o["kind"] == "text")["text"]
        self.assertEqual(text, f"WP1 {warmap.hhmm(eta)} +{int(leg) // 3600}:{int(leg) // 60 % 60:02d}")

    def test_a_selected_flight_shows_its_package_in_yellow(self):
        w, k = self.w, 1000
        sead = w.flight("sead1", "blue_sq", FlightState.PLANNED, mission="SEAD", takeoff=w.state.clock + 600,
                        player=True)
        strike = w.flight("strike2", "blue_sq", FlightState.PLANNED, mission="STRIKE",
                          takeoff=w.state.clock + 900, player=True)
        strike.waypoints = [Waypoint(Pos(0, 10 * k), 7000, action="BOMB", tot=w.state.clock + 3600)]
        strike.package_id = sead.package_id
        w.state.packages[sead.package_id].flight_ids.append("strike2")
        spec = w.picture()
        yellow = [o for o in spec["objects"] if o["kind"] == "line" and o["color"] == warmap.YELLOW]
        # Each one's route in yellow while the other is selected.
        self.assertEqual(sorted(tuple(o["groups"]) for o in yellow), [("f:sead1",), ("f:strike2",)])
        label = next(o for o in spec["objects"] if o["kind"] == "text" and o["groups"] == ["f:sead1"]
                     and o["color"] == warmap.YELLOW)
        self.assertEqual(label["text"], "STRIKE2 STRIKE 03:05")     # its time on target
        tgt = next(o for o in spec["objects"] if o["kind"] == "text" and o["groups"] == ["f:strike2"]
                   and o["color"] == warmap.WHITE)
        self.assertEqual(tgt["text"], "TGT 03:05 +0:45")

    def test_battles_and_news(self):
        w = self.w
        w.state.battles["east_town"] = Battle("east_town", Side.BLUE, 0.0, hours=3)
        spec = w.picture(news=["02:00 Coalition attacks East Town"])
        self.assertIn("Battle of East Town", [o.get("text") for o in spec["objects"]])
        self.assertIn("  East Town: Coalition attacking, 3 h", spec["panel"])
        self.assertIn("  02:00 Coalition attacks East Town", spec["panel"])
        town = w.theater.objectives["east_town"].pos
        self.assertEqual((spec["center"]["x"], spec["center"]["y"]), (round(town.x), round(town.y)))


class SymbolTests(unittest.TestCase):
    def test_the_client_knows_every_symbol(self):
        lua = CLIENT.read_text()
        table = lua[lua.index("local SYMBOLS = {"):]
        table = table[:table.index("\n}")]
        known = set(re.findall(r"(\w+) = \"", table))
        used = (set(warmap.ROLE_SYM.values()) | set(warmap.INFRA_SYM.values())
                | {"airbase", "farp", "target", "sam_long", "sam_medium", "sam_short", "aaa", "helo", "awacs",
                   "tanker", "fighter", "attack"})
        self.assertEqual(used - known, set())
        self.assertIn("spec.show == false", lua)


class Hooks:
    def __init__(self):
        self.calls = []

    async def __call__(self, op, data):
        self.calls.append((op, data))

    def maps(self):
        return [(d["player_id"], d["args"][0]["show"], d["args"][0]["keep_view"]) for op, d in self.calls
                if op == "client_rpc" and d["method"] == "campaignMap"]


class WarMapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.w = World()
        self.hooks = Hooks()
        self.now = 1000.0
        self.map = WarMap(self.w.state, self.w.theater, self.hooks, refresh_s=30, clock=lambda: self.now)
        self.tasks = []

    def bg(self, coro):
        self.tasks.append(asyncio.ensure_future(coro))

    async def settle(self):
        await asyncio.gather(*self.tasks)
        self.tasks = []

    async def test_hello_preloads_hidden_and_refreshes(self):
        self.assertTrue(self.map.on_player({"kind": "client_hello", "player_id": 3, "version": 4}, self.bg))
        await self.settle()
        self.assertEqual(self.hooks.maps(), [(3, False, False)])
        self.assertIn("Ctrl+Shift+F8", self.hooks.calls[-1][1]["text"])
        self.now += 10
        await self.map.tick()
        self.assertEqual(len(self.hooks.maps()), 1)
        self.now += 25
        await self.map.tick()
        self.assertEqual(self.hooks.maps()[-1], (3, False, True))   # kept view, not popped open
        self.map.on_player({"kind": "player_disconnect", "player_id": 3}, self.bg)
        self.now += 60
        await self.map.tick()
        self.assertEqual(len(self.hooks.maps()), 2)

    async def test_map_command_shows_it(self):
        self.assertTrue(self.map.on_player({"kind": "chat_command", "player_id": 5, "text": "map"}, self.bg))
        self.assertFalse(self.map.on_player({"kind": "chat_command", "player_id": 5, "text": "status"}, self.bg))
        await self.settle()
        self.assertEqual(self.hooks.maps(), [(5, True, False)])
        # Never said hello (a restarted server, or no client script): say what it needs.
        self.assertIn("client script", self.hooks.calls[-1][1]["text"])

    async def test_a_failing_send_is_logged_not_raised(self):
        async def broken(op, data):
            raise RuntimeError("hooks gone")
        m = WarMap(self.w.state, self.w.theater, broken, clock=lambda: self.now)
        m.on_player({"kind": "client_hello", "player_id": 3}, self.bg)
        with self.assertLogs("campaign", "WARNING"):
            await self.settle()

    def test_news_and_territory_cache(self):
        self.map.add_news("coalition takes East Town")
        self.assertEqual(list(self.map.news), ["02:05 Coalition takes East Town"])
        t1 = self.map.terrain()
        self.assertIs(self.map.terrain(), t1)
        self.w.state.objectives["east_town"].owner = Side.BLUE
        self.assertIsNot(self.map.terrain(), t1)


class SettingTests(unittest.TestCase):
    def test_map_refresh(self):
        self.assertEqual(server_config({}).map_refresh_s, 30)
        self.assertEqual(server_config({"map_refresh_s": 10}).map_refresh_s, 10)
        for bad in ({"map_refresh_s": 1}, {"map_refresh_s": "30"}):
            with self.assertRaises(SettingsError):
                server_config(bad)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_southern_syria_has_a_war_map(self):
        from campaign.scenarios.southern_syria import make
        from campaign.server import Engine
        from campaign.settings import load_settings
        hooks = Hooks()

        async def mission(op, data):
            return {}
        e = Engine(load_settings(None), make(), mission_call=mission, hooks_call=hooks)
        self.assertIsNotNone(e.warmap)
        e.on_player({"kind": "client_hello", "player_id": 2, "version": 4})
        await asyncio.gather(*e.tasks)
        (pid, show, keep), = hooks.maps()
        spec = hooks.calls[0][1]["args"][0]
        self.assertEqual((pid, show), (2, False))
        self.assertIn("Coalition", "\n".join(spec["panel"]))
        self.assertLess(len(spec["objects"]), 400)
        e.news("coalition attacks Jabab")
        self.assertIn("Coalition attacks Jabab", e.warmap.news[-1])


if __name__ == "__main__":
    unittest.main()
