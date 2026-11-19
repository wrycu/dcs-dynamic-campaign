"""Proof P2: dynamic slots on a dedicated server. Pair with build/campaign-slots.miz.

    python3 -m tools.slots_probe

The mission has dynamic slots at three blue airbases. This script sets their Hornet stock and
rules, and logs everything that tells us how DCS handles a player picking an aircraft there:

  Khalkhalah     4 Hornets: the normal case
  Tiyas          2 Hornets, but the Hooks refuse slots at Tiyas: does refusing work for dynamic slots?
  Marj Ruhayyil  0 Hornets: is an empty base hidden or blocked? (F10 menu adds 2 later)

It also spawns "Join Me", two Hornets orbiting over Khalkhalah with #2 as a Client unit, to see
whether a slot added at runtime can be joined in the air.

Printed: every slot attempt as DCS describes it (slot_try), refusals, slot changes, births and
other events of player aircraft, and the stock at the three bases whenever it changes.
F10 > Other > Campaign: "Stock", "Marj Ruhayyil: add 2 Hornets", "Khalkhalah: set Hornets to 0".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any

from campaign.bridge import Bridge, BridgeError, Connection
from campaign.flight_spawn import build_flight_spawn
from campaign.model import Flight, Pos, Side, StartMode, Waypoint
from campaign.protocol import Envelope, Hello, Src
from tools.build_test_missions import AIRBASE_NAME, AIRBASE_XY, DYNAMIC_AIRDROMES

HORNET = "FA-18C_hornet"
STOCK = {"Khalkhalah": 4, "Tiyas": 2, "Marj Ruhayyil": 0}
DENIED_BASE = "Tiyas"
DENY_REASON = "P2 TEST: slots at Tiyas are refused by the campaign."
FT = 0.3048
POLL_S = 5.0
EVENT_KINDS = {"birth", "player_enter_unit", "player_leave_unit", "takeoff", "land", "engine_shutdown",
               "unit_killed", "crash", "ejection"}
MENU = [{"label": "Stock", "key": "stock"},
        {"label": "Marj Ruhayyil: add 2 Hornets", "key": "marj_add"},
        {"label": "Khalkhalah: set Hornets to 0", "key": "khal_zero"}]


def short(data: Any, limit: int = 600) -> str:
    return json.dumps(data, sort_keys=True)[:limit]


def join_me_group() -> dict[str, Any]:
    """Two Hornets orbiting over Khalkhalah at 15,000 ft; #2 is a Client slot."""
    home = Pos(*AIRBASE_XY)
    alt = 15_000 * FT
    f = Flight(id="joinme", package_id="p2", squadron_id="p2", callsign="Join Me", mission="CAP", aircraft=2,
               loadout="", takeoff_time=0, start=StartMode.AIR, waypoints=[
                   Waypoint(Pos(home.x + 5_000, home.y), alt),
                   Waypoint(Pos(home.x + 5_000, home.y), alt, action="ORBIT", params={"duration_s": 7_200})])
    data = build_flight_spawn(f, HORNET, Side.BLUE).data
    data["group"]["units"][1]["skill"] = "Client"
    return data


class Probe:
    def __init__(self) -> None:
        self.mission: Connection | None = None
        self.stock: dict[str, dict[str, int]] = {}

    def on_hello(self, conn: Connection, hello: Hello, new_run: bool) -> None:
        print(f"HELLO {conn.src} run={hello.run_id} new_run={new_run} spawned={hello.spawned}", flush=True)
        if conn.src == Src.HOOKS:
            asyncio.create_task(self.rules(conn))
        elif conn.src == Src.MISSION:
            self.mission = conn
            if new_run:
                asyncio.create_task(self.setup(conn))

    def on_disconnect(self, conn: Connection) -> None:
        print(f"DISCONNECT {conn.src}", flush=True)
        if conn.src == Src.MISSION:
            self.mission = None

    def on_message(self, conn: Connection, env: Envelope) -> None:
        if env.type != "event":
            return
        kind = env.data.get("kind")
        if conn.src == Src.HOOKS:
            print(f"HOOKS   {kind:18} {short({k: v for k, v in env.data.items() if k != 'kind'})}", flush=True)
        elif kind == "menu_select":
            asyncio.create_task(self.menu(env.data.get("key")))
        elif kind in EVENT_KINDS:
            unit = env.data.get("unit") or {}
            if unit.get("player") or unit.get("type") == HORNET:
                print(f"EVENT   {kind:18} {short(env.data)}", flush=True)

    async def rules(self, conn: Connection) -> None:
        try:
            await conn.call("slot_rules", {"allowed_sides": [0, 2], "deny": {},
                                           "deny_airdromes": {str(DYNAMIC_AIRDROMES[DENIED_BASE]): DENY_REASON}})
            print(f"RULES   slots at {DENIED_BASE} (airdrome {DYNAMIC_AIRDROMES[DENIED_BASE]}) are refused", flush=True)
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"slot_rules FAILED: {e}", flush=True)

    async def setup(self, conn: Connection) -> None:
        try:
            for base, n in STOCK.items():
                await conn.call("warehouse_set", {"airbase": base, "aircraft": {HORNET: n}})
            await conn.call("menu", {"audience": "all", "items": MENU})
            await conn.call("message", {"audience": "all", "duration": 30, "text":
                                        "P2 slots probe connected. Dynamic slots: Khalkhalah 4 Hornets, Tiyas 2 "
                                        "(refused by the campaign), Marj Ruhayyil 0. F10 > Other > Campaign."})
            res = await conn.call("spawn_group", join_me_group())
            print(f"SPAWN   Join Me over {AIRBASE_NAME}, #2 is a Client unit: {short(res)}", flush=True)
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"SETUP FAILED: {type(e).__name__}: {e}", flush=True)
        asyncio.create_task(self.watch_stock(conn))

    async def query_stock(self, conn: Connection) -> dict[str, dict[str, int]]:
        out = {}
        for base in STOCK:
            res = await conn.call("query", {"what": "warehouse", "airbase": base, "types": [HORNET]})
            out[base] = res.get("counts", {}) if res else {}
        return out

    async def watch_stock(self, conn: Connection) -> None:
        while self.mission is conn:
            try:
                stock = await self.query_stock(conn)
                if stock != self.stock:
                    self.stock = stock
                    print("STOCK   " + ", ".join(f"{b}: {c.get(HORNET)}" for b, c in stock.items()), flush=True)
            except (BridgeError, asyncio.TimeoutError) as e:
                print(f"stock query failed: {e}", flush=True)
            await asyncio.sleep(POLL_S)

    async def menu(self, key: str | None) -> None:
        conn = self.mission
        if conn is None:
            return
        try:
            if key == "marj_add":
                n = (self.stock.get("Marj Ruhayyil") or {}).get(HORNET) or 0
                await conn.call("warehouse_set", {"airbase": "Marj Ruhayyil", "aircraft": {HORNET: n + 2}})
                print("MENU    Marj Ruhayyil: +2 Hornets", flush=True)
            elif key == "khal_zero":
                await conn.call("warehouse_set", {"airbase": "Khalkhalah", "aircraft": {HORNET: 0}})
                print("MENU    Khalkhalah: Hornets set to 0", flush=True)
            stock = await self.query_stock(conn)
            text = "Hornet stock: " + ", ".join(f"{b} {c.get(HORNET)}" for b, c in stock.items())
            await conn.call("message", {"audience": "all", "text": text, "duration": 15})
        except (BridgeError, asyncio.TimeoutError) as e:
            print(f"menu {key} failed: {e}", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7341)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    probe = Probe()
    bridge = Bridge(probe.on_message, probe.on_hello, probe.on_disconnect, host=args.host, port=args.port)
    await bridge.start()
    print(f"listening on {args.host}:{bridge.port}; waiting for DCS (load campaign-slots.miz)", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
