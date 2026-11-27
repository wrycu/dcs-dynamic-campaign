# Engine ↔ DCS protocol (v1)

The Python engine (`campaign/bridge.py`) is the TCP server, listening on `127.0.0.1:7341`. The DCS server has two clients:

| Client | File | Environment | Role |
|---|---|---|---|
| `mission` | `lua/mission/campaign_bridge.lua` | Mission scripting | Events, telemetry, spawning, F10 map/menus, warehouses |
| `hooks` | `lua/hooks/campaign_hooks.lua` | GameGUI (Hooks) | Player identity by UCID, slot gating |

The engine is authoritative. The Lua side holds almost no state. It forwards events and executes commands.

## Setup (dedicated server)

1. **Mission bridge:** in the `.miz`, add a MISSION START trigger with a DO SCRIPT FILE action pointing at `campaign_bridge.lua`.
2. **Hooks script:** copy `campaign_hooks.lua` to `Saved Games\DCS.<variant>\Scripts\Hooks\`.
3. **Desanitize:** in `<DCS install>\Scripts\MissionScripting.lua`, comment out `_G['require'] = nil` and `_G['package'] = nil` (LuaSocket needs them). Re-apply this after every DCS update. See `docs/testing-in-dcs.md` for the exact command.
4. **Disable pause-when-empty:** the mission-side network pump runs on model time and stops while the mission is paused.
5. **Optional config:** define `CampaignBridgeConfig` or `CampaignHooksConfig` globals before the scripts load to override their defaults.

## Framing

- One JSON object per line, UTF-8, terminated by `\n`.
- Lua encodes with DCS's `net.lua2json`. Its behaviour, verified on DCS 2.9.31:
  - an empty table encodes as `{}`, so an empty list also arrives as `{}`. The engine also accepts `[]` for `data`, and list fields such as `telemetry.groups` go through `protocol.as_list()`;
  - a sparse array becomes an object with string keys (`{"1":"a","3":"c"}`);
  - newlines inside strings are escaped, so line framing is safe.
- Object names are always strings. Scenery `getName()` returns a number, which the bridge converts with `tostring`.

## Envelope

```json
{"v":1, "type":"event", "src":"mission", "seq":17, "t":3621.4, "data":{...}}
```

| Field | Meaning |
|---|---|
| `v` | Protocol version. Must be `1`. |
| `type` | `hello`, `welcome`, `event`, `telemetry`, `player`, `cmd` or `ack`. |
| `src` | `mission`, `hooks` or `engine`. |
| `seq` | Per-connection sequence number. `0` means unsequenced; see "Delivery guarantees" below. |
| `t` | DCS model time (`timer.getTime()`). Present on mission messages. |
| `id` | Command id (on `cmd`). |
| `re`, `ok`, `result`, `error` | Ack fields: the command id being answered, success flag, result or error text. |

## Delivery guarantees

**Sequenced messages** (`event`, `player`) are numbered 1, 2, 3, … per run.
- The client queues them while disconnected (up to 5000) and keeps the last 2000 sent for replay.
- After reconnecting, the client sends `hello`. The engine replies `welcome {last_seq}`, and the client replays everything newer, oldest first.
- The engine drops any seq ≤ `last_seq`, so delivery is effectively exactly-once within a run.

**Unsequenced messages** (`telemetry`, `ack`, and `cmd` in the other direction) are sent only while connected. They are never replayed.

**Commands are not replayed.** If the connection drops, every pending `call()` fails with `Disconnected`. The engine re-derives what it wants from its own state.

**`run_id`:**
- The mission client generates a new one on every mission load. The hooks client generates one per DCS process.
- A new `run_id` means the client's sequence restarts at 1. For the mission, it also means everything previously spawned is gone.

**Engine restarts:** persist `bridge.run_state(src)` alongside the campaign snapshot. Call `bridge.restore_run(...)` on startup, so a client resuming the same run doesn't replay events whose effects are already in the snapshot. `run_state` reports the last message whose handler has *finished*, not just the last received, so a save never claims an event it hasn't applied. `campaign/persist.py` does all of this; see `docs/saves.md`.

## Handshake

```json
→ {"type":"hello","src":"mission","seq":0,"data":{"proto":1,"run_id":"1763...","map":"Caucasus","spawned":["gu:2f9a"]}}
← {"type":"welcome","src":"engine","seq":0,"data":{"last_seq":0}}
```

- `hello` must be the first message on a connection.
- A newer connection from the same `src` replaces the older one.
- `spawned` lists the campaign groups that are still alive, for reconciliation:
  - groups listed that the engine doesn't know about → despawn them;
  - groups the engine believes are spawned but aren't listed → mark them aggregated.

## Naming

- Campaign groups are named `<prefix>:<model id>` and their units `<prefix>:<model id>#<n>`.
- Prefixes: `gu` (ground unit), `fl` (flight), `st` (static).
- `campaign.protocol.parse_dcs_name()` turns a name back into a model id. It returns `None` for foreign objects such as player dynamic spawns, scenery and editor-placed objects.

## Positions

- The model's `Pos(x, y)` is DCS 2D map space: `x` = DCS `x` (north), `y` = DCS `z` (east).
- Altitude travels separately as `alt`, which is DCS `y`.

## mission → engine

### `event`
Each event's `data.kind` is one of the following. Unit and object descriptors look like `{name, category, type, coalition, group, player, pos, alt}`.

| kind | data |
|---|---|
| `birth` | `unit`, `place` |
| `takeoff` / `land` | `unit`, `place` (airbase name) |
| `engine_shutdown` | `unit` |
| `unit_killed` | `unit`. DEAD, UNIT_LOST and CRASH merged, once per unit life (player slots reuse unit names). Not sent for despawns or `remove_static`. |
| `unit_removed` | `unit`, `reason` (`player_left`). DCS deletes an aircraft when its player changes slot or disconnects, and fires loss events for it. The bridge reports this instead of `unit_killed`/`pilot_dead`, unless the pilot had ejected. Whether the airframe is lost is campaign policy. |
| `kill` | `killer`, `victim`, `weapon`: kill credit. It may arrive before or after the matching `unit_killed`. |
| `hit` | `target`, `shooter`, `weapon`. Statics and scenery only, at most once per target per second. |
| `scenery_destroyed` | `object`. `name` is `getName()` as a string, the unsigned scenery id that `Feature.dcs_ref` stores. `id_` is the same value as a signed 32-bit integer, so it goes negative above 2^31 (`2215182453` arrives as `-2079784843`). |
| `ejection` | `unit`, `pilot` (the object DCS hands over as the ejected pilot, if any) |
| `pilot_dead` | `unit` |
| `landing_after_ejection` | `unit`, `place` |
| `player_enter_unit` / `player_leave_unit` | `unit` (with `player` = player name) |
| `menu_select` | `key`, `audience`, `group` (group name, for group menus only) |

### `telemetry` (every 3 s)
```json
{"players":[{"name":"...","player":"Viper","group":"...","type":"F-16C_50","pos":{...},"alt":6100,"hdg":270,"spd":230}],
 "groups":[{"id":"gu:2f9a","pos":{...},"alive":9},
           {"id":"fl:cap1","pos":{...},"alive":2,"alt":6100,"hdg":10,"spd":220,"fuel":0.71,"in_air":true}]}
```
`groups` covers every campaign group the bridge spawned. Aircraft groups also carry the lead's `alt` (m), `hdg` (degrees true), `spd` (m/s), `fuel` (fraction) and `in_air`.

`walkers` lists ejected pilots the bridge was handed by `ejection` or `landing_after_ejection`: `key`, `exists`, `from` (the unit they left), and while they exist the usual `type`, `category`, `pos`. A pilot that no longer exists is reported once more and then dropped.

## hooks → engine

### `player`
Each message's `data.kind` is one of:

| kind | data |
|---|---|
| `player_connect` | `player_id`, `ucid`, `name` |
| `player_disconnect` | `player_id`, `ucid`, `reason` |
| `slot_try` | `player_id`, `ucid`, `name`, `side`, `slot`, `unit_type`, `entry`: every slot attempt, before the rules. `entry` is DCS's slot listing (`unitId`, `type`, `groupName`, `airdromeId`, ...) or null |
| `player_slot` | `player_id`, `ucid`, `name`, `side`, `slot`, `unit_type`, `entry` |
| `slot_denied` | `player_id`, `ucid`, `name`, `side`, `slot`, `reason` |
| `chat_command` | `player_id`, `ucid`, `name`, `side`, `slot`, `text`: chat starting with `-` (the `-` removed). The other players don't see it. Live only: never queued or replayed after a reconnect; while the engine is away the player is told it isn't connected. |
| `frame_stats` | `frames`, `avg_ms`, `max_ms`, `over_50ms`, `seconds`: the server's frame times, every 10 s of real time |
| `client_hello` | `player_id`, `version`: that player runs the optional client script (`lua/client/campaign_client.lua`); sent on each mission load |
| `client_click` | `player_id`, `ucid`, `name`, `side`, `slot`, `key`: a button in the client script's window or web page. Live only, like `chat_command`. The war map's join button sends `join:f:<flight id>` and `leave:f:<flight id>` (client script version 14). |
| `client_web` | `player_id`, `stage` (`window`, `open`, `created`, `loaded`, `mount_failed`, and for the war map `map` and `map_click`), `detail`: how the client script's window, web page and map got on (P19, P21 diagnostics). Live only. |
| `mission_load` | `stage` (`requested`, `begin`, `end`, `start`, `stop`), `file` or `path`, `ok`/`error` (for `requested`), `real_time` |

The engine links mission events to UCIDs by player name. Names are unique on a server at any one time.

## engine → client: `cmd`

```json
{"type":"cmd","src":"engine","seq":5,"id":"c42","data":{"op":"despawn_group","group":"gu:2f9a"}}
← {"type":"ack","src":"mission","seq":0,"re":"c42","ok":true,"result":{...}}
```

From Python: `await bridge.call("mission", "despawn_group", {"group": "gu:2f9a"})`. It raises `CommandFailed`, `Disconnected`, `NotConnected` or `TimeoutError`.

### Mission commands

| op | data | result |
|---|---|---|
| `spawn_group` | `category` (`airplane`, `helicopter`, `ground` or `ship`), `country` (id or `country.id` name), `group` (a full DCS group table; `name` required) | `{group, units}`. Fails if DCS created no units, which is what a ramp or runway start does when the airbase warehouse has none of the type |
| `despawn_group` | `group` | `{survivors: {type: count}, alive: [unit names], pos}`, or `{survivors: {}, gone: true}` |
| `spawn_static` | `country`, `static` (a DCS static table; `name` required) | `{name}` |
| `remove_static` | `name` | `{removed}` |
| `set_route` | `group`, `points` (DCS route points) | – |
| `set_task` | `group`, `task`, `push?` | – |
| `set_command` | `group`, `command` (a DCS command table, e.g. `{"id": "Start", "params": {}}` to start an uncontrolled flight) | – |
| `draw` | `id` (number, owned by the engine), `audience`, `shape`, plus the shape's fields (see below) | – |
| `draw_batch` | `items`: a list of `draw` data | `{drawn, ms}` (ms of real time DCS took) |
| `undraw` | `id`, or `ids` | – |
| `smoke` | `pos`, `color?` (`red`, `green`, `white`, `orange` or `blue`; default `red`). Smoke lasts about 5 min. | – |
| `message` | `audience`, `text`, `duration?` (10), `clear?` | – |
| `menu` | `audience`, `root?` ("Campaign"), `items` | – |
| `warehouse_set` | `airbase`, `aircraft?: {type: n}`, `weapons?: {type: n}`, `liquids?: {"0": tons}`. Aircraft types are validated (unknown type → error); weapon names cannot be validated in DCS, so the engine must keep its own catalog. | – |
| `query` | `what`: `time`; `group` with `group`; `airbase` with `airbase`; `heights` with `points` (list of `{x, y}`); `parking` with `airbase`, `free_only?`; `type_size` with `type` → `{span, length, height}` (metres, from the type's bounding box); `warehouse` with `airbase`, `types`; `airbases` (all of them: `name`, `id`, `pos`, `alt`, `category`, `coalition`, `type`, `runways`, `parking` count); `ll_to_xy` with `points` (`{lat, lon}`) → `{points}` as `{x, y}`; `scenery` with `center`, `radius`, `match?` (type-name substrings), `limit?` → `{total, counts: {type: n}, found: [{type, pos}]}`; `objects` with `center`, `radius`, `limit?` (50) → `{found}`: units, weapons, statics and cargo nearby, described like event units plus `id`; `mission` (the loaded mission's `date`, `start_time`, `theatre`, `weather` summary, and `abs`/`model` time now); `road_point` with `pos`, `road?` (`roads` or `railroads`); `road_path` with `from`, `to`, `road?` | Depends on the query: `road_point` returns `{found, pos}`, the nearest point on the network; `road_path` returns `{points}`, DCS's road route as a polyline (empty if none); `airbase` returns `{exists, name, id, pos, alt, coalition, category}`; `heights` returns `{heights: [m, …]}` in point order; `parking` returns `{spots: [{term, type, pos, dist_rw, occupied}]}` (`term` is the `Term_Index` a ramp start's unit `parking` takes); `warehouse` returns `{counts: {type: n}}` |

Notes on `spawn_group` and `despawn_group`:
- `spawn_group` refuses a name that is already alive. Silent replacement would orphan the old group.
- When a group despawns, `despawn_group`'s `survivors` is the authoritative strength. Reconcile `GroundUnit.composition` from it.
- For aircraft, `despawn_group` also returns the lead's `alt` (m), `hdg` (degrees true) and `spd` (m/s), the lowest `fuel` fraction in the group, and `ammo` (`{weapon type: count}` summed over the group), so a flight can be respawned where it was.
- In an aircraft unit's `payload`, `pylons` may be keyed by strings (JSON objects always are); the bridge converts them to pylon numbers. A unit's table `callsign` (`{1: name index, 2: flight, 3: unit, name}`) gets the same conversion. `fuel_fraction` (0–1) is converted to kilograms from the type's `fuelMassMax`.

**Draw shapes:**

| shape | fields |
|---|---|
| `mark` | `pos`, `text` |
| `line` | `from`, `to` |
| `circle` | `center`, `radius` |
| `rect` | `from`, `to` |
| `poly` | `points` (at least 3) |
| `text` | `pos`, `text`, `font_size?` |

- Common optional fields: `color` and `fill` (`[r,g,b,a]`, 0–1), `line_type`, `read_only`, `message`.
- Drawing with an existing `id` replaces that shape.
- Only `mark` can target a group audience.

**Audience:** `all`, `coalition:blue`, `coalition:red`, `coalition:neutral`, or `group:<group name>`.

**Menus:**
- `items` is `[{label, key} | {label, items:[...]}]`.
- Sending `menu` again for the same audience replaces the whole tree; empty `items` removes it.
- DCS shows at most 10 entries per level.
- Coalition and `all` menus cannot tell who clicked. Use group menus for per-player actions.

### Hooks commands

| op | data |
|---|---|
| `slot_rules` | `allowed_sides` (default `[0, 2]`, spectators and blue), `deny: {ucid: reason}`, `deny_airdromes: {airdrome id: reason}` (slots DCS lists at that airdrome). Replaces the whole rule set. |
| `load_mission` | `file` (a name in Saved Games\Missions, or a full Windows path), `delay?` (s, default 1). Loads that mission on the server a little later, after the ack; progress arrives as `mission_load` events. Returns `{path}`. |
| `chat_to` | `player_id?`, `text`: a chat line to one player, or to everyone without `player_id`. |
| `players` | – Returns every connected player: `[{id, name, ucid, side, slot}]`. |
| `force_slot` | `player_id`, `side`, `slot` (`0` and `""` for spectators): `net.force_player_slot`. Returns `{ok}`. |
| `spawn_for` | `player_id`, `spawn`: a dynamic-slot spawn for that player, the fields the slot screen passes to `DCS.create_client_aircraft` (`airdromeId`, `isAirdrome`, `coalition`, `coalitionId`, `typeLA`, `category`, `numFuel` kg, `numFlare`, `numChaff`, `gun`, `ammoType`, `liveryEntry`, `isHot`, `isInAir`, ...). Pylons go as `pylon_list: [{num, CLSID}]` (JSON can't carry numeric keys). `userId` and `nickName` default to the player. Returns `{result}`, what DCS returned. Run 14: DCS creates the slot and assigns it, but the player's own DCS never enters it; use `force_slot` into a mission slot, or the client script. |
| `client_rpc` | `player_id`, `method`, `args` (list): an event to that player's optional client script over the stock RPC channel. Methods: `campaignJoin` (args `[spawn]`, the `spawn_for` fields: the client spawns and enters, as its slot screen does), `campaignEnter` (no args: the client calls `net.spawn_player()`), `campaignWindow` (args `[{title, lines, buttons: [{key, label}]}]`: a dxgui window; buttons come back as `client_click`) `campaignWeb` (args `[{html}]` or `[{url}]`: a page in DCS's Chromium widget; a page's `window.edQuery` method `click` comes back as `client_click`) and `campaignMap` (args `[{title, center, scale, mode, keep_view, show, panel, objects}]`: the war map window, P21; `show: false` (version 4) keeps it for Ctrl+Shift+F8, or redraws it in place if it's open; `detail_scale` and the objects' `detail`, `groups` and `select` (version 6) put objects in layers drawn when zoomed in or while a flight is selected; `flights` (version 7) are the panel's flight buttons (`label`, `select`, `info`, from version 8 `fit`: `[x0, y0, x1, y1]` to bring into view, and from version 9 `name`, `tail` and `takeoff_in` (seconds) for a label the client counts down: `<name>  T-h:mm:ss  <tail>`); `packages` (version 10): each selectable flight's package, for the list's marks; `joins` (version 14): each selectable player flight's join button for this viewer (`label`, `key`, `ok`); the object kinds are listed in the client script's `open_map` comment). Fire and forget; a player without the script ignores it. For the host of a server hosted from DCS, the Hooks call the client script directly. |
| `stands` | `airdrome_id`. Returns `{stands: [{index, x, y, shelter, airplanes, helicopters, width, length, height}]}`: the airdrome's parking stands with the sizes the Mission Editor checks (metres; `height` may be missing). Errors if the Hooks can't load DCS's `terrain` module. |

`onPlayerTryChangeSlot` enforces these rules on the server. A denied player gets the reason as a chat message, and the engine receives `slot_denied`.
