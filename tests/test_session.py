import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from campaign.air import AirSpawner
from campaign.air_bubble import AirBubble
from campaign.flight_spawn import Takeoff
from campaign.model import FlightState, Pos, SpawnState
from campaign.seats import FLY_OPEN_S, LATE_S, free_seats
from campaign.session import Session, client_group, mission_now
from campaign.settings import ServerConfig
from campaign.warmap import WarMap
from tests.test_warmap import World

TAKEOFF = Takeoff(airdrome_id=12, pos=Pos(0, -60_000), elev_m=400,
                  parking=[(31, Pos(10, -60_010)), (32, Pos(20, -60_020))])


class FakeEngine:
    def __init__(self, w: World):
        self.state = w.state
        self.settings = SimpleNamespace(server=ServerConfig(missions_dir=""))
        self.calls = []
        self.players = []
        self.said = []
        self.newsl = []
        self.tasks = []
        self.client_groups = []
        self.air = AirSpawner(self.state, self.mission_call)
        self.warmap = WarMap(self.state, w.theater, self.hooks_call)
        self.warmap.pilots["abc"] = "Viper"
        self.force_ok = True

    async def mission_call(self, op, data):
        self.calls.append(("mission", op, data))
        if op == "query" and data.get("what") == "mission":
            return {"date": {"Year": 2024, "Month": 6, "Day": 1}, "abs": 26 * 3600 + 300, "theatre": "Syria"}
        if op == "query" and data.get("what") == "type_size":
            return {"span": 11.8, "length": 17.1, "height": 5.5, "fuel_max": 4900.0}
        if op == "query" and data.get("what") == "client_groups":
            return {"groups": self.client_groups}
        return {}

    async def hooks_call(self, op, data):
        self.calls.append(("hooks", op, data))
        if op == "force_slot":
            return {"ok": self.force_ok}
        return {}

    async def takeoff_for(self, f, skip=()):
        return TAKEOFF

    def prepare_flight(self, f):
        pass

    def background(self, coro):
        self.tasks.append(asyncio.ensure_future(coro))

    def say(self, text, **kw):
        self.said.append(text)

    def news(self, text):
        self.newsl.append(text)

    def save_if_due(self, force=False):
        pass

    def hooks(self, op):
        return [d for src, o, d in self.calls if src == "hooks" and o == op]


class ClientGroupTests(unittest.TestCase):
    def test_players_seats_are_client_the_rest_ai(self):
        w = World()
        f = w.flight("bai2", "blue_sq", FlightState.PLANNED, mission="BAI", takeoff=w.state.clock + 1800, player=True)
        f.callsign, f.number, f.loadout = "Uzi 4", 22, ""
        f.player_seats = {1: "abc"}
        group, files = client_group(f, "FA-18C_hornet", w.state.squadrons["blue_sq"].side, TAKEOFF, fuel_kg=4900.0)
        self.assertEqual(group["name"], "Uzi 4 BAI")
        self.assertEqual((group["airdrome_id"], group["start"], group["task"]), (12, "hot", "CAS"))
        self.assertEqual(group["points"][0]["type"], "TakeOffParkingHot")
        u1, u2 = group["units"]
        self.assertEqual((u1["name"], u1["skill"], u2["name"], u2["skill"]), ("Uzi 4-1", "Client", "Uzi 4-2", "High"))
        self.assertEqual((u1["parking"], u2["parking"]), (31, 32))
        self.assertEqual(u1["callsign"]["name"], "Uzi41")
        self.assertEqual(u1["payload"]["fuel"], 4900.0)
        # A cartridge for the player's seat only, named for it, set on the unit.
        self.assertEqual(list(files), ["DTC/uzi_4_1.dtc"])
        self.assertEqual(u1["dtc"], "uzi_4_1")
        self.assertNotIn("dtc", u2)
        self.assertEqual(json.loads(files["DTC/uzi_4_1.dtc"])["data"]["terrain"], "Syria")
        cold, _ = client_group(f, "FA-18C_hornet", w.state.squadrons["blue_sq"].side, TAKEOFF, cold=True)
        self.assertEqual((cold["start"], cold["points"][0]["type"]), ("cold", "TakeOffParking"))

    def test_mission_starts_where_the_last_one_is_now(self):
        self.assertEqual(mission_now({"date": {"Year": 2024, "Month": 6, "Day": 1}, "abs": 26 * 3600 + 65}),
                         {"date": {"Year": 2024, "Month": 6, "Day": 2}, "start_time": 2 * 3600 + 65})


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.w = World()
        self.e = FakeEngine(self.w)
        self.s = Session(self.e)
        self.e.warmap.session = self.s
        self.f = self.w.flight("bai2", "blue_sq", FlightState.PLANNED, mission="BAI",
                               takeoff=self.w.state.clock + 1800, player=True)
        self.f.callsign = "Uzi 4"
        self.f.player_seats = {1: "abc"}

    async def settle(self):
        while self.e.tasks:
            t, self.e.tasks = self.e.tasks, []
            await asyncio.gather(*t)

    async def commit(self):
        with mock.patch("tools.build_test_missions.build", return_value=Path("out.miz")) as build:
            reply = self.s.commit(1, "abc", "bai2")
            await self.settle()
        return reply, build

    async def test_commit_builds_and_loads_the_session_mission(self):
        reply, build = await self.commit()
        self.assertEqual(reply, "Committing Uzi 4: 1 player, 1 AI. Building the mission; it restarts in about 15 s")
        self.assertTrue(self.f.committed)
        self.assertEqual(self.f.client_group, "Uzi 4 BAI")
        args = build.call_args.args
        self.assertEqual(args[2], "campaign-session-a.miz")
        extra = args[9]
        self.assertTrue(extra["drop_players"])
        self.assertEqual(extra["start_time"], 2 * 3600 + 300)
        self.assertEqual([g["name"] for g in extra["client_groups"]], ["Uzi 4 BAI"])
        self.assertEqual((args[11], args[12]), (None, False))   # no test airbase, the player group not moved
        self.assertEqual(self.e.hooks("load_mission"), [{"file": "campaign-dev\\campaign-session-a.miz", "delay": 15}])
        self.assertEqual(self.w.state.session_builds, 1)
        self.assertIn("Uzi 4 BAI is committed", self.e.said[0])

    async def test_who_cant_commit(self):
        f = self.f
        f.player_seats = {1: "lead", 2: "abc"}
        self.assertEqual(self.s.commit(1, "abc", "bai2"), "Only Uzi 4-1, the lead, can commit Uzi 4")
        f.player_seats = {1: "abc"}
        self.e.players = [{"player": "Goose", "name": "Colt 2-1"}]
        self.assertEqual(self.s.commit(1, "abc", "bai2"),
                         "Can't restart the mission while Goose (Colt 2-1) is flying: commit once they're out of their jets")
        self.e.players = []
        f.committed = True
        self.assertEqual(self.s.commit(1, "abc", "bai2"), "Uzi 4 is committed already")
        f.committed, f.state = False, FlightState.ENROUTE
        self.assertEqual(self.s.commit(1, "abc", "bai2"), "Uzi 4 has taken off")
        self.assertEqual(self.e.tasks, [])

    async def test_a_failed_build_puts_the_flight_back(self):
        with mock.patch("tools.build_test_missions.build", side_effect=SystemExit("patch failed")):
            self.s.commit(1, "abc", "bai2")
            with self.assertLogs("campaign", "WARNING"):
                await self.settle()
        self.assertFalse(self.f.committed)
        self.assertIn("Commit failed: patch failed", self.e.hooks("chat_to")[-1]["text"])
        self.assertFalse(self.s.building)

    async def loaded(self):
        """Committed, and the session mission loaded with its group."""
        await self.commit()
        self.e.client_groups = [{"name": "Uzi 4 BAI", "units": [{"name": "Uzi 4-1", "id": "41", "skill": "Client"},
                                                                {"name": "Uzi 4-2", "id": "42", "skill": "High"}]}]
        await self.s.on_mission()

    async def test_the_loaded_mission_gives_the_slots_and_the_rules(self):
        await self.loaded()
        self.assertEqual(self.f.slots, {1: "41"})
        self.assertEqual(free_seats(self.f), [])   # seat 2 is AI now: nobody can join it
        rules = self.e.hooks("slot_rules")[-1]["slots"]
        self.assertEqual(rules, {"41": {"reason": "Uzi 4-1 opens at 02:15, 20 min before take-off"}})
        self.w.state.clock = self.f.takeoff_time - FLY_OPEN_S + 1
        await self.s.tick()
        self.assertEqual(self.e.hooks("slot_rules")[-1]["slots"],
                         {"41": {"owner": "abc", "reason": "Uzi 4-1 is Viper's seat"}})
        n = len(self.e.hooks("slot_rules"))
        await self.s.tick()
        self.assertEqual(len(self.e.hooks("slot_rules")), n)   # sent only when they change
        self.f.player_seats = {1: None}   # left: free for anyone
        await self.s.seats_changed()
        self.assertEqual(self.e.hooks("slot_rules")[-1]["slots"], {})

    async def test_a_mission_without_its_group_sends_it_to_the_ai(self):
        await self.commit()
        self.e.client_groups = []
        await self.s.on_mission()
        self.assertFalse(self.f.committed)
        self.assertEqual(self.f.player_seats, {})
        self.assertIn("goes to the AI: this mission doesn't have its jets", self.e.newsl[0])

    async def test_fly(self):
        await self.loaded()
        self.assertEqual(await self.s.fly(1, "abc", "bai2"), "Fly opens at 02:15, 20 min before take-off")
        self.w.state.clock = self.f.takeoff_time - FLY_OPEN_S
        self.assertEqual(await self.s.fly(1, "abc", "bai2"), "Into Uzi 4-1: BAI, take-off 02:35")
        self.assertEqual(self.e.hooks("force_slot"), [{"player_id": 1, "side": 2, "slot": "41"}])
        self.assertEqual(self.e.hooks("client_rpc")[-1]["method"], "campaignEnter")
        self.e.force_ok = False
        self.assertIn("pick blue in the slot list first", await self.s.fly(1, "abc", "bai2"))
        self.assertEqual(await self.s.fly(1, "zzz", "bai2"), "You aren't in that flight")

    async def test_the_jet_born_is_the_flight_and_the_ai_doesnt_fly_it(self):
        await self.loaded()
        bubble = AirBubble(self.w.state, self.e.air, base_pos=lambda sq: Pos(0, -60_000), takeoff_for=self.e.takeoff_for)
        self.w.state.clock = self.f.takeoff_time + 60
        plan = bubble.plan()
        self.assertNotIn("bai2", plan.launch_virtual + plan.launch_dcs)   # waiting for its players
        ready = self.w.state.squadrons["blue_sq"].aircraft_ready
        self.s.on_event({"kind": "birth", "unit": {"name": "Uzi 4-1", "group": "Uzi 4 BAI", "player": "Viper"}})
        self.assertEqual((self.f.spawn, self.f.state, self.f.dcs_group), (SpawnState.SPAWNED, FlightState.TAXI, "Uzi 4 BAI"))
        self.assertEqual(self.w.state.squadrons["blue_sq"].aircraft_ready, ready - 2)
        self.e.air.on_event({"kind": "takeoff", "unit": {"name": "Uzi 4-1"}})
        self.assertEqual(self.f.state, FlightState.ENROUTE)
        self.assertEqual(bubble.plan().aggregate, [])   # never taken out of DCS
        # Telemetry by the group's own name.
        self.e.air.on_telemetry({"groups": [{"id": "Uzi 4 BAI", "pos": {"x": 5.0, "y": -40_000.0}, "alt": 7000}]})
        self.assertEqual(self.f.pos, Pos(5.0, -40_000.0))
        # Landed and shut down: recovered without despawning the players' jets.
        for n in ("Uzi 4-1", "Uzi 4-2"):
            self.e.air.on_event({"kind": "land", "unit": {"name": n}})
            self.e.air.on_event({"kind": "engine_shutdown", "unit": {"name": n}})
        await self.e.air.tick()
        self.assertEqual(self.f.state, FlightState.LANDED)
        self.assertEqual(self.w.state.squadrons["blue_sq"].aircraft_ready, ready)
        self.assertFalse(any(op == "despawn_group" for _, op, _ in self.e.calls))
        await self.s.tick()
        self.assertFalse(self.f.committed)
        self.assertEqual(self.e.hooks("slot_rules")[-1]["slots"], {"41": {"reason": "Uzi 4 has flown its mission"}})

    async def test_players_leaving_in_the_air_are_home(self):
        await self.loaded()
        self.s.on_event({"kind": "birth", "unit": {"name": "Uzi 4-1", "group": "Uzi 4 BAI", "player": "Viper"}})
        self.e.air.on_event({"kind": "takeoff", "unit": {"name": "Uzi 4-1"}})
        ready = self.w.state.squadrons["blue_sq"].aircraft_ready
        for _ in range(3):
            self.e.air.on_telemetry({"groups": []})
        self.assertEqual(self.f.state, FlightState.LANDED)
        self.assertEqual(self.w.state.squadrons["blue_sq"].aircraft_ready, ready + 2)
        self.assertEqual(self.f.spawn, SpawnState.AGGREGATED)

    async def test_nobody_in_by_the_deadline_the_ai_flies_it(self):
        await self.loaded()
        self.w.state.clock = self.f.takeoff_time + LATE_S + 1
        await self.s.tick()
        self.assertFalse(self.f.committed)
        self.assertEqual(self.e.hooks("slot_rules")[-1]["slots"], {"41": {"reason": "Uzi 4 has gone to the AI"}})
        self.assertIn("nobody got in by 02:45", self.e.newsl[0])

    async def test_a_free_seat_taken_from_the_slot_list_is_theirs(self):
        self.f.player_seats = {1: "abc"}
        self.f.aircraft = 2
        self.f.player_seats[2] = "other"
        await self.commit()
        self.e.client_groups = [{"name": "Uzi 4 BAI", "units": [{"name": "Uzi 4-1", "id": "41", "skill": "Client"},
                                                                {"name": "Uzi 4-2", "id": "42", "skill": "Client"}]}]
        await self.s.on_mission()
        self.f.player_seats[2] = None   # its player left: seat 2 is free
        self.s.on_player({"kind": "player_slot", "ucid": "new", "name": "Ice", "slot": "42"})
        self.assertEqual(self.f.player_seats[2], "new")
        self.s.on_player({"kind": "player_slot", "ucid": "thief", "slot": "41"})   # held: not theirs
        self.assertEqual(self.f.player_seats[1], "abc")

    async def test_the_map_buttons(self):
        m = self.e.warmap
        m.ucids[1] = "abc"
        spec = m.spec(show=True, keep_view=False, player_id=1)
        self.assertEqual(spec["actions"]["f:bai2"],
                         {"label": "Commit Uzi 4 (restarts the mission)", "key": "commit:f:bai2", "ok": True})
        await self.loaded()
        spec = m.spec(show=True, keep_view=False, player_id=1)
        self.assertEqual(spec["actions"]["f:bai2"], {"label": "Fly Uzi 4-1 from 02:15", "key": "fly:f:bai2", "ok": False,
                                                     "opens_in": 600, "ready": "Fly Uzi 4-1"})
        self.assertEqual(spec["joins"]["f:bai2"]["label"], "Leave Uzi 4 (you're Uzi 4-1)")
        info = next(b for b in spec["flights"] if b["select"] == "f:bai2")["info"]
        self.assertIn("1 of 2 seats taken, committed", info)
        self.assertIn("AI: Uzi 4-2", info)
        # A wingman who isn't the lead waits for the lead's commit.
        self.f.committed, self.f.player_seats = False, {1: "lead", 2: "abc"}
        spec = m.spec(show=True, keep_view=False, player_id=1)
        self.assertEqual(spec["actions"]["f:bai2"], {"label": "Waiting for Uzi 4-1 to commit", "key": "", "ok": False})


if __name__ == "__main__":
    unittest.main()
