# Bubble: aggregated vs spawned ground forces

The campaign state is the truth. A DCS group is a temporary projection of a `GroundUnit`, and it exists only while a player could see it, shoot it, or be shot by it.

| Module | Role |
|---|---|
| `campaign/bubble.py` | Pure planner: which formations should be spawned now |
| `campaign/ground_spawn.py` | `GroundUnit` → `spawn_group` payload (formation layout, names, country) |
| `campaign/spawner.py` | `GroundSpawner`: runs the plan through the bridge and folds DCS results back into the state |
| `campaign/catalog.py` | Air-defence ranges, SAM core types, side → country |

## Rules

- **Observers:** every player aircraft from telemetry. Each observer also has a look-ahead point (`lookahead_s`, 60 s along its velocity), so fast jets don't outrun the bubble.
- **Spawn and despawn distances** are set in `config/campaign.toml` (`[bubble]`, in nm). A formation spawns when a player comes within its spawn distance, and despawns only once every player is beyond its despawn distance. The despawn distance must be at least the spawn distance; the gap stops flicker at the edge.
  - **Ordinary formations:** `ground_spawn_nm` (20) and `ground_despawn_nm` (25).
  - **Anything with air defences:** its missile range plus `sam_spawn_margin_nm` (10) or `sam_despawn_margin_nm` (15), or the ground distances if those are larger. An SA-11 (18.9 nm) spawns at 28.9 nm, so it's up and searching before a player enters its envelope.
- **Dwell:** a fresh spawn stays at least `min_dwell` (90 s).
- **Combat pin:** a formation that kills or loses a unit stays spawned for 180 s.
- **Budget:** `max_units` (300) live vehicles.
  - Candidates are ranked by distance ÷ spawn distance.
  - At most `max_spawns_per_tick` (4) spawn per tick, to spread out frame hitches.
  - When over budget, the least relevant formation that is neither pinned nor young is shed first.
- **No players:** everything despawns once its dwell has passed, so an empty server runs the whole war aggregated.

## Losses

- `unit_killed` for `gu:<id>#<n>` decrements the composition, once per DCS unit name per spawn.
- On despawn, DCS's `survivors` replace the composition. Every unit name not in `alive` counts as already dead, so a kill event queued behind the despawn ack is ignored rather than counted twice.
- A formation with no vehicles left is destroyed. DCS removes the group itself.

## Reconciliation

- **New run** (mission restarted): every SPAWNED formation becomes AGGREGATED. The next tick respawns what's in range, with its losses.
- **Same run, reconnect:**
  - SPAWNED formations missing from `hello.spawned` become AGGREGATED; replayed kill events still apply.
  - `gu:` groups that DCS has but the engine doesn't know about are despawned as orphans.
- **Missing from telemetry** two reports in a row: aggregated.
- **Failed spawn:** retried after 60 s. If the failure was "already exists", that group is removed as an orphan first.

## Verified in DCS (2.9.31, 2026-11-16)

Everything in Run 3 of `testing-in-dcs.md` passed:
- spawn and despawn by distance, with hysteresis;
- loss counting, with no double counts, and losses persisting through a respawn and a mission restart;
- the combat pin;
- the SA-6 spawning outside its envelope.

The Hornet data cartridge loads its waypoints after the MUMI transfer.

## Not done yet

- **Movement.** Spawned formations hold position; nothing gives them a route from their `GroundOrder`. Aggregated movement belongs to the ground-war simulation.
- **Terrain checks.** Layouts are not checked for water or buildings. A `query` for `land.getSurfaceType` would let the engine nudge positions.
- **AI-only fights.** Only players are observers. Two AI forces meeting far from players need abstract resolution, or the AI flight involved could be added as an observer.
- **Flights.** AI flights are always spawned by the ATO for now. A flight bubble (aggregating AI flights far from players) is a later step.
- **Shots.** The bridge doesn't forward `S_EVENT_SHOT`, so a SAM that fires without killing anything isn't pinned. Its despawn radius is far outside its envelope, so this should be harmless.
- **SAM emission control.** A SAM spawns radiating: its search radar comes up as soon as it's spawned, 5 nm or more outside its missile range. In the 2026-11-16 test, the SA-6 locked the player almost immediately at about 16.9 nm. Real sites, and BMS, keep radars dark or radiate on a schedule, so a pilot can't count on the RWR to tell them where a SAM is. To do:
  - Give each air-defence site a doctrine in the campaign model: always on, on a schedule (on/off windows, randomized per site), or ambush (dark until a target is within a fraction of its range).
  - Apply it after the spawn: set the group's alarm state to GREEN (dark) or RED (radiating) from a timer, or set emissions directly where the unit supports it.
  - The site keeps its on/off state across spawns, so its timetable doesn't restart every time it spawns.
  - Despawning a radiating site resets nothing in the campaign model.
  - A HARM shot (needs `S_EVENT_SHOT`, see **Shots**) can make a site shut down for a while.
