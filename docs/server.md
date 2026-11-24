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

## Settings

`config/campaign.toml`, `[server]`:

| Key | Default | |
|---|---|---|
| `host`, `port` | `127.0.0.1`, `7341` | Where the bridge listens. |
| `save` | `build/saves/campaign.json` | |
| `autosave_s` | `30` | Real seconds between saves. |
| `status_s` | `60` | Real seconds between console status lines. |
| `slot_sides` | `[0, 2]` | Sides players may take: 0 spectators, 1 red, 2 blue. |

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
