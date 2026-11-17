"""Save and load the campaign.

A save file is one JSON document:

    {"format": 1, "saved_at": "<UTC ISO time>",
     "state":  CampaignState,                       the campaign itself
     "runs":   {src: [run_id, last_seq]},           Bridge.run_state() per client
     "extra":  {name: ...}}                         spawner bookkeeping and tool-specific data

The state is encoded field by field from the dataclass type hints, so model changes need no codec
changes: enums by name, ``Pos`` as ``{"x", "y"}``, sets as sorted lists, and dict keys as strings
(enum names, or numbers as text). A field that is missing from an older save takes its default.

``runs`` is what makes an engine restart seamless. The mission's last_seq is saved together with
the state it produced, so after ``Bridge.restore_run`` the mission replays exactly the events that
arrived after the save (it keeps the last 2000), and none that the state already includes.

Writes are atomic (temp file, fsync, rename) and the previous saves are kept as ``<name>.1`` ..
``<name>.<keep>``.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import types
import typing
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .model import CampaignState

FORMAT = 1
KEEP = 5


class SaveError(Exception):
    pass


# ---------- codec ----------

def encode(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: encode(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.name
    if isinstance(obj, dict):
        return {_encode_key(k): encode(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted(encode(v) for v in obj)
    if isinstance(obj, (list, tuple)):
        return [encode(v) for v in obj]
    if obj is None or isinstance(obj, (str, bool, int, float)):
        return obj
    raise SaveError(f"can't save a {type(obj).__name__}")


def _encode_key(k: Any) -> str:
    if isinstance(k, Enum):
        return k.name
    if isinstance(k, (str, int)):
        return str(k)
    raise SaveError(f"can't save a dict key of type {type(k).__name__}")


def decode(tp: Any, raw: Any, where: str = "state") -> Any:
    """Rebuild a value of type ``tp`` from ``encode`` output."""
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = typing.get_args(tp)
        if raw is None and type(None) in args:
            return None
        inner = [a for a in args if a is not type(None)]
        if len(inner) != 1:
            raise SaveError(f"{where}: unsupported type {tp}")
        return decode(inner[0], raw, where)
    if tp is Any:
        return raw
    if origin is list:
        (item,) = typing.get_args(tp) or (Any,)
        return [decode(item, v, f"{where}[{i}]") for i, v in enumerate(_expect(raw, list, where))]
    if origin in (set, frozenset):
        (item,) = typing.get_args(tp) or (Any,)
        return origin(decode(item, v, where) for v in _expect(raw, list, where))
    if origin is tuple:
        args = typing.get_args(tp)
        return tuple(decode(a, v, where) for a, v in zip(args, _expect(raw, list, where)))
    if origin is dict or tp is dict:
        kt, vt = typing.get_args(tp) or (str, Any)
        return {_decode_key(kt, k, where): decode(vt, v, f"{where}.{k}")
                for k, v in _expect(raw, dict, where).items()}
    if isinstance(tp, type) and issubclass(tp, Enum):
        try:
            return tp[raw]
        except KeyError:
            raise SaveError(f"{where}: {raw!r} is not a {tp.__name__}") from None
    if dataclasses.is_dataclass(tp):
        data = _expect(raw, dict, where)
        hints = typing.get_type_hints(tp)
        kwargs = {}
        for f in dataclasses.fields(tp):
            if f.name in data:
                kwargs[f.name] = decode(hints[f.name], data[f.name], f"{where}.{f.name}")
            elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                raise SaveError(f"{where}: missing {f.name}")
        return tp(**kwargs)
    if tp is float:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise SaveError(f"{where}: expected a number, got {raw!r}")
        return float(raw)
    if tp in (int, str, bool):
        if not isinstance(raw, tp) or (tp is int and isinstance(raw, bool)):
            raise SaveError(f"{where}: expected {tp.__name__}, got {raw!r}")
        return raw
    raise SaveError(f"{where}: unsupported type {tp}")


def _decode_key(kt: Any, k: str, where: str) -> Any:
    if isinstance(kt, type) and issubclass(kt, Enum):
        return decode(kt, k, where)
    if kt is int:
        try:
            return int(k)
        except ValueError:
            raise SaveError(f"{where}: dict key {k!r} is not a number") from None
    return k


def _expect(raw: Any, tp: type, where: str) -> Any:
    if not isinstance(raw, tp):
        raise SaveError(f"{where}: expected {tp.__name__}, got {type(raw).__name__}")
    return raw


# ---------- files ----------

@dataclass
class Snapshot:
    state: CampaignState
    runs: dict[str, tuple[str, int]] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    saved_at: str = ""


def dumps(snap: Snapshot) -> str:
    doc = {"format": FORMAT, "saved_at": snap.saved_at or _now(), "state": encode(snap.state),
           "runs": {src: [run_id, seq] for src, (run_id, seq) in snap.runs.items()},
           "extra": encode(snap.extra)}
    return json.dumps(doc, indent=1, allow_nan=False)


def loads(text: str) -> Snapshot:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise SaveError(f"not valid JSON: {e}") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise SaveError(f"unsupported save format {doc.get('format') if isinstance(doc, dict) else '?'}")
    runs = {}
    for src, v in _expect(doc.get("runs", {}), dict, "runs").items():
        if not (isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int)):
            raise SaveError(f"runs.{src}: expected [run_id, last_seq]")
        runs[src] = (v[0], v[1])
    return Snapshot(state=decode(CampaignState, doc.get("state")), runs=runs,
                    extra=_expect(doc.get("extra", {}), dict, "extra"), saved_at=str(doc.get("saved_at", "")))


def save(path: Path, snap: Snapshot, keep: int = KEEP) -> None:
    """Write ``snap`` to ``path`` atomically, keeping the previous ``keep`` saves."""
    text = dumps(snap)  # encode first: a bad state must not touch the files
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    if path.exists() and keep > 0:
        for i in range(keep - 1, 0, -1):
            older = path.with_name(f"{path.name}.{i}")
            if older.exists():
                os.replace(older, path.with_name(f"{path.name}.{i + 1}"))
        os.replace(path, path.with_name(f"{path.name}.1"))
    os.replace(tmp, path)


class Autosave:
    """Save ``snapshot()`` to ``path`` whenever it differs from the last save.

    Call ``maybe_save()`` as often as you like (each call encodes the state, which is cheap at
    campaign scale), and ``save_now()`` on shutdown.
    """

    def __init__(self, path: Path, snapshot: typing.Callable[[], Snapshot], keep: int = KEEP) -> None:
        self.path = path
        self.snapshot = snapshot
        self.keep = keep
        self._last: str | None = None

    def maybe_save(self) -> bool:
        snap = self.snapshot()
        snap.saved_at = "-"  # compare content only
        key = dumps(snap)
        if key == self._last:
            return False
        snap.saved_at = ""
        save(self.path, snap, self.keep)
        self._last = key
        return True

    def save_now(self) -> None:
        self._last = None
        self.maybe_save()


def load(path: Path) -> Snapshot:
    try:
        return loads(path.read_text(encoding="utf-8"))
    except SaveError as e:
        raise SaveError(f"{path}: {e}") from None


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
