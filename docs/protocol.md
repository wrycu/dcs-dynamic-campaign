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

**Engine restarts:** persist `bridge.run_state(src)` alongside the campaign snapshot. Call `bridge.restore_run(...)` on startup, so a client resuming the same run doesn't replay events whose effects are already in the snapshot.

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
| `ejection` / `pilot_dead` | `unit` |
| `landing_after_ejection` | `unit`, `place` |
| `player_enter_unit` / `player_leave_unit` | `unit` (with `player` = player name) |
| `menu_select` | `key`, `audience`, `group` (group name, for group menus only) |

### `telemetry` (every 3 s)
```json
{"players":[{"name":"...","player":"Viper","group":"...","type":"F-16C_50","pos":{...},"alt":6100,"hdg":270,"spd":230}],
 "groups":[{"id":"gu:2f9a","pos":{...},"alive":9}]}
```

## hooks → engine

### `player`
Each message's `data.kind` is one of:

| kind | data |
|---|---|
| `player_connect` | `player_id`, `ucid`, `name` |
| `player_disconnect` | `player_id`, `ucid`, `reason` |
| `player_slot` | `player_id`, `ucid`, `name`, `side`, `slot`, `unit_type` |
| `slot_denied` | `player_id`, `ucid`, `name`, `side`, `slot`, `reason` |

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
| `spawn_group` | `category` (`airplane`, `helicopter`, `ground` or `ship`), `country` (id or `country.id` name), `group` (a full DCS group table; `name` required) | `{group, units}` |
| `despawn_group` | `group` | `{survivors: {type: count}, alive: [unit names], pos}`, or `{survivors: {}, gone: true}` |
| `spawn_static` | `country`, `static` (a DCS static table; `name` required) | `{name}` |
| `remove_static` | `name` | `{removed}` |
| `set_route` | `group`, `points` (DCS route points) | – |
| `set_task` | `group`, `task`, `push?` | – |
| `draw` | `id` (number, owned by the engine), `audience`, `shape`, plus the shape's fields (see below) | – |
| `undraw` | `id`, or `ids` | – |
| `smoke` | `pos`, `color?` (`red`, `green`, `white`, `orange` or `blue`; default `red`). Smoke lasts about 5 min. | – |
| `message` | `audience`, `text`, `duration?` (10), `clear?` | – |
| `menu` | `audience`, `root?` ("Campaign"), `items` | – |
| `warehouse_set` | `airbase`, `aircraft?: {type: n}`, `weapons?: {type: n}`, `liquids?: {"0": tons}`. Aircraft types are validated (unknown type → error); weapon names cannot be validated in DCS, so the engine must keep its own catalog. | – |
| `query` | `what`: `time`; `group` with `group`; `airbase` with `airbase`; `heights` with `points` (list of `{x, y}`) | Depends on the query; `heights` returns `{heights: [m, …]}` in point order |

Notes on `spawn_group` and `despawn_group`:
- `spawn_group` refuses a name that is already alive. Silent replacement would orphan the old group.
- When a group despawns, `despawn_group`'s `survivors` is the authoritative strength. Reconcile `GroundUnit.composition` from it.

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
| `slot_rules` | `allowed_sides` (default `[0, 2]`, spectators and blue), `deny: {ucid: reason}`. Replaces the whole rule set. |

`onPlayerTryChangeSlot` enforces these rules on the server. A denied player gets the reason as a chat message, and the engine receives `slot_denied`.
