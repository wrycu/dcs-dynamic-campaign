import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign import rides
from campaign.model import FlightState, SpawnState
from campaign.seats import FLY_OPEN_S
from campaign.session import Session
from campaign.warmap import Viewer
from tests.test_session import FakeEngine
from tests.test_warmap import World

FAST = dict(GATHER_S=0.05, DROP_S=0.01, POS_S=0.001, SPAWN_WAIT_S=0.0, PARK_S=0.0, ENTER_S=0.0)


class RideEngine(FakeEngine):
    """The Humvee goes straight to the end of each route it's given."""

    def __init__(self, w):
        super().__init__(w)
        self.car = None
        self.dcs_players = []

    async def hooks_call(self, op, data):
        if op == "players":
            self.calls.append(("hooks", op, data))
            return self.dcs_players
        return await super().hooks_call(op, data)

    async def mission_call(self, op, data):
        if op == "spawn_group":
            u = data["group"]["units"][0]
            self.car = (u["x"], u["y"])
        elif op == "set_route":
            p = data["points"][-1]
            self.car = (p["x"], p["y"])
        elif op == "despawn_group":
            self.car = None
        elif op == "query" and data.get("what") == "group":
            self.calls.append(("mission", op, data))
            if self.car is None:
                return {"exists": False}
            return {"exists": True, "units": [{"pos": {"x": self.car[0], "y": self.car[1]}, "hdg": 0, "spd": 5}]}
        return await super().mission_call(op, data)


class RideTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patch = mock.patch.multiple(rides, **FAST)
        self.patch.start()
        self.w = World()
        self.e = RideEngine(self.w)
        self.s = Session(self.e)
        self.s.rides.dcs_dir = Path("/nonexistent")   # no taxiways: straight lines
        self.tmp = tempfile.TemporaryDirectory()
        self.s.rides.starts_path = Path(self.tmp.name) / "ride_starts.json"
        self.e.airbases = {"Base": {"name": "Base", "pos": {"x": 1000.0, "y": 0.0}, "category": 0},
                           "Far": {"name": "Far", "pos": {"x": 90_000.0, "y": 0.0}, "category": 0}}
        self.e.warmap.session = self.s
        self.f = self.w.flight("bai2", "blue_sq", FlightState.PLANNED, mission="BAI",
                               takeoff=self.w.state.clock + 1800, player=True)
        self.f.callsign, self.f.aircraft, self.f.committed = "Uzi 4", 2, True
        self.f.player_seats = {1: "abc"}
        self.f.client_group = "Uzi 4 BAI"
        self.e.client_groups = [{"name": "Uzi 4 BAI", "units": [
            {"name": "Uzi 4-1", "id": "41", "skill": "Client", "type": "FA-18C_hornet", "x": 1000.0, "y": 0.0,
             "heading": 0.0},
            {"name": "Uzi 4-2", "id": "42", "skill": "High", "type": "FA-18C_hornet", "x": 1000.0, "y": 40.0,
             "heading": 0.0}]}]

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def mission(self, op):
        return [d for src, o, d in self.e.calls if src == "mission" and o == op]

    async def settle(self):
        for _ in range(200):
            await asyncio.sleep(0.005)
            if not self.s.rides.rides and all(t.done() for t in self.e.tasks):
                return
        self.fail("the ride didn't finish")

    async def test_stand_ins_on_every_spot(self):
        await self.s.on_mission()
        statics = [d["static"]["name"] for d in self.mission("spawn_static")]
        self.assertEqual(statics, ["Stand-in Uzi 4-1", "Stand-in Uzi 4-2"])

    async def test_fly_rides_to_the_jet(self):
        await self.s.on_mission()
        self.e.warmap.viewers[1] = Viewer(1, version=24)
        self.w.state.clock = self.f.takeoff_time - FLY_OPEN_S
        reply = await self.s.fly(1, "abc", "bai2")
        self.assertIn("To Uzi 4-1 by Humvee: front passenger seat", reply)
        self.assertEqual(self.e.hooks("force_slot"), [])   # not yet: the ride first
        await self.settle()
        rpcs = [d["method"] for d in self.e.hooks("client_rpc")]
        self.assertEqual(rpcs[0], "campaignRide")
        self.assertEqual(self.e.hooks("client_rpc")[0]["args"][0]["unit"], "Ride Uzi 4-1")
        self.assertIn("campaignRidePos", rpcs)
        self.assertEqual(rpcs[-1], "campaignEnter")
        self.assertEqual(self.e.hooks("force_slot"), [{"player_id": 1, "side": 2, "slot": "41"}])
        # Both stand-ins went before the player was put in: theirs, and the AI wingman's.
        removed = [d["name"] for d in self.mission("remove_static")[2:]]
        self.assertEqual(sorted(removed), ["Stand-in Uzi 4-1", "Stand-in Uzi 4-2"])
        self.assertIsNone(self.e.car)   # driven off and removed
        self.assertTrue(any("Off we go" in d["text"] for d in self.e.hooks("chat_to")))

    async def test_waiting_blue_spectators_ride_by_themselves_once_fly_opens(self):
        await self.s.on_mission()
        self.e.warmap.viewers[1] = Viewer(1, version=24)
        self.e.dcs_players = [{"id": 1, "ucid": "abc", "side": 0, "slot": ""}]
        await self.s.tick()   # still on the slot screen
        self.assertEqual(self.e.hooks("chat_to"), [])
        self.e.dcs_players[0]["side"] = 2
        await self.s.tick()   # blue spectator, too early: told when
        self.assertIn("Your ride to Uzi 4-1 comes at 02:15", self.e.hooks("chat_to")[0]["text"])
        self.assertEqual(self.s.rides.rides, {})
        await self.s.tick()
        self.assertEqual(len(self.e.hooks("chat_to")), 1)   # told once
        self.w.state.clock = self.f.takeoff_time - FLY_OPEN_S
        await self.s.tick()
        self.assertIn("To Uzi 4-1 by Humvee", self.e.hooks("chat_to")[-1]["text"])
        await self.settle()
        self.assertEqual(self.e.hooks("force_slot"), [{"player_id": 1, "side": 2, "slot": "41"}])
        n = len(self.e.hooks("force_slot"))
        await self.s.tick()   # boarded once only, not again after getting in
        self.assertEqual(len(self.e.hooks("force_slot")), n)

    def mark(self, idx, text, x, y, change="added"):
        self.s.on_event({"kind": "mark", "change": change, "idx": idx, "text": text, "pos": {"x": x, "y": y}})

    async def test_mapping_ride_starts_with_f10_marks(self):
        await self.s.on_mission()
        self.mark(1, "", 900.0, 300.0)                        # a new mark: no text yet
        self.mark(1, "Ride start", 900.0, 300.0, "changed")   # typed in
        self.assertIn("Ride start saved for Base (1 there now)", self.e.said[-1])
        self.mark(2, "ride start", 1100.0, -300.0)
        saved = json.loads(self.s.rides.starts_path.read_text())
        self.assertEqual([(s["x"], s["y"]) for s in saved["Syria"]["Base"]], [(900.0, 300.0), (1100.0, -300.0)])
        self.mark(1, "ride start, by the ops building", 900.0, 300.0, "changed")   # edited: still one
        self.assertEqual(len(self.s.rides.starts.at("Syria", "Base")), 2)
        self.mark(3, "ride start delete", 1110.0, -290.0)
        self.assertEqual(len(self.s.rides.starts.at("Syria", "Base")), 1)
        self.mark(4, "ride start", 50_000.0, 0.0)
        self.assertIn("No airbase within", self.e.said[-1])
        self.mark(5, "SAM here", 900.0, 0.0)                  # other marks: ignored
        self.assertEqual(len(self.s.rides.starts.at("Syria", "Base")), 1)
        await asyncio.gather(*self.e.tasks)
        drawn = self.mission("draw")[-1]
        self.assertEqual((drawn["shape"], drawn["text"]), ("mark", "Ride start (Base, saved)"))

    async def test_the_ride_starts_at_the_mapped_start(self):
        self.s.rides.starts.add("Syria", "blue_ab", rides.Pos(700.0, 20.0))
        self.s.rides.starts.add("Syria", "blue_ab", rides.Pos(5000.0, 0.0))
        await self.s.on_mission()
        self.e.warmap.viewers[1] = Viewer(1, version=24)
        self.w.state.clock = self.f.takeoff_time - FLY_OPEN_S
        await self.s.fly(1, "abc", "bai2")
        await self.settle()
        car = self.mission("spawn_group")[0]["group"]["units"][0]
        self.assertEqual((car["x"], car["y"]), (700.0, 20.0))

    async def test_an_old_client_script_goes_straight_in(self):
        await self.s.on_mission()
        self.e.warmap.viewers[1] = Viewer(1, version=23)
        self.w.state.clock = self.f.takeoff_time - FLY_OPEN_S
        self.assertEqual(await self.s.fly(1, "abc", "bai2"), "Into Uzi 4-1: BAI, take-off 02:35")
        self.assertEqual(len(self.e.hooks("force_slot")), 1)

    async def test_ride_off_in_settings_goes_straight_in(self):
        self.e.settings.server.ride_to_jet = False
        await self.s.on_mission()
        self.assertEqual(self.mission("spawn_static"), [])
        self.e.warmap.viewers[1] = Viewer(1, version=24)
        self.w.state.clock = self.f.takeoff_time - FLY_OPEN_S
        self.assertEqual(await self.s.fly(1, "abc", "bai2"), "Into Uzi 4-1: BAI, take-off 02:35")

    async def test_taking_the_slot_from_the_list_clears_the_stand_ins(self):
        await self.s.on_mission()
        self.s.on_player({"kind": "player_slot", "player_id": 3, "ucid": "abc", "slot": "41", "name": "Viper"})
        await asyncio.gather(*self.e.tasks)
        self.assertEqual(sorted(d["name"] for d in self.mission("remove_static")[2:]),
                         ["Stand-in Uzi 4-1", "Stand-in Uzi 4-2"])

    async def test_stand_ins_go_when_the_flight_goes_to_the_ai(self):
        await self.s.on_mission()
        self.s.release(self.f, "test")
        await asyncio.gather(*self.e.tasks)
        self.assertEqual(len(self.mission("remove_static")), 4)
        self.assertEqual(self.s.rides.jets, {})

    async def test_spawned_flight_gets_no_stand_ins(self):
        self.f.spawn = SpawnState.SPAWNED
        await self.s.on_mission()
        self.assertEqual(self.mission("spawn_static"), [])


if __name__ == "__main__":
    unittest.main()
