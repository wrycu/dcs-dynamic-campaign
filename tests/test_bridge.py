import asyncio
import unittest

from campaign.bridge import Bridge, CommandFailed, Disconnected, NotConnected
from campaign.protocol import Envelope, MsgType, Src, decode, encode

TIMEOUT = 2.0


class FakeClient:
    """Speaks the client side of the protocol, like the Lua bridge would."""

    def __init__(self, src: str = Src.MISSION):
        self.src = src
        self.reader: asyncio.StreamReader
        self.writer: asyncio.StreamWriter

    async def connect(self, port: int, run_id: str = "run-1", spawned=()) -> Envelope:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
        await self.send(MsgType.HELLO, {"run_id": run_id, "map": "Caucasus", "spawned": list(spawned)})
        return await self.recv()

    async def send(self, mtype: str, data=None, seq: int = 0, **extra) -> None:
        env = Envelope(type=mtype, src=self.src, seq=seq, data=data or {}, **extra)
        self.writer.write(encode(env))
        await self.writer.drain()

    async def recv(self) -> Envelope:
        line = await asyncio.wait_for(self.reader.readline(), TIMEOUT)
        if not line:
            raise ConnectionError("closed")
        return decode(line)

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.messages: list[Envelope] = []
        self.hellos: list[tuple[str, bool]] = []
        self.disconnects: list[str] = []
        self.got_message = asyncio.Event()
        self.bridge = Bridge(
            on_message=self.on_message,
            on_hello=lambda conn, hello, new_run: self.hellos.append((hello.run_id, new_run)),
            on_disconnect=lambda conn: self.disconnects.append(conn.src),
            port=0,
        )
        await self.bridge.start()

    async def asyncTearDown(self):
        await self.bridge.close()

    async def on_message(self, conn, env):
        if getattr(self, "gate", None) is not None:
            await self.gate.wait()
        self.messages.append(env)
        self.got_message.set()

    async def wait_for(self, predicate):
        async def loop():
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(loop(), TIMEOUT)

    async def test_run_state_counts_only_handled_messages(self):
        self.gate = asyncio.Event()
        c = FakeClient()
        await c.connect(self.bridge.port)
        await c.send(MsgType.EVENT, {"kind": "takeoff"}, seq=1)
        await asyncio.sleep(0.05)
        self.assertEqual(self.bridge.run_state(Src.MISSION), ("run-1", 0))  # received, handler still running
        self.gate.set()
        await self.wait_for(lambda: self.messages)
        await self.wait_for(lambda: self.bridge.run_state(Src.MISSION) == ("run-1", 1))
        await c.close()

    async def test_welcome_and_events_in_order(self):
        c = FakeClient()
        welcome = await c.connect(self.bridge.port)
        self.assertEqual(welcome.type, MsgType.WELCOME)
        self.assertEqual(welcome.data, {"last_seq": 0})
        for seq in (1, 2, 3):
            await c.send(MsgType.EVENT, {"kind": "takeoff", "n": seq}, seq=seq)
        await self.wait_for(lambda: len(self.messages) == 3)
        self.assertEqual([m.data["n"] for m in self.messages], [1, 2, 3])
        self.assertEqual(self.hellos, [("run-1", True)])
        self.assertTrue(self.bridge.connected(Src.MISSION))
        self.assertEqual(self.bridge.run_state(Src.MISSION), ("run-1", 3))
        await c.close()

    async def test_reconnect_same_run_dedupes_replay(self):
        c = FakeClient()
        await c.connect(self.bridge.port)
        await c.send(MsgType.EVENT, {"n": 1}, seq=1)
        await c.send(MsgType.EVENT, {"n": 2}, seq=2)
        await self.wait_for(lambda: len(self.messages) == 2)
        await c.close()
        await self.wait_for(lambda: self.disconnects == [Src.MISSION])

        c2 = FakeClient()
        welcome = await c2.connect(self.bridge.port)
        self.assertEqual(welcome.data, {"last_seq": 2})
        await c2.send(MsgType.EVENT, {"n": 2}, seq=2)  # replayed duplicate
        await c2.send(MsgType.EVENT, {"n": 3}, seq=3)
        await self.wait_for(lambda: len(self.messages) == 3)
        self.assertEqual([m.data["n"] for m in self.messages], [1, 2, 3])
        self.assertEqual(self.hellos, [("run-1", True), ("run-1", False)])
        await c2.close()

    async def test_new_run_resets_sequence(self):
        c = FakeClient()
        await c.connect(self.bridge.port, run_id="run-1")
        await c.send(MsgType.EVENT, {"n": 1}, seq=1)
        await self.wait_for(lambda: len(self.messages) == 1)
        await c.close()

        c2 = FakeClient()
        welcome = await c2.connect(self.bridge.port, run_id="run-2")
        self.assertEqual(welcome.data, {"last_seq": 0})
        await c2.send(MsgType.EVENT, {"n": 1}, seq=1)
        await self.wait_for(lambda: len(self.messages) == 2)
        self.assertEqual(self.hellos[-1], ("run-2", True))
        await c2.close()

    async def test_restore_run_after_engine_restart(self):
        self.bridge.restore_run(Src.MISSION, "run-1", 41)
        c = FakeClient()
        welcome = await c.connect(self.bridge.port, run_id="run-1")
        self.assertEqual(welcome.data, {"last_seq": 41})
        self.assertEqual(self.hellos, [("run-1", False)])
        await c.close()

    async def test_unsequenced_messages_are_never_deduped(self):
        c = FakeClient()
        await c.connect(self.bridge.port)
        await c.send(MsgType.TELEMETRY, {"n": 1})
        await c.send(MsgType.TELEMETRY, {"n": 2})
        await self.wait_for(lambda: len(self.messages) == 2)
        await c.close()

    async def test_call_ok_and_error(self):
        c = FakeClient()
        await c.connect(self.bridge.port)
        call = asyncio.create_task(self.bridge.call(Src.MISSION, "query", {"what": "time"}))
        cmd = await c.recv()
        self.assertEqual(cmd.type, MsgType.CMD)
        self.assertEqual(cmd.data, {"op": "query", "what": "time"})
        await c.send(MsgType.ACK, re=cmd.id, ok=True, result={"model": 12.5})
        self.assertEqual(await call, {"model": 12.5})

        call = asyncio.create_task(self.bridge.call(Src.MISSION, "despawn_group", {"group": "gu:x"}))
        cmd = await c.recv()
        await c.send(MsgType.ACK, re=cmd.id, ok=False, error="no such group")
        with self.assertRaisesRegex(CommandFailed, "no such group"):
            await call
        await c.close()

    async def test_call_timeout(self):
        c = FakeClient()
        await c.connect(self.bridge.port)
        with self.assertRaises(asyncio.TimeoutError):
            await self.bridge.call(Src.MISSION, "query", {"what": "time"}, timeout=0.1)
        await c.close()

    async def test_disconnect_fails_pending_call(self):
        c = FakeClient()
        await c.connect(self.bridge.port)
        call = asyncio.create_task(self.bridge.call(Src.MISSION, "query", {"what": "time"}))
        await c.recv()
        await c.close()
        with self.assertRaises(Disconnected):
            await asyncio.wait_for(call, TIMEOUT)
        self.assertFalse(self.bridge.connected(Src.MISSION))

    async def test_not_connected(self):
        with self.assertRaises(NotConnected):
            await self.bridge.call(Src.HOOKS, "slot_rules", {})

    async def test_handler_can_await_call(self):
        results = []

        async def on_message(conn, env):
            results.append(await conn.call("query", {"what": "group", "group": env.data["group"]}))

        self.bridge._on_message = on_message
        c = FakeClient()
        await c.connect(self.bridge.port)
        await c.send(MsgType.EVENT, {"kind": "birth", "group": "gu:a"}, seq=1)
        cmd = await c.recv()
        await c.send(MsgType.ACK, re=cmd.id, ok=True, result={"exists": True})
        await self.wait_for(lambda: results == [{"exists": True}])
        await c.close()

    async def test_mission_and_hooks_are_independent(self):
        m, h = FakeClient(Src.MISSION), FakeClient(Src.HOOKS)
        await m.connect(self.bridge.port, run_id="m-run")
        await h.connect(self.bridge.port, run_id="h-run")
        await h.send(MsgType.PLAYER, {"kind": "player_connect", "ucid": "u1"}, seq=1)
        await m.send(MsgType.EVENT, {"kind": "birth"}, seq=1)
        await self.wait_for(lambda: len(self.messages) == 2)
        self.assertEqual({e.src for e in self.messages}, {Src.MISSION, Src.HOOKS})

        call = asyncio.create_task(self.bridge.call(Src.HOOKS, "slot_rules", {"allowed_sides": [0, 2]}))
        cmd = await h.recv()
        self.assertEqual(cmd.data["op"], "slot_rules")
        await h.send(MsgType.ACK, re=cmd.id, ok=True)
        self.assertIsNone(await call)
        await m.close()
        await h.close()

    async def test_newer_connection_replaces_older(self):
        c1 = FakeClient()
        await c1.connect(self.bridge.port)
        c2 = FakeClient()
        await c2.connect(self.bridge.port)
        with self.assertRaises(ConnectionError):
            await c1.recv()
        self.assertTrue(self.bridge.connected(Src.MISSION))
        await c2.send(MsgType.EVENT, {"n": 1}, seq=1)
        await self.wait_for(lambda: len(self.messages) == 1)
        await c1.close()
        await c2.close()

    async def test_first_message_must_be_hello(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.bridge.port)
        writer.write(encode(Envelope(type=MsgType.EVENT, src=Src.MISSION, seq=1)))
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), TIMEOUT), b"")
        self.assertFalse(self.bridge.connected(Src.MISSION))
        writer.close()

    async def test_bad_line_is_skipped(self):
        c = FakeClient()
        await c.connect(self.bridge.port)
        c.writer.write(b"garbage\n")
        await c.send(MsgType.EVENT, {"n": 1}, seq=1)
        await self.wait_for(lambda: len(self.messages) == 1)
        await c.close()


if __name__ == "__main__":
    unittest.main()
