"""Asyncio TCP server that the DCS mission bridge and Hooks script connect to.

- One live connection per source ("mission", "hooks"); a newer connection replaces an older one.
- The first message on a connection must be ``hello``; the server answers ``welcome`` with the last
  sequence number it has seen for that run, so the client can replay anything that was lost.
- Sequenced messages at or below the last seen seq are dropped (replay dedupe).
- ``ack`` messages are resolved inside the read loop; everything else is handed to the engine
  through a per-connection dispatcher task, so a handler may ``await bridge.call(...)`` safely.
"""
from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .protocol import (
    Envelope, Hello, MsgType, ProtocolError, Src, decode, encode,
)

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7341
MAX_LINE = 16 * 1024 * 1024


class BridgeError(Exception):
    pass


class NotConnected(BridgeError):
    """No client of the requested source is connected."""


class Disconnected(BridgeError):
    """The connection closed before the command was acknowledged."""


class CommandFailed(BridgeError):
    """The client acknowledged the command with ok=false."""


MessageHandler = Callable[["Connection", Envelope], Awaitable[None] | None]
HelloHandler = Callable[["Connection", Hello, bool], Awaitable[None] | None]
DisconnectHandler = Callable[["Connection"], Awaitable[None] | None]


@dataclass
class _RunState:
    run_id: str
    last_seq: int = 0   # highest seq received (dedupe, and what welcome reports)
    applied: int = 0    # highest seq whose handler has finished (what a save may claim)


async def _maybe_await(value: Any) -> None:
    if inspect.isawaitable(value):
        await value


class Connection:
    def __init__(self, bridge: Bridge, src: str, hello: Hello,
                 writer: asyncio.StreamWriter) -> None:
        self.bridge = bridge
        self.src = src
        self.hello = hello
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._out_seq = itertools.count(1)
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self.closed = False

    @property
    def run_id(self) -> str:
        return self.hello.run_id

    async def send(self, env: Envelope) -> None:
        if self.closed:
            raise Disconnected(f"{self.src} connection is closed")
        async with self._write_lock:
            self._writer.write(encode(env))
            await self._writer.drain()

    async def call(self, op: str, data: dict[str, Any] | None = None,
                   timeout: float = 10.0) -> Any:
        cmd_id = f"c{next(self.bridge._cmd_ids)}"
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = fut
        env = Envelope(type=MsgType.CMD, src=Src.ENGINE, seq=next(self._out_seq), id=cmd_id,
                       data={"op": op, **(data or {})})
        try:
            await self.send(env)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(cmd_id, None)

    def _resolve_ack(self, env: Envelope) -> None:
        fut = self._pending.get(env.re or "")
        if fut is None or fut.done():
            log.debug("ack for unknown or finished command %r", env.re)
            return
        if env.ok:
            fut.set_result(env.result)
        else:
            fut.set_exception(CommandFailed(env.error or "command failed"))

    def _fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(Disconnected(f"{self.src} disconnected"))
        self._pending.clear()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._fail_pending()
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass


_CLOSE = object()


class Bridge:
    def __init__(self, on_message: MessageHandler,
                 on_hello: HelloHandler | None = None,
                 on_disconnect: DisconnectHandler | None = None,
                 host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self._on_message = on_message
        self._on_hello = on_hello
        self._on_disconnect = on_disconnect
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None
        self._cmd_ids = itertools.count(1)
        self._runs: dict[str, _RunState] = {}
        self.connections: dict[str, Connection] = {}
        self._client_tasks: set[asyncio.Task[None]] = set()

    @property
    def port(self) -> int:
        """The bound port (useful when constructed with port=0)."""
        if self._server is None or not self._server.sockets:
            return self._port
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._client_entry, self._host, self._port, limit=MAX_LINE)
        log.info("bridge listening on %s:%d", self._host, self.port)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for conn in list(self.connections.values()):
            await conn.close()
        for task in list(self._client_tasks):
            task.cancel()
        await asyncio.gather(*self._client_tasks, return_exceptions=True)

    def run_state(self, src: str) -> tuple[str, int] | None:
        """(run_id, seq) for a source: the last sequenced message whose handler has finished.

        Persist it with the campaign snapshot. Messages received but not yet handled are not
        included, so after a restart the client replays them.
        """
        run = self._runs.get(src)
        return (run.run_id, run.applied) if run else None

    def restore_run(self, src: str, run_id: str, last_seq: int) -> None:
        """Seed run state after an engine restart, before clients reconnect.

        Without this, a client resuming the same run would replay messages whose effects
        are already in the restored snapshot.
        """
        self._runs[src] = _RunState(run_id, last_seq, last_seq)

    def connected(self, src: str) -> bool:
        conn = self.connections.get(src)
        return conn is not None and not conn.closed

    async def call(self, src: str, op: str, data: dict[str, Any] | None = None,
                   timeout: float = 10.0) -> Any:
        conn = self.connections.get(src)
        if conn is None or conn.closed:
            raise NotConnected(src)
        return await conn.call(op, data, timeout)

    # ---------- connection handling ----------

    async def _client_entry(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._client_tasks.add(task)
        try:
            await self._serve(reader, writer)
        finally:
            self._client_tasks.discard(task)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            first = await reader.readline()
            env = decode(first)
            if env.type != MsgType.HELLO or env.src not in Src.CLIENTS:
                raise ProtocolError("first message must be hello from mission or hooks")
            hello = Hello.from_envelope(env)
        except (ProtocolError, ValueError, ConnectionError) as e:
            log.warning("rejecting client %s: %s", peer, e)
            writer.close()
            return

        src = env.src
        old = self.connections.get(src)
        if old is not None:
            log.info("%s reconnected; closing previous connection", src)
            await old.close()

        run = self._runs.get(src)
        new_run = run is None or run.run_id != hello.run_id
        if new_run:
            run = self._runs[src] = _RunState(hello.run_id)
        assert run is not None

        conn = Connection(self, src, hello, writer)
        self.connections[src] = conn
        queue: asyncio.Queue[Any] = asyncio.Queue()
        dispatcher = asyncio.create_task(self._dispatch(conn, queue))
        log.info("%s connected from %s (run %s, %s)", src, peer, hello.run_id,
                 "new run" if new_run else f"resuming after seq {run.last_seq}")

        try:
            await conn.send(Envelope(type=MsgType.WELCOME, src=Src.ENGINE,
                                     data={"last_seq": run.last_seq}))
            queue.put_nowait(("hello", hello, new_run))
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    log.error("%s sent a line longer than %d bytes; dropping connection",
                              src, MAX_LINE)
                    break
                if not line:
                    break
                try:
                    msg = decode(line)
                except ProtocolError as e:
                    log.warning("bad message from %s: %s", src, e)
                    continue
                if msg.type == MsgType.ACK:
                    conn._resolve_ack(msg)
                    continue
                if msg.seq:
                    if msg.seq <= run.last_seq:
                        continue  # replayed duplicate
                    if msg.seq != run.last_seq + 1:
                        log.warning("%s seq gap: expected %d, got %d",
                                    src, run.last_seq + 1, msg.seq)
                    run.last_seq = msg.seq
                queue.put_nowait(("msg", msg, run))
        except (ConnectionError, OSError) as e:
            log.info("%s connection error: %s", src, e)
        finally:
            await conn.close()
            if self.connections.get(src) is conn:
                del self.connections[src]
            queue.put_nowait(_CLOSE)
            await dispatcher
            log.info("%s disconnected", src)

    async def _dispatch(self, conn: Connection, queue: asyncio.Queue[Any]) -> None:
        while True:
            item = await queue.get()
            try:
                if item is _CLOSE:
                    if self._on_disconnect:
                        await _maybe_await(self._on_disconnect(conn))
                    return
                if item[0] == "hello":
                    if self._on_hello:
                        await _maybe_await(self._on_hello(conn, item[1], item[2]))
                else:
                    try:
                        await _maybe_await(self._on_message(conn, item[1]))
                    finally:
                        if item[1].seq:  # a failed handler is not retried either way
                            item[2].applied = max(item[2].applied, item[1].seq)
            except Exception:
                log.exception("handler failed for %s message", conn.src)
