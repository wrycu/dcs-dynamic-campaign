# Campaign server

`campaign/server.py` is the engine process for a whole session: one process, one save, every
part of the war (backlog item 6). It replaces running the demos one at a time.

```bash
python3 -m campaign.server --scenario khalkhalah        # first time, or to carry on
python3 -m campaign.server --new --scenario khalkhalah  # start over (the old save is kept as .1)
```

The log goes to the console and is appended to `build/logs/server.log`. Don't pipe it into
`tee`: Ctrl-C stops `tee` too, and the rest of the log is lost.

| Flag | Meaning |
|---|---|
| `--scenario NAME` | `campaign/scenarios/NAME.py`. Optional once a save exists, and must match it. |
| `--config PATH` | Settings TOML. Default `config/campaign.toml`. |
| `--save PATH` | Save file. Default `[server] save`. |
| `--new` | Start the scenario over. |
| `--port N` | Bridge port. Default `[server] port`. |
| `--ghost BRG/NM\|AIRBASE` | Test aid: a ghost player the bubbles treat as real, at a magnetic bearing and range from the scenario's home airbase, or on an airbase. Repeat for more. Drawn as `GHOST n`; Status ignores it. |
| `--log PATH` | Also append the log here. Default `build/logs/server.log`. |
| `-v` | Debug logging. |

Stop it with Ctrl-C (or SIGTERM): it lets a running tick finish, saves, and closes the bridge. A
second Ctrl-C quits at once.

## What it runs

- **The bridge:** the mission (events, telemetry, spawning) and the Hooks (players, slots). On
  Hooks connect it sends the slot rules (`[server] slot_sides`, default spectators and blue).
- **The ground bubble:** formations spawn near players ([bubble](bubble.md)), and near AI flights
  in DCS that are attacking them: the target spawns when the flight is as close as a player
  would need to be, and stays until the flight has left its attack waypoint behind.
- **AI flights:** in DCS near players, flown by the engine everywhere else ([air](air.md)).
- **Systems:** the war simulation and the planner plug in here (items 8 and 2), as
  `campaign.server.System` subclasses that a scenario returns from `systems()`.
- **The scenario:** builds the starting state on the first mission connection, when DCS can be
  queried (airbases, parking), and can add its own displays.
- **Saving:** loaded at startup; written every `autosave_s` while anything changes, on mission
  connect and disconnect, and on stop ([saves](saves.md)).
- **Status:** a line on the console every `status_s`. In DCS: F10 > Other > Campaign > Status.

## The tick

Each telemetry report (every 3 s) runs one tick, in this order:

1. **Inputs:** telemetry updates positions and players. Events are applied as they arrive.
2. **Clock:** the campaign clock advances by the DCS model time since the last tick. A paused
   mission stops the war; time acceleration speeds it up (D1). After a stall, at most 120 s is
   applied at once.
3. **War:** each system's `advance(t0, t1)`. Flights that aren't in DCS fly their routes.
4. **Planning:** each system's `tick()`.
5. **Bubbles:** flights recovered or aborted, then flights and formations put into or taken out
   of DCS.
6. **Scenario:** its `tick()`.
7. **Save,** if `autosave_s` has passed and something changed.

A part that fails is logged, and the rest of the tick still runs. A report that arrives while a
tick is running doesn't start another; the clock catches up on the next one.

Dwell times, combat pins and the taxi/shutdown watchdogs run on the campaign clock, so they
pause with the mission.

## Scenarios

A scenario module defines `make()` returning a `campaign.server.Scenario`:

| Method | When |
|---|---|
| `new_state()` | A new campaign: the empty `CampaignState`. |
| `systems(engine)` | At startup: the systems to run. |
| `setup(engine)` | Once per campaign, on the first mission connection: fill the state. |
| `on_mission(engine, new_run)` | Every mission connection: warehouse stock, drawings, menus. |
| `on_event(engine, data)`, `tick(engine)` | Every event and tick. |

Until the theater (item 7) maps objectives to airbases, a squadron's `base_id` is the DCS
airbase name. Anything else (e.g. `offmap`) has no position, so its flights start in the air.

**`khalkhalah`** is the test scenario: the bubble demo's five formations and the flight demo's
three flights (Red Ferry, Blue CAS, Blue Ferry), with every formation and flight labelled on the
F10 map (`[DCS]` or `[engine]`), and every change announced on screen. Status gives magnetic
bearing and range from the player to each. Use `build/campaign-smoke.miz`;
`python3 -m campaign.scenarios.khalkhalah DTC_DIR` writes a test cartridge. Steps: Run 15 in
[testing in DCS](testing-in-dcs.md).

## The war map

Players with the opt-in client script (`lua/client/campaign_client.lua`, version 9) get the war
in a full-screen native DCS window with the Mission Editor's map (proof P21), built by `campaign/warmap.py`
for scenarios with a map layer (`southern_syria`). On loading in, a player's script says hello
and the server sends the map hidden: **Ctrl+Shift+F8** shows or hides it; `-map` in chat sends it
and opens it. It's refreshed every `map_refresh_s` for everyone who has it: in place if it's
open (the view stays where the player left it), and kept for the hotkey if it isn't. The `MAP`
lines in the log say what was sent (objects, size, build time) and how each client got on.

What blue sees ("plausible intel": what a real side would know, not all the engine knows):
- **Ground:** each side's shaded, the front line where they meet. A point belongs to the side
  holding the nearest objective. Neutral objectives (Lebanon) don't count, so they're shaded as
  whoever is nearest. An enclave is drawn over its surroundings, as a ring.
- **Own forces:** every formation (click: order, strength, morale, supply, battle), airbase
  (squadrons, aircraft ready and damaged), depot, factory and power plant (damage), and battle.
- **Air defence,** in three layers, each site with its ring (the ring takes no clicks):
  - **always:** long range SAMs only (SA-2, SA-5, SA-10, SA-20, Patriot), both sides';
  - **zoomed in** to 40 nm across or closer (the view's width, measured on the map): every site blue knows of: all its own (Hawks,
    Avengers), the enemy's SAMs reaching 15 km or more (they radiate), and the enemy's short
    range air defence within 8 nm of blue ground or forces, or in a battle;
  - **a flight selected** (its symbol, route or panel button): every enemy site, of any size, that reaches
    within 3 nm of its route or target (its threat brief), also listed in the flight's details.
    A click on empty map clears the selection.
  Click a site: ours give launchers and vehicles; the enemy's a rough count ("3 or 4
  launchers"), whether it's reported damaged, and its radar.
- **Flights:** own flights in the air (players' flights in yellow). No routes by default. Select
  a flight (the panel's **Your flights** buttons, the first 6 player flights in the next 3 hours,
  each with its time to take-off as BMS shows it, `T-0:19:32`, counted down live on mission time;
  or a flight's symbol in the air) to see its plan: its route in white with each waypoint's time
  and time from take-off (`WP1 00:42 +0:12`, `TGT`, `STN` for a station, `LAND`), its target, the
  other flights of its package as yellow dashed routes with their time on target, and every enemy
  air defence site along the way. The view moves to the plan, zooming out only if it doesn't fit.
  Times are planned: take-off, then each leg at the flight's cruise speed (the package's slowest)
  unless the waypoint has its own time, plus any time on station.
- **The enemy:** airbases with the aircraft types reported there; depots, factories and power
  as targets, with their damage; formations only within 8 nm of blue ground or forces, with a
  rough strength ("about 25 vehicles") and no names; flights only while airborne within 80 nm
  of a blue airbase or 150 nm of a blue AWACS, with their kind (fighters, attack) but not type.
- **Panel:** day and time, objectives held, battles, known threats by type, the latest war news,
  and the player flights' buttons.

## Settings

`config/campaign.toml`, `[server]`:

| Key | Default | |
|---|---|---|
| `host`, `port` | `127.0.0.1`, `7341` | Where the bridge listens. |
| `save` | `build/saves/campaign.json` | |
| `autosave_s` | `30` | Real seconds between saves. |
| `status_s` | `60` | Real seconds between console status lines. |
| `slot_sides` | `[0, 2]` | Sides players may take: 0 spectators, 1 red, 2 blue. |
| `map_refresh_s` | `30` | Real seconds between war map refreshes (at least 5). |

`[bubble]` is in [bubble](bubble.md). Unknown keys and sections are an error.

## Checked locally (2026-11-20)

Against a fake mission and Hooks over TCP (no DCS):
- The scenario was set up on the first connection, and the Hooks got their slot rules.
- The formations within 20 nm of the player spawned.
- Each flight launched: Red Ferry from its air start, Blue CAS on the ramp at +5 min, and Blue
  Ferry in the engine first, then in DCS as it came near.
- Over 240 ticks (12 min of model time), the save was written on connect, disconnect and stop.
- A restart with a new mission run took everything back into the engine and respawned it.
- A save from another scenario was refused.
