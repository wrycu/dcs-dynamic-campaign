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

Players with the opt-in client script (`lua/client/campaign_client.lua`, version 16) get the war
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
  a flight (the panel's **Upcoming flights** buttons, the first 6 player flights in the next 3 hours,
  each with its time to take-off as BMS shows it, `T-0:19:32`, counted down live on mission time,
  and marked `>` when selected and `+` when in the selected flight's package;
  or a flight's symbol in the air) to see its plan: its route in white with each waypoint's time
  and time from take-off (`WP1 00:42 +0:12`, `TGT`, `STN` for a station, `LAND`), its own target
  (a SEAD flight's SAM site, a moving target where it is now, else its attack point; not the
  package's), the other flights of its package as yellow dashed routes with their time on target, and every enemy
  air defence site along the way. The view moves to the plan, zooming out only if it doesn't fit.
  Times are planned: take-off, then each leg at the flight's cruise speed (the package's slowest)
  unless the waypoint has its own time, plus any time on station.
- **The enemy:** airbases with the aircraft types reported there; depots, factories and power
  as targets, with their damage; formations only within 8 nm of blue ground or forces, with a
  rough strength ("about 25 vehicles") and no names; flights only while airborne within 80 nm
  of a blue airbase or 150 nm of a blue AWACS, with their kind (fighters, attack) but not type.
- **Panel:** the campaign's day and the time of day as DCS has it, the mission's weather in a line
  (clouds, visibility, wind speed, temperature; the mission bridge's `query mission`, asked on each
  mission connect), objectives held, battles, the latest war news, and the **Upcoming flights**
  buttons. A scale bar on the map's bottom-left corner (a round distance in nm) follows the zoom. All the map's times are times
  of day. No summary of threats: the map shows them.
- **Joining a flight:** with a player flight selected, a button under the list says
  `Join Uzi 4 (2 of 2 seats free)`, `Leave Uzi 4 (you're Uzi 4-1)`, or why not (full, taken off).
  Joining gives the player the first free seat (`Flight.player_seats`, saved; one flight at a time:
  joining another leaves the first), answers in chat and sends the map again. The flight's details
  list who has which seat; its button shows `1/2 seats`. Players are known by ucid, learnt from
  `player_connect` and from the click itself.
- **Into the jet** (`campaign/session.py`): a second button, for players seated in the flight.
  - **Commit** (the lead, seat 1, once everyone is in): the server builds the *session mission*
    and has the Hooks load it 15 s later, so everyone goes back to the slot screen. It has a
    Client group for every committed flight still on the ground (`Uzi 4 BAI`, units `Uzi 4-1`...):
    the players' seats are Client slots, the others AI in the same group. Each group has the
    flight's type, loadout, fuel, route, callsigns, parking (stands it fits) and, for a Hornet,
    each player's cartridge (it loads itself). The mission starts at the campaign's time now; the
    template's own player slots are left out. Built from `mission_template` into `missions_dir`
    as `campaign-session-a.miz` / `-b.miz` in turn. Refused while any player is in a jet (the
    reload would take it away). Once committed, only its Client seats can be joined.
  - **Fly** (`Fly Uzi 4-1 from 08:00`, greyed out until 20 min before take-off): the server moves
    the player into their slot (`force_player_slot`; they must be blue, spectators are fine) and
    the client script puts them in the cockpit, closing the map. Without the client script, take
    the slot from the slot list: the Hooks refuse a seat before it opens and a seat held for
    someone else; a free seat is anyone's, and taking it makes it theirs.
  - **The ride to the jet** (`ride_to_jet`, client script version 24 or later; campaign/rides.py):
    Fly doesn't put the player straight in. A static of the flight's type stands on each of its
    spots from the mission's load (Client jets aren't in the world until their player takes the
    slot). Nobody has to press anything: after the reload, a committed player who picks blue
    spectators waits on the map (the "briefing room"; chat says when the ride comes) and is
    boarded by themselves when Fly opens, 20 min before take-off; Fly does the same by hand. The
    first of a flight starts its ride: a Humvee beside the base's tower or the
    building nearest the jets, the player's view in its passenger seat. It waits 15 s for the
    rest of the flight (each Fly boards too, up to three), then drives the base's taxiways
    (DCS's own taxiway file) to the row of jets. It stops beside each nose, nearest first,
    putting each rider into their own jet and stopping a moment at the others, then drives
    straight on and is removed 250 m later. A Fly after it has left goes straight in. A seat's
    stand-in goes just before its player is put in; the AI seats' go with the first player (they
    spawn with them), all of them when the flight takes off or goes to the AI.
  - **In the jet:** the Client group is the flight from its first birth: take-off, landing and
    losses count as for any flight. It's never despawned while players are in it; landed and shut
    down, it's recovered and the players leave when they like. Players leaving on the ground give
    the jets back (the flight waits for them again); leaving in the air counts as coming home.
  - **Deadline:** nobody in by 10 min after take-off, or a loaded mission without the flight's
    group: it goes to the AI (its slots close, its seats empty) and flies as any other flight.
- **Opens by itself** when the player becomes a blue spectator, about a second after the slot
  screen has gone (or as soon as the first map arrives); for the host, once the slot screen is
  closed (its game is paused until then). Never in a jet (version 16, after Run 28: Fly's move into
  the cockpit opened it there), and getting into one cancels an opening on its way.
  `AUTO_OPEN_MAP` in the client script.

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
| `mission_template` | `""` | The .miz the session mission is built from (it needs a player aircraft to copy). `""`: `a2a.miz` in Saved Games' Missions, as the test missions. |
| `missions_dir` | `""` | Where the session mission is written; must be under Saved Games' Missions, where the Hooks load it from. `""`: `Missions/campaign-dev`. |

| `ride_to_jet` | `true` | Fly as a ride in a Humvee to the jets, for players with client script 24 or later. |
| `ride_kmh` | `20` | The Humvee's speed (5 to 80). |

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
