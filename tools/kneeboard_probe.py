"""Proof P5: how kneeboard pages reach the cockpit. Host the mission from your DCS
(MULTIPLAYER > New server), so you can leave your slot and take it again without restarting.

    python3 -m tools.kneeboard_probe

Two routes are tested:

- Pages built into the mission. The probe builds campaign-dev/campaign-kneeboard.miz with a page
  for every aircraft (KNEEBOARD/IMAGES) and one for the FA-18C only (KNEEBOARD/FA-18C_hornet/IMAGES).
- Pages in your Saved Games Kneeboard folder, which DCS scans when a cockpit is created. The probe
  writes "P5 PAGE A, version 1" into Kneeboard/FA-18C_hornet before you start. During the mission,
  F10 > Other > Campaign can rewrite page A (version 2, 3, ...) and add page B. Do they show at
  once, or only after you leave the slot and take it again?

Every page says what it is and when it was written. The probe removes its pages from your
Kneeboard folder when it stops (Ctrl+C).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import io
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from campaign.bridge import Bridge, BridgeError, Connection
from campaign.protocol import Envelope, Hello, Src
from tools.build_test_missions import DEFAULT_TEMPLATE, SAVED_GAMES, build

HORNET = "FA-18C_hornet"
W, H = 768, 1024
PREFIX = "campaign_p5_"
MENU = [{"label": "Kneeboard: rewrite page A (next version)", "key": "kb_update"},
        {"label": "Kneeboard: add page B", "key": "kb_add"}]


def page(title: str, lines: list[str], color: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (W, H), (245, 242, 230))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W - 1, 150], fill=color)
    d.text((30, 40), title, fill=(255, 255, 255), font=ImageFont.load_default(size=64))
    body = ImageFont.load_default(size=40)
    for i, line in enumerate(lines):
        d.text((30, 200 + i * 70), line, fill=(20, 20, 20), font=body)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def stamp() -> str:
    return f"written {dt.datetime.now():%H:%M:%S}"


class Probe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.folder: Path = args.kneeboard_dir / HORNET
        self.version = 0
        self.written: list[Path] = []

    def write(self, name: str, data: bytes) -> Path:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{PREFIX}{name}.png"
        path.write_bytes(data)
        if path not in self.written:
            self.written.append(path)
        return path

    def write_a(self) -> None:
        self.version += 1
        path = self.write("a", page("P5 PAGE A", [f"version {self.version}", "from the Kneeboard folder",
                                                  stamp()], (40, 90, 160)))
        print(f"FOLDER  page A version {self.version} -> {path}", flush=True)

    def cleanup(self) -> None:
        for p in self.written:
            p.unlink(missing_ok=True)
        if self.written:
            print(f"CLEANUP removed {len(self.written)} test page(s) from {self.folder}", flush=True)

    def build_mission(self) -> Path:
        files = {
            "KNEEBOARD/IMAGES/p5_mission_all.png":
                page("P5 MISSION", ["every aircraft", "built into the .miz", "KNEEBOARD/IMAGES", stamp()],
                     (150, 60, 40)),
            f"KNEEBOARD/{HORNET}/IMAGES/p5_mission_hornet.png":
                page("P5 MISSION F/A-18C", ["this type only", "built into the .miz", f"KNEEBOARD/{HORNET}/IMAGES",
                                            stamp()], (40, 130, 60)),
        }
        out = self.args.missions_dir
        path = build(self.args.template, out, "campaign-kneeboard.miz", "lua/mission/campaign_bridge.lua", "",
                     "Load campaign bridge", None, True, None, None, files)
        print(f"MISSION {path}: 2 pages built in", flush=True)
        return path

    def on_hello(self, conn: Connection, hello: Hello, new_run: bool) -> None:
        print(f"HELLO   {conn.src} new_run={new_run}", flush=True)
        if conn.src == Src.MISSION and new_run:
            asyncio.create_task(self.setup(conn))

    def on_disconnect(self, conn: Connection) -> None:
        print(f"DISCONNECT {conn.src}", flush=True)

    def on_message(self, conn: Connection, env: Envelope) -> None:
        if env.type not in ("event", "player"):
            return
        kind = env.data.get("kind")
        if kind == "menu_select":
            key = env.data.get("key")
            if key == "kb_update":
                self.write_a()
            elif key == "kb_add":
                path = self.write("b", page("P5 PAGE B", ["added during the mission", "from the Kneeboard folder",
                                                          stamp()], (120, 40, 140)))
                print(f"FOLDER  page B -> {path}", flush=True)
            asyncio.create_task(self.say(conn, f"Kneeboard: page A is version {self.version}"
                                               f"{', page B written' if key == 'kb_add' else ''}. "
                                               "Check now, then leave the slot and take it again."))
        elif kind in ("player_slot", "player_enter_unit", "birth") and (
                (env.data.get("unit") or {}).get("player") or env.data.get("ucid")):
            unit = env.data.get("unit") or {}
            print(f"EVENT   {kind} {unit.get('type') or env.data.get('unit_type') or ''}", flush=True)

    async def say(self, conn: Connection, text: str) -> None:
        try:
            await conn.call("message", {"audience": "all", "text": text, "duration": 15})
        except (BridgeError, asyncio.TimeoutError):
            pass

    async def setup(self, conn: Connection) -> None:
        try:
            await conn.call("menu", {"audience": "all", "items": MENU})
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"menu failed: {e}", flush=True)
        await self.say(conn, f"P5 kneeboard probe. Page A is version {self.version}. F10 > Other > Campaign.")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    ap.add_argument("--missions-dir", type=Path, default=SAVED_GAMES / "Missions" / "campaign-dev")
    ap.add_argument("--kneeboard-dir", type=Path, default=SAVED_GAMES / "Kneeboard")
    ap.add_argument("--port", type=int, default=7341)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    probe = Probe(args)
    probe.build_mission()
    probe.write_a()
    bridge = Bridge(probe.on_message, probe.on_hello, probe.on_disconnect, port=args.port)
    try:
        await bridge.start()
        print(f"listening on 127.0.0.1:{bridge.port}; host campaign-dev/campaign-kneeboard.miz "
              "(MULTIPLAYER > New server)", flush=True)
        await asyncio.Event().wait()
    finally:
        probe.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
