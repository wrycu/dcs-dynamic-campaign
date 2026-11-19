"""Proof P4: rebuild the mission with the campaign's date, time and weather, and have the server
load it with nobody touching it. Runs next to DCS, which must be a server: a dedicated server, or
your own DCS hosting (MULTIPLAYER > New server); net.load_mission is server-only.

    python3 -m tools.restart_probe                      # your PC: your a2a.miz, repo via Z:
    python3 -m tools.restart_probe --template "$SSG/Missions/a2a.miz" --repo-path 'C:\\dcs-dynamic-campaign'

Start it, then host campaign-smoke.miz (or campaign-server.miz on the dedicated server). On each rebuild
(F10 > Other > Campaign > "Rebuild: +6 h, next weather", or every --every-min game minutes):

1. the campaign clock moves on --advance-h hours from where the mission is now;
2. a new .miz is built from the template with that date, start time and the next weather in
   the cycle, as campaign-next-a.miz / -b.miz (alternating) in the template's folder;
3. everyone gets a countdown, then the Hooks load the new mission (net.load_mission);
4. when the new mission's bridge connects, the probe asks DCS what it loaded and prints a
   CHECK line per item, and how long the reload took.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import time
from pathlib import Path
from typing import Any

from campaign.bridge import Bridge, BridgeError, Connection
from campaign.protocol import Envelope, Hello, Src
from tools.build_test_missions import DEFAULT_TEMPLATE, build

KT_PER_MS = 1.943844
WEATHERS: list[tuple[str, dict[str, Any]]] = [
    ("overcast with rain, surface wind 12 kt", {
        "clouds": {"preset": "RainyPreset1", "base": 1200},
        "wind": {"atGround": {"speed": 6, "dir": 270}, "at2000": {"speed": 12, "dir": 280},
                 "at8000": {"speed": 25, "dir": 290}},
        "season": {"temperature": 16}, "qnh": 752, "enable_fog": False}),
    ("scattered cloud and fog", {
        "clouds": {"preset": "Preset8", "base": 1800},
        "wind": {"atGround": {"speed": 2, "dir": 90}, "at2000": {"speed": 5, "dir": 100},
                 "at8000": {"speed": 15, "dir": 120}},
        "season": {"temperature": 22}, "qnh": 760, "enable_fog": True,
        "fog": {"thickness": 300, "visibility": 3000}}),
    ("clear and calm", {
        "clouds": {"preset": "Preset2", "base": 2500},
        "wind": {"atGround": {"speed": 0, "dir": 0}, "at2000": {"speed": 0, "dir": 0},
                 "at8000": {"speed": 0, "dir": 0}},
        "season": {"temperature": 30}, "qnh": 760, "enable_fog": False}),
]


def campaign_now(info: dict[str, Any]) -> dt.datetime:
    """The mission's date and time now: its start date plus timer.getAbsTime()."""
    d = info["date"]
    return dt.datetime(int(d["Year"]), int(d["Month"]), int(d["Day"])) + dt.timedelta(seconds=float(info["abs"]))


def next_mission(now: dt.datetime, advance_h: float, n: int) -> dict[str, Any]:
    when = now + dt.timedelta(hours=advance_h)
    when = when.replace(second=0, microsecond=0)
    label, weather = WEATHERS[n % len(WEATHERS)]
    return {"when": when, "label": label, "extra": {
        "date": {"Year": when.year, "Month": when.month, "Day": when.day},
        "start_time": when.hour * 3600 + when.minute * 60,
        "weather": weather}}


def checks(expected: dict[str, Any], info: dict[str, Any]) -> list[tuple[str, bool, str]]:
    """(item, ok, detail) comparing what was built with what DCS reports."""
    ex = expected["extra"]
    w = info.get("weather") or {}
    xw = ex["weather"]
    got_when = campaign_now(info)
    out = [
        ("date", info.get("date") == ex["date"], f"wanted {ex['date']}, DCS {info.get('date')}"),
        ("start time", info.get("start_time") == ex["start_time"],
         f"wanted {ex['start_time']} s, DCS {info.get('start_time')} s"),
        ("clock", abs((got_when - expected["when"]).total_seconds()) < 600,
         f"wanted about {expected['when']:%Y-%m-%d %H:%M}, DCS now {got_when:%Y-%m-%d %H:%M:%S}"),
        ("clouds", w.get("preset") == xw["clouds"]["preset"] and w.get("base") == xw["clouds"]["base"],
         f"wanted {xw['clouds']['preset']} base {xw['clouds']['base']} m, DCS {w.get('preset')} base {w.get('base')} m"),
        ("temperature", w.get("temperature") == xw["season"]["temperature"],
         f"wanted {xw['season']['temperature']} C, DCS {w.get('temperature')} C"),
        ("qnh", w.get("qnh") == xw["qnh"], f"wanted {xw['qnh']} mmHg, DCS {w.get('qnh')}"),
        ("fog", bool(w.get("fog")) == bool(xw.get("enable_fog")), f"wanted {xw.get('enable_fog')}, DCS {w.get('fog')}"),
    ]
    wg, xg = w.get("wind_ground") or {}, xw["wind"]["atGround"]
    out.append(("surface wind", wg.get("speed") == xg["speed"] and wg.get("dir") == xg["dir"],
                f"wanted {xg['dir']} deg {xg['speed'] * KT_PER_MS:.0f} kt, "
                f"DCS {wg.get('dir')} deg {float(wg.get('speed') or 0) * KT_PER_MS:.0f} kt"))
    return out


class Probe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.template: Path = args.template
        self.out_dir: Path = args.missions_dir or args.template.parent
        self.mission: Connection | None = None
        self.hooks: Connection | None = None
        self.rebuilds = 0
        self.expected: dict[str, Any] | None = None
        self.requested_at: float | None = None
        self.busy = False

    def ts(self) -> str:
        return f"+{time.monotonic() - self.requested_at:5.1f} s" if self.requested_at else "       "

    def on_hello(self, conn: Connection, hello: Hello, new_run: bool) -> None:
        print(f"HELLO   {conn.src} run={hello.run_id} new_run={new_run} {self.ts()}", flush=True)
        if conn.src == Src.HOOKS:
            self.hooks = conn
        elif conn.src == Src.MISSION:
            self.mission = conn
            if new_run:
                asyncio.create_task(self.on_mission(conn))

    def on_disconnect(self, conn: Connection) -> None:
        print(f"DISCONNECT {conn.src} {self.ts()}", flush=True)
        if conn is self.mission:
            self.mission = None
        if conn is self.hooks:
            self.hooks = None

    def on_message(self, conn: Connection, env: Envelope) -> None:
        if env.type not in ("event", "player"):
            return
        kind = env.data.get("kind")
        if kind == "mission_load":
            d = env.data
            print(f"LOAD    {d.get('stage'):9} {self.ts()} {d.get('file') or d.get('path') or ''}"
                  f"{'' if d.get('ok', True) else ' FAILED ' + str(d.get('error'))}", flush=True)
        elif kind == "menu_select" and env.data.get("key") == "rebuild":
            asyncio.create_task(self.rebuild())

    async def on_mission(self, conn: Connection) -> None:
        try:
            info = await conn.call("query", {"what": "mission"})
            await conn.call("menu", {"audience": "all", "items": [{"label": "Rebuild: +6 h, next weather",
                                                                   "key": "rebuild"}]})
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"mission query failed: {e}", flush=True)
            return
        now = campaign_now(info)
        w = info.get("weather") or {}
        print(f"MISSION {info.get('theatre')} {now:%Y-%m-%d %H:%M:%S} | clouds {w.get('preset')} base {w.get('base')} m"
              f" | {w.get('temperature')} C | qnh {w.get('qnh')} | fog {w.get('fog')} | wind {w.get('wind_ground')}",
              flush=True)
        if self.expected:
            took = time.monotonic() - self.requested_at if self.requested_at else None
            results = checks(self.expected, info)
            for item, ok, detail in results:
                print(f"CHECK   {'ok  ' if ok else 'FAIL'} {item:12} {detail}", flush=True)
            passed = sum(ok for _, ok, _ in results)
            took_txt = f"; load to mission running took {took:.0f} s" if took is not None else ""
            print(f"RELOAD  {self.rebuilds}: {passed}/{len(results)} checks ok{took_txt}", flush=True)
            self.expected, self.requested_at = None, None
        try:
            await conn.call("message", {"audience": "all", "duration": 20, "text":
                                        f"P4 restart probe. Campaign time {now:%d %b %Y %H:%M}. "
                                        "F10 > Other > Campaign > Rebuild to move on 6 hours."})
        except (BridgeError, asyncio.TimeoutError):
            pass
        if self.args.every_min > 0:
            asyncio.create_task(self.auto(conn))

    async def auto(self, conn: Connection) -> None:
        try:
            start = (await conn.call("query", {"what": "time"}))["model"]
            while self.mission is conn:
                await asyncio.sleep(5)
                t = (await conn.call("query", {"what": "time"}))["model"]
                if t - start >= self.args.every_min * 60:
                    await self.rebuild()
                    return
        except (BridgeError, asyncio.TimeoutError, KeyError, TypeError):
            return

    async def rebuild(self) -> None:
        if self.busy or not self.mission or not self.hooks:
            print("REBUILD skipped: busy, or the mission or Hooks aren't connected", flush=True)
            return
        self.busy = True
        try:
            info = await self.mission.call("query", {"what": "mission"})
            nxt = next_mission(campaign_now(info), self.args.advance_h, self.rebuilds)
            name = f"campaign-next-{'ab'[self.rebuilds % 2]}.miz"
            t = time.monotonic()
            path = await asyncio.to_thread(
                build, self.template, self.out_dir, name, "lua/mission/campaign_bridge.lua", "",
                "Load campaign bridge", self.args.repo_path, True, None, nxt["extra"])
            print(f"BUILD   {path} for {nxt['when']:%Y-%m-%d %H:%M}, {nxt['label']} "
                  f"({(time.monotonic() - t) * 1000:.0f} ms)", flush=True)
            self.rebuilds += 1
            self.expected = nxt
            await self.mission.call("message", {"audience": "all", "duration": self.args.countdown_s + 5, "text":
                                                f"Campaign: the mission restarts in {self.args.countdown_s} s "
                                                f"for {nxt['when']:%d %b %H:%M}, {nxt['label']}. Rejoin your slot "
                                                "after it loads."})
            res = await self.hooks.call("load_mission", {"file": name, "delay": self.args.countdown_s})
            self.requested_at = time.monotonic() + self.args.countdown_s
            print(f"REQUEST load {res.get('path') if isinstance(res, dict) else name} in {self.args.countdown_s} s",
                  flush=True)
        except (BridgeError, asyncio.TimeoutError, SystemExit) as e:
            print(f"REBUILD failed: {type(e).__name__}: {e}", flush=True)
            self.expected = None
        finally:
            self.busy = False


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE,
                    help="the template .miz, in DCS's Saved Games Missions folder (default: your a2a.miz)")
    ap.add_argument("--missions-dir", type=Path, help="where DCS's Saved Games Missions folder is "
                                                      "(default: the template's folder)")
    ap.add_argument("--repo-path", help="the repo as DCS sees it, for the bridge loader, if not this checkout "
                                        "through Wine's Z: drive (dedicated server: C:\\dcs-dynamic-campaign)")
    ap.add_argument("--advance-h", type=float, default=6.0, help="hours the campaign moves on per rebuild")
    ap.add_argument("--every-min", type=float, default=0.0, help="also rebuild every N game minutes; 0 = menu only")
    ap.add_argument("--countdown-s", type=int, default=15, help="warning before the load")
    ap.add_argument("--port", type=int, default=7341)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    probe = Probe(args)
    bridge = Bridge(probe.on_message, probe.on_hello, probe.on_disconnect, port=args.port)
    await bridge.start()
    print(f"listening on 127.0.0.1:{bridge.port}; host campaign-smoke.miz (MULTIPLAYER > New server)", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
