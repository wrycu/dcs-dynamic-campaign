"""Wire protocol between the Python engine and the DCS-side Lua (mission + Hooks).

Transport: TCP on localhost, one JSON object per line (UTF-8, ``\\n``-terminated).
Python is the server; the mission bridge and the Hooks script each connect as a client.
See docs/protocol.md for the full specification.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTO_VERSION = 1


class ProtocolError(Exception):
    """A message could not be decoded or violates the protocol."""


# ---------- message types ----------

class MsgType:
    HELLO = "hello"            # client -> server, first message on every connection
    WELCOME = "welcome"        # server -> client, reply to hello (carries last_seq for replay)
    EVENT = "event"            # mission -> server
    TELEMETRY = "telemetry"    # mission -> server
    PLAYER = "player"          # hooks -> server
    CMD = "cmd"                # server -> client
    ACK = "ack"                # client -> server, reply to cmd


class Src:
    MISSION = "mission"
    HOOKS = "hooks"
    ENGINE = "engine"

    CLIENTS = (MISSION, HOOKS)


class Op:
    """Command operations (``cmd.data.op``)."""
    # mission
    SPAWN_GROUP = "spawn_group"
    DESPAWN_GROUP = "despawn_group"
    SPAWN_STATIC = "spawn_static"
    REMOVE_STATIC = "remove_static"
    SET_ROUTE = "set_route"
    SET_TASK = "set_task"
    DRAW = "draw"
    UNDRAW = "undraw"
    SMOKE = "smoke"
    MESSAGE = "message"
    MENU = "menu"
    WAREHOUSE_SET = "warehouse_set"
    QUERY = "query"
    # hooks
    SLOT_RULES = "slot_rules"


class EventKind:
    """Kinds carried in ``event.data.kind`` (mission) and ``player.data.kind`` (hooks)."""
    # flight lifecycle
    BIRTH = "birth"
    TAKEOFF = "takeoff"
    LAND = "land"
    ENGINE_SHUTDOWN = "engine_shutdown"
    # losses and damage
    UNIT_KILLED = "unit_killed"      # deduplicated DEAD / UNIT_LOST / CRASH
    UNIT_REMOVED = "unit_removed"    # removed by DCS because its player left it (not a kill)
    KILL = "kill"                    # kill credit (S_EVENT_KILL)
    HIT = "hit"                      # throttled; statics and scenery only
    SCENERY_DESTROYED = "scenery_destroyed"
    EJECTION = "ejection"
    PILOT_DEAD = "pilot_dead"
    LANDING_AFTER_EJECTION = "landing_after_ejection"
    # players (mission side, by name)
    PLAYER_ENTER_UNIT = "player_enter_unit"
    PLAYER_LEAVE_UNIT = "player_leave_unit"
    # UI
    MENU_SELECT = "menu_select"
    # hooks (by UCID)
    PLAYER_CONNECT = "player_connect"
    PLAYER_DISCONNECT = "player_disconnect"
    PLAYER_SLOT = "player_slot"
    SLOT_DENIED = "slot_denied"


# ---------- envelope ----------

@dataclass
class Envelope:
    type: str
    src: str
    seq: int = 0          # per-connection-direction counter; 0 = unsequenced (hello/welcome)
    t: float | None = None  # DCS model time (timer.getTime) for mission messages
    data: dict[str, Any] = field(default_factory=dict)
    id: str | None = None   # cmd id
    re: str | None = None   # ack: the cmd id being answered
    ok: bool | None = None  # ack
    result: Any = None      # ack (ok)
    error: str | None = None  # ack (not ok)
    v: int = PROTO_VERSION

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"v": self.v, "type": self.type, "src": self.src, "seq": self.seq}
        for k in ("t", "id", "re", "ok", "result", "error"):
            val = getattr(self, k)
            if val is not None:
                out[k] = val
        out["data"] = self.data
        return out


_KNOWN_TYPES = {v for k, v in vars(MsgType).items() if not k.startswith("_")}


def encode(env: Envelope) -> bytes:
    return json.dumps(env.to_json(), separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def decode(line: bytes | str) -> Envelope:
    try:
        raw = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ProtocolError(f"invalid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise ProtocolError("message is not an object")
    if raw.get("v") != PROTO_VERSION:
        raise ProtocolError(f"unsupported protocol version {raw.get('v')!r}")
    mtype = raw.get("type")
    if mtype not in _KNOWN_TYPES:
        raise ProtocolError(f"unknown message type {mtype!r}")
    src = raw.get("src")
    if not isinstance(src, str):
        raise ProtocolError("missing src")
    seq = raw.get("seq", 0)
    if not isinstance(seq, int) or seq < 0:
        raise ProtocolError(f"bad seq {seq!r}")
    data = raw.get("data")
    # DCS's lua2json encodes an empty Lua table as [], so accept it as an empty object.
    if data is None or data == []:
        data = {}
    if not isinstance(data, dict):
        raise ProtocolError("data is not an object")
    return Envelope(
        type=mtype, src=src, seq=seq, t=raw.get("t"), data=data,
        id=raw.get("id"), re=raw.get("re"), ok=raw.get("ok"),
        result=raw.get("result"), error=raw.get("error"), v=raw["v"],
    )


# ---------- payload helpers ----------

@dataclass
class Hello:
    run_id: str          # changes on every mission (or DCS process, for hooks) start
    map: str | None = None
    mission: str | None = None
    spawned: list[str] = field(default_factory=list)  # live campaign group names (mission only)

    @classmethod
    def from_envelope(cls, env: Envelope) -> Hello:
        d = env.data
        run_id = d.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ProtocolError("hello without run_id")
        spawned = as_list(d.get("spawned"))
        return cls(run_id=run_id, map=d.get("map"), mission=d.get("mission"), spawned=spawned)


def as_list(value: Any) -> list[Any]:
    """Normalize a list field from Lua: net.lua2json encodes an empty table as {}."""
    if value is None or value == {}:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and all(isinstance(k, str) and k.isdigit() for k in value):
        return [value[k] for k in sorted(value, key=int)]  # sparse Lua array
    raise ProtocolError(f"expected a list, got {type(value).__name__}")


# Audience strings for draw / message / menu:
#   "all" | "coalition:blue" | "coalition:red" | "coalition:neutral" | "group:<dcs group name>"
AUDIENCE_ALL = "all"


def audience_coalition(side: str) -> str:
    if side not in ("blue", "red", "neutral"):
        raise ValueError(side)
    return f"coalition:{side}"


def audience_group(group_name: str) -> str:
    return f"group:{group_name}"


# ---------- DCS naming ----------
# Campaign-spawned groups are named "<prefix>:<model id>", units "<prefix>:<model id>#<n>".
# Anything without a known prefix (player dynamic spawns, scenery, mission-editor objects) is foreign.

PREFIX_GROUND = "gu"
PREFIX_FLIGHT = "fl"
PREFIX_STATIC = "st"
_PREFIXES = {PREFIX_GROUND, PREFIX_FLIGHT, PREFIX_STATIC}


@dataclass(frozen=True)
class DcsName:
    prefix: str
    model_id: str
    unit_index: int | None = None


def group_name(prefix: str, model_id: str) -> str:
    if prefix not in _PREFIXES:
        raise ValueError(f"unknown prefix {prefix!r}")
    if not model_id or any(c in model_id for c in ":#"):
        raise ValueError(f"model id may not be empty or contain ':' or '#': {model_id!r}")
    return f"{prefix}:{model_id}"


def unit_name(prefix: str, model_id: str, index: int) -> str:
    return f"{group_name(prefix, model_id)}#{index}"


def parse_dcs_name(name: str | None) -> DcsName | None:
    """Return the campaign identity behind a DCS group/unit/static name, or None if foreign."""
    if not name:
        return None
    prefix, sep, rest = name.partition(":")
    if not sep or prefix not in _PREFIXES or not rest:
        return None
    model_id, hsep, idx = rest.partition("#")
    if not model_id:
        return None
    if not hsep:
        return DcsName(prefix, model_id)
    if not idx.isdigit():
        return None
    return DcsName(prefix, model_id, int(idx))
