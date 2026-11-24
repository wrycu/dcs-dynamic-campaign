# AI flights

How a campaign `Flight` becomes a DCS group and back. The code is in `campaign/flight_spawn.py` (building the group table), `campaign/air.py` (`AirSpawner`, the lifecycle) and `campaign/payloads.py` (loadouts). `tools/air_demo.py` is the in-game test (Run 5 in `testing-in-dcs.md`).

## Building a flight

- **Names:** group `fl:<flight id>`, units `fl:<flight id>#<n>`.
- **Callsigns and board numbers:** each spawned flight gets a number from 1 to 89 that no other spawned flight on its side is using. Board numbers are `<number><unit>`, e.g. `131`, `132`. Blue callsigns use the NATO names, e.g. Springfield 4-1; red uses numeric callsigns such as 241.
- **Starts** (`Flight.start`):

  | Start | DCS first point | Needs |
  |---|---|---|
  | `AIR` | none; units spawn at the first waypoint, or where the flight was aggregated | – |
  | `RUNWAY` | `TakeOff` / From Runway | airbase |
  | `RAMP_HOT` | `TakeOffParkingHot` | airbase, one free parking spot per aircraft |
  | `RAMP_COLD` | `TakeOffParking` | as above; can be held `uncontrolled` until a `Start` command |

- **Waypoint actions:**
  - `ORBIT`: circle the point for a set time.
  - `CAS`: engage ground units in a zone.
  - `SEAD`: engage air defences in a zone.
  - `BOMB`: bomb a point.
  - `EXIT`: leave the map; reaching it counts as getting home.
  - `LAND`: land at an airbase; must be the last waypoint.
- **On every flight's first point:** weapons free, and RTB on bingo fuel. Fighter missions also engage air targets within 60 km (32 nm) of their route.
- **Loadouts** are the stock DCS ones, by name, from `data/payloads.json`. `python3 -m tools.extract_payloads` rebuilds it from the install after a DCS update. Fuel is a fraction; the bridge converts it to kilograms per aircraft type.

## Lifecycle

```
PLANNED --spawn--> TAXI --takeoff--> ENROUTE --all landed--> LANDED --all shut down--> recovered
          (air start goes straight to ENROUTE)     \--every aircraft killed--> LOST
```

- **Squadron stock:** a flight's aircraft leave the squadron when it first launches. Survivors return when it's recovered: landed and shut down, or reached its `EXIT` waypoint.
- **Losses:** each kill reduces `Flight.aircraft`, once per DCS unit name per spawn.
- **Watchdogs:**
  - A ramp or runway flight that hasn't taken off 15 minutes after starting is aborted, and its aircraft are returned. This guards against taxi deadlocks.
  - A landed flight that hasn't shut down within 10 minutes is recovered anyway.
- **Aggregation:** an airborne flight can be taken out of DCS. Its position, altitude, heading, speed, fuel and next waypoint are kept, and a later spawn resumes it in the air from there.
- **Mission restart, or the group vanishing:**
  - Airborne flights become aggregated and resume in the air.
  - Flights that never took off go back to PLANNED, with their aircraft returned.
- **Orphans:** unknown `fl:` groups in DCS are removed.

## Verified in DCS (2.9, Run 5, FA-18C and MiG-29A)
- Air starts fly their route and fight. Blue and red CAPs engaged each other, and losses were counted.
- Hot ramp start: the flight taxied, took off, attacked the CAS target, landed and was recovered. The warehouse count dropped by 2 at spawn and came back on recovery.
- A cold start held `uncontrolled` stays parked until the `Start` command, then starts up and flies its route.
- An airborne flight taken out of DCS and put back reappears where it was and carries on.
- An empty aircraft stock makes DCS create no aircraft, and `spawn_group` now fails with that reason. Empty munitions give gun-only jets (see Munitions logistics below).
- Callsigns and board numbers show correctly on the F10 map and in F2.

## Flight bubble

`campaign/air_bubble.py`, in-game test `tools/flight_bubble_demo.py` (Run 7). Settings are in `[bubble]`: `air_spawn_nm` (60), `air_despawn_nm` (80), `air_min_dwell_s` (120) and `max_flights` (12).

- **In DCS near players only.**
  - An airborne flight is spawned with an air start, where the engine has it, once a player is within `air_spawn_nm`.
  - It leaves DCS once every player is beyond `air_despawn_nm`, it has been in DCS for `air_min_dwell_s`, and it hasn't been in a fight for 5 minutes. A fight is a kill, a hit or a loss involving the flight.
  - Flights on the ground in DCS (taxiing, landed) stay there until `AirSpawner` recovers them.
- **Takeoff:** at `takeoff_time` on the campaign clock.
  - If a player is within `air_spawn_nm` of the flight's base, it starts on the ramp or runway in DCS.
  - Otherwise it takes off without DCS, from its base, or from its first waypoint if it is off-map or air-started.
  - Either way, the aircraft leave the squadron at that moment.
- **Without DCS:**
  - Flights fly their route at cruise speed (220 m/s, about 430 kt) and hold at an ORBIT for its duration.
  - At LAND or EXIT they're home, and the aircraft return to the squadron.
- **Campaign clock:** follows DCS model time from telemetry, so time acceleration speeds up the whole war and a paused mission stops it. A single jump is capped at 120 s. It is saved with the campaign.
- **Orbits:**
  - An ORBIT stays the flight's next waypoint until its time is up, counted on the campaign clock from when the flight first got there, whether in DCS or not.
  - A flight taken out of DCS mid-orbit comes back for the rest of the orbit only.
- **Cap:** at most `max_flights` in DCS; the flights closest to a player go first. A due flight that doesn't fit takes off without DCS.
- **Non-combat missions** (`FERRY`, `TRANSPORT`) have weapons hold. An empty loadout name means a clean aircraft.

### Verified in DCS (Run 7, FA-18C)
- Due flights take off without DCS when no player is near their base, and their engine positions move along the route (F10 text markers).
- An airborne flight spawns where the engine has it once a player is within range, at the right altitude, and flies on normally.
- A ramp start near a player's base taxis, takes off and attacks its target.
- A flight beyond the despawn range leaves DCS. Its orbit time keeps counting down outside DCS, and a respawn mid-orbit continues the orbit.
- A ferry flight spawns on approach, lands and is recovered to its squadron.
- Time acceleration speeds up the campaign clock. A flight that reaches EXIT outside DCS returns its aircraft to the squadron.

## Not done yet
- **Fighting without DCS.** Flights that aren't in DCS never meet. Air combat away from players needs an abstract resolution, e.g. odds from types, numbers and loadouts.
- **Fuel without DCS.** Fuel isn't burned on the engine's legs, and a flight's bingo in DCS (RTB) isn't reflected in its route. If a flight that turned home early is taken out of DCS, it goes back to its route.
- **Weapons after a respawn.** `despawn_group` reports the remaining `ammo`, but a respawned flight gets its full loadout back. Mapping the remaining weapons back onto pylons needs a weapon → pylon catalog.
- **Parking choice** is naive: the first free fighter-sized spots, with shelters first.
- **Radio frequencies** are left to DCS defaults.
- **Rules of engagement are a stopgap.** CAS, SEAD and strike flights use ROE open fire, so they engage only what their tasks name. Weapons free let Run 15's CAS lead leave its target for an SA-6 32 nm away. But open fire may also make them ignore pop-up threats and other obvious targets near their route. A better answer is probably weapons free with limits: a route corridor for targets of opportunity, a threat reaction, or ROE switched per leg (free near the target, open fire en route). This hasn't been checked in DCS yet.
- **Munitions logistics.** AI ramp and runway starts take their weapons and drop tanks from the airbase warehouse. If a limited warehouse doesn't have them, the jets spawn with only the gun, silently (verified on DCS 2.9). The test mission keeps munitions unlimited. Tracking munitions would need a map from loadout pylons (CLSIDs) to warehouse weapon names. An empty *aircraft* stock is different: DCS creates no aircraft at all, and `spawn_group` fails.
