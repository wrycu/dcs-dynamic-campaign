# Saves

`campaign/persist.py` saves and loads the campaign. The [campaign server](server.md) uses it directly; `tools/demo_save.py` wires it into the demos.

## What's in a save

One JSON file:

| Key | Contents |
|---|---|
| `format` | `1` |
| `saved_at` | UTC time |
| `state` | the whole `CampaignState`, field by field |
| `runs` | `{src: [run_id, seq]}` from `Bridge.run_state()`: the last event already applied to `state` |
| `extra` | spawner bookkeeping: unit-name → type maps, units already counted dead, dwell and combat-pin timers (stored as ages), and AI flight takeoff/landing progress |

**Encoding:**
- It comes from the dataclass type hints, so a new model field needs no codec change.
- Enums are stored by name, `Pos` as `{x, y}`, sets as sorted lists.
- Dict keys are strings: enum names, or numbers as text.
- A field missing from an older save takes its default. A field that is required, or has the wrong type, fails the load with its path, e.g. `state.flights.f1.aircraft: expected int`.

**Writes:**
- Each write is atomic: a temp file, fsync, then rename.
- The previous five saves are kept as `<name>.1` to `<name>.5`.
- A state that can't be encoded raises before any file is touched.

## Restarts

| What restarted | What happens on load and reconnect |
|---|---|
| Engine only (mission still running) | The bridge is seeded with the saved `runs`, so the mission's hello counts as the same run. The mission replays every event after the saved seq, which covers both those the old engine never applied and those that happened while it was down. Groups still in DCS are adopted with their bookkeeping, not respawned. |
| Mission only | A new run: everything the engine thought was spawned is aggregated, then the bubble respawns it from the state, losses included. Airborne flights resume in the air, and flights that never took off go back to their squadron. |
| Both | As for a mission restart, after loading the save. |

**Limits:**
- The mission replays at most the last 2000 events. An engine that's down long enough for more to happen loses the oldest, and the bridge logs a seq gap.
- A save made while a spawn or despawn command is waiting on its ack stores the pre-command state. If the engine stops before the ack, the group it spawned is removed as an orphan on reconnect. A group it despawned is respawned when the bubble next wants it.
- Timers are kept as ages, so time while the engine is down doesn't count towards dwell or combat pins.

## Verified in DCS (2.9, Run 6)
- Restarting the engine while the mission kept running: the mission resumed as the same run, and a kill made while the engine was down was replayed once and counted. The spawned formation was kept, not respawned or duplicated.
- Restarting the mission, and restarting both: losses persisted, and formations respawned at their reduced strength.
- `--new` started over, and the previous save was kept.
- An AI flight that was taxiing when the engine restarted carried on, landed and was recovered.

## Demos

- `python3 -m tools.bubble_demo` and `python3 -m tools.air_demo` load `build/saves/bubble.json` and `build/saves/air.json` at startup.
- While running, they save every 5 s if anything changed, and once more on Ctrl-C.
- `--new` starts over. `--save PATH` picks another file, for example an older `bubble.json.2`.
- `build/` is git-ignored, so saves stay local.
