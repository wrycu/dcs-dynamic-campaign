"""End-to-end smoke test: run the real bridge and exercise the DCS-side Lua.

    python3 -m tools.smoke_server [--airbase Khalkhalah] [--host 127.0.0.1] [--port 7341]

Then start build/campaign-smoke.miz (or any mission that loads lua/mission/campaign_bridge.lua).
As soon as the mission connects, this script:
  1. queries model time and the airbase position,
  2. sends a message to everyone,
  3. draws a red circle around the airbase on the F10 map,
  4. adds an F10 menu "Campaign > Ping" (pick it; a menu_select event should print),
  5. spawns two ground vehicles 600 m north of the airbase,
  6. despawns them after 20 s and prints the reported survivors.
All traffic from DCS is printed, so it also shows the JSON coming out of net.lua2json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging

from campaign.bridge import Bridge, BridgeError, Connection
from campaign.protocol import Envelope, Hello, Src, group_name, unit_name


class Smoke:
    def __init__(self, airbase: str) -> None:
        self.airbase = airbase

    def on_hello(self, conn: Connection, hello: Hello, new_run: bool) -> None:
        print(f"HELLO {conn.src} run={hello.run_id} new_run={new_run} map={hello.map} spawned={hello.spawned}",
              flush=True)
        if conn.src == Src.MISSION and new_run:
            asyncio.create_task(self.scenario(conn))
        if conn.src == Src.HOOKS:
            asyncio.create_task(self.set_slot_rules(conn, [0], "SLOT TEST: blue is now BLOCKED. "
                                                    "Pick a blue slot; it should be refused with a chat message."))

    def on_disconnect(self, conn: Connection) -> None:
        print(f"DISCONNECT {conn.src}", flush=True)

    def on_message(self, conn: Connection, env: Envelope) -> None:
        print(f"{env.src:7} {env.type:9} seq={env.seq:<4} {json.dumps(env.data)[:300]}", flush=True)
        if env.src == Src.HOOKS and env.data.get("kind") == "slot_denied":
            asyncio.create_task(self.set_slot_rules(conn, [0, 2], "SLOT TEST: denial worked; blue is ALLOWED "
                                                    "again. Pick the blue slot; expect a player_slot line."))

    async def set_slot_rules(self, conn: Connection, sides: list[int], note: str) -> None:
        try:
            await conn.call("slot_rules", {"allowed_sides": sides, "deny": {}})
            print(note, flush=True)
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"slot_rules FAILED: {type(e).__name__}: {e}", flush=True)

    async def scenario(self, conn: Connection) -> None:
        try:
            print("RESULT query time:", await conn.call("query", {"what": "time"}), flush=True)
            ab = await conn.call("query", {"what": "airbase", "airbase": self.airbase})
            print("RESULT query airbase:", ab, flush=True)
            if not ab or not ab.get("exists"):
                print(f"airbase {self.airbase!r} not found; pass --airbase", flush=True)
                return
            pos = ab["pos"]
            await conn.call("message", {"audience": "all", "text": "Campaign smoke test connected", "duration": 15})
            await conn.call("draw", {"id": 9001, "audience": "all", "shape": "circle", "center": pos,
                                     "radius": 3000, "color": [1, 0, 0, 1], "fill": [1, 0, 0, 0.15],
                                     "message": "smoke test"})
            await conn.call("menu", {"audience": "all", "items": [{"label": "Ping", "key": "ping"}]})

            gid = "smoke1"
            name = group_name("gu", gid)
            north = {"x": pos["x"] + 600, "y": pos["y"]}
            units = [
                {"name": unit_name("gu", gid, i + 1), "type": t, "x": north["x"] + 20 * i, "y": north["y"],
                 "heading": 0, "skill": "Average"}
                for i, t in enumerate(["M-1 Abrams", "Hummer"])
            ]
            spawned = await conn.call("spawn_group", {
                "category": "ground", "country": "USA",
                "group": {"name": name, "task": "Ground Nothing", "units": units},
            })
            print("RESULT spawn_group:", spawned, flush=True)
            await conn.call("draw", {"id": 9002, "audience": "all", "shape": "mark", "pos": north,
                                     "text": "smoke test group"})
            await asyncio.sleep(20)
            print("RESULT despawn_group:", await conn.call("despawn_group", {"group": name}), flush=True)
            await conn.call("undraw", {"ids": [9001, 9002]})
            print("SCENARIO DONE (menu stays; pick F10 > Other > Campaign > Ping to test menu_select)", flush=True)
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"SCENARIO FAILED: {type(e).__name__}: {e}", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--airbase", default="Khalkhalah")
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 if DCS runs on another machine")
    ap.add_argument("--port", type=int, default=7341)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    smoke = Smoke(args.airbase)
    bridge = Bridge(smoke.on_message, smoke.on_hello, smoke.on_disconnect, host=args.host, port=args.port)
    await bridge.start()
    print(f"listening on {args.host}:{bridge.port}; waiting for DCS", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
