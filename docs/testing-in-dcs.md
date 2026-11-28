# Testing the bridge in DCS (Lutris / Proton)

Paths below assume the Lutris prefix at `~/Games/dcs-world`. Run all commands from the repo root.

```bash
DCS="$HOME/Games/dcs-world/drive_c/Program Files/Eagle Dynamics/DCS World"
SG="$HOME/Games/dcs-world/drive_c/users/tim/Saved Games/DCS"
```

## One-time setup

**1. Let missions load LuaSocket.** DCS 2.9.31 removes `require` and `package` from the mission environment. This comments those two lines out and keeps a backup:

```bash
cp "$DCS/Scripts/MissionScripting.lua" "$DCS/Scripts/MissionScripting.lua.orig"
sed -i "s/^\(\s*\)_G\['require'\] = nil/\1--_G['require'] = nil/; s/^\(\s*\)_G\['package'\] = nil/\1--_G['package'] = nil/" \
  "$DCS/Scripts/MissionScripting.lua"
grep -n "_G\['require'\]\|_G\['package'\]" "$DCS/Scripts/MissionScripting.lua"   # both lines should start with --
```

- Only test item 2 and the smoke test need this.
- DCS updates and repairs overwrite the file, so re-run it afterwards.
- To undo: `mv "$DCS/Scripts/MissionScripting.lua.orig" "$DCS/Scripts/MissionScripting.lua"`.
- Desanitizing lets any mission you run (including missions downloaded from multiplayer servers) load native code. Undo it when you're not testing, if that matters to you.

**2. Build and install the test missions.**

```bash
python3 -m tools.build_test_missions
mkdir -p "$SG/Missions/campaign-dev"
cp build/campaign-*.miz "$SG/Missions/campaign-dev/"
```

- Both missions come from your `a2a.miz`: Syria, an FA-18C at about 10,000 ft, about 4–5 nm south of Khalkhalah, and Khalkhalah set to blue with limited aircraft (munitions unlimited).
- Each mission's only trigger loads its Lua straight from this repo through Wine's `Z:` drive. **Editing the Lua only needs a mission restart.** You only rebuild and copy if the builder itself changes.

## Run 1: API probe (`campaign-probe.miz`)

1. **Terminal A:** start the smoke server, so the probe's socket test has an open port.
   ```bash
   python3 -m tools.smoke_server
   ```
   The probe connects and immediately disconnects, so `rejecting client ... invalid JSON` in this terminal is expected.
2. **Terminal B:** watch the log.
   ```bash
   tail -F "$SG/Logs/dcs.log" | grep --line-buffered -e '\[probe\]' -e '\[loader\]'
   ```
3. Launch **DCS World** from Lutris (VR or flat). Go to MISSION → My Missions → `campaign-dev/campaign-probe.miz` and take the FA-18 slot.
4. Within about 1 s you should see `[loader] running Z:\...\probe.lua`. After about 25 s a summary appears on screen and in the log.
5. Things you'll see happen:
   - Two vehicles appear next to Khalkhalah. One vanishes (despawn); the other is blown up.
   - A building on the airbase, ideally the tower, is blown up.
6. Exit the mission. Nothing needs copying, because the results are already in `dcs.log`.

Reading the results:

| Item | Good result | If not |
|---|---|---|
| 1 json | `PASS`, `round trip ok` | `net.*` is missing from the mission environment, so a JSON library has to be bundled. |
| 2 socket | Closed port `connected=false`, open port `connected=true`, `blocked` ≈ 0 ms | Large `blocked` = connect stalls frames. `sanitized` = setup step 1 wasn't applied. |
| 3 destroy | Shows which events `Group:destroy()` fires, compared with a real kill | If destroy fires loss events later than 5 s, raise the suppression window. |
| 4 warehouse | Real types accepted, `NOT_A_REAL_TYPE` rejected; the inventory dump shows the key format | Rejected real types need the naming the inventory dump uses. |
| 5 scenery | The known tower id is `FOUND`, and the DEAD event's name matches | Use whichever field matched as `Feature.dcs_ref`. |

> **Runs 3 and 5–8 used demos that are now retired** (`tools.bubble_demo`, `tools.air_demo`, `tools.flight_bubble_demo`; still in git history). Their scenario lives on as `python3 -m campaign.server --scenario khalkhalah` (Run 15), and ghosts as `--ghost`.

## Run 2: end-to-end bridge (`campaign-smoke.miz`)

1. **Terminal A:** `python3 -m tools.smoke_server`.
2. Load `campaign-dev/campaign-smoke.miz` and take the FA-18.
3. **In terminal A**, expect:
   - `HELLO mission ... map=Syria`;
   - `RESULT query time`, `RESULT query airbase` (with Khalkhalah's position) and `RESULT spawn_group`;
   - telemetry every 3 s;
   - after 20 s, `RESULT despawn_group` showing `{"M-1 Abrams": 1, "Hummer": 1}` with no `unit_killed` lines for `gu:smoke1`.
4. **In game**, expect:
   - the message "Campaign smoke test connected";
   - on the F10 map, a red circle on Khalkhalah and a "smoke test group" mark with two vehicles until the despawn;
   - F10 radio → Other → Campaign → Ping, which prints a `menu_select` event in terminal A.
5. **Reconnect test:**
   - Ctrl-C terminal A and pick F10 → Campaign → Ping while the server is down.
   - Restart the server within a minute.
   - You should see `HELLO` again and the `menu_select` you made while it was down.
   - The scenario also runs again, because the smoke server doesn't persist run state.
6. **Mission restart test:** restart the mission. Expect a new `run=` id, and `seq` starting at 1 again.

## Troubleshooting

- **`LOADER FAILED` on screen:** the Proton container can't see the repo under `Z:\home\...` or `Z:\var\home\...`. The `[loader]` lines in `dcs.log` show the error for each path tried.
- **Nothing at all in `dcs.log`:** check that the mission's trigger exists. In the Mission Editor, open Triggers and look for one MISSION START with DO SCRIPT.

## Hooks (needs multiplayer; a listen server you host is enough)

1. Install the Hooks script and restart DCS. It is loaded at DCS start, not at mission start.
   ```bash
   cp lua/hooks/campaign_hooks.lua "$SG/Scripts/Hooks/"
   grep -n "CAMPAIGN" "$SG/Logs/dcs.log"   # after DCS starts: "campaign hooks loaded (run ...)"
   ```
2. Start `python3 -m tools.smoke_server`.
3. Host `campaign-smoke.miz` via MULTIPLAYER → New server.
4. Stay in **spectators** at first. The Hooks script connects once the mission is running:
   - `HELLO hooks ...`
   - `SLOT TEST: blue is now BLOCKED`
5. Pick the blue FA-18 slot. Expect:
   - a chat message, "That side is AI-only in this campaign.";
   - a `hooks player ... slot_denied` line carrying your UCID;
   - `SLOT TEST: denial worked; blue is ALLOWED again`.
6. Pick the blue slot again. Expect a `hooks player ... player_slot` line with your UCID and `unit_type`.

If step 5 lets you in with no `slot_denied`, DCS does not run `onPlayerTryChangeSlot` for the host. Slot gating would then need a second client (or a dedicated server) to test.

**Verified on a dedicated server (Run 8, 2026-11-18):** a remote client's slot was refused with the chat message, then allowed after the rules changed; `player_slot`, `player_disconnect` and remote-player telemetry all arrived.

To uninstall: `rm "$SG/Scripts/Hooks/campaign_hooks.lua"`.

## Run 3: bubble (`campaign-smoke.miz` + `tools.bubble_demo`)

Single player. About 25 minutes of flying. You only need the Hornet's gun. Every distance in the game messages is in nm and every bearing is magnetic.

### Setup
```bash
cd ~/PycharmProjects/dcs-dynamic-campaign
python3 -m tools.bubble_demo --dtc-dir "$HOME/Games/dcs-world/drive_c/users/tim/Saved Games/DCS/DTC"
```
Start `build/campaign-smoke.miz`. In the briefing, load `campaign_bubble.dtc` (see Run 4), press FLY, then transfer it with MENU → **MUMI** → **WYPT**. You start in the FA-18C about 4–5 nm south of Khalkhalah at about 10,000 ft. The jet has no pylons, only the gun.

### The layout
Ground formations spawn within **8 nm** and despawn beyond **10 nm**. The SA-6 uses its missile range (11.9 nm) plus 5 nm to spawn and plus 9 nm to despawn: it spawns within **16.9 nm** and despawns beyond **20.9 nm**. These are the demo's short test distances. Change them with `--spawn`, `--despawn`, `--sam-margin` and `--sam-despawn-margin` (nm), or use the campaign's real ones with `--config config/campaign.toml`.

```
            N
            |  WP4 mech1   24 nm N   (BTRs + trucks)
            |
            |  WP3 armor1  13 nm N   (T-72s + BMPs)
            |
            |  WP2 near1    3 nm N   (2 BTR + 2 Ural)
  WP6 Khalkhalah +---------------------------------- WP5 sa6  32 nm E (SA-6, LIVE)
            |  WP1 blue1    2 nm S   (4 Abrams, friendly)
            |
             YOU  ~4-5 nm S, ~10,000 ft
```

Every spawn, despawn and loss appears as an in-game message with your bearing and range at that moment, and as an `MSG` line in the terminal. **F10 > Other > Campaign > Status** lists every formation with its state, vehicle count, bearing and range from you, spawn radius and despawn range. **Navigate to** gives a steer every 10 s as a backup to the waypoints. Spawned formations are marked with red or blue smoke.

### Steps

1. **Initial spawn** (no flying). blue1 spawns within a few seconds. near1 (about 8 nm away) spawns either right away or as soon as you head north. Open **Status**: armor1, mech1 and sa6 should be `aggregated`.
2. **Losses.** Fly to **WP2** and descend. Gun down **two** of near1's vehicles; the Urals are easiest. Expect `near1 lost a Ural-375: 3 left`, then `2 left`, and the F10 label should drop to `2 veh`. Note the time of your last kill. near1 is now pinned and stays spawned for 3 minutes whatever you do.
3. **Spawn by distance.** Climb back to about 10,000 ft and fly toward **WP3** at about 350 kt.
   - About 5 nm north of the field: `armor1 SPAWNED (… 8.0 nm from you)`.
   - About 8 nm north: `blue1 despawned (… 10.0 nm from you)`.
4. **Pin, then despawn.** Continue to **WP4**. About 16 nm north of the field: `mech1 SPAWNED`. near1 despawns once you're more than 10 nm from it (13 nm north of the field) **and** 3 minutes have passed since your last kill, whichever comes later.
5. **Respawn with losses.** Turn back toward **WP2**. Behind you, mech1 despawns about 14 nm north of the field and armor1 about 3 nm north. near1 respawns about 11 nm north of the field with **exactly 2 BTRs, no Urals**. Fly over it to count.
6. **SAM spawns before its envelope.** From **WP6**, fly toward **WP5** at 10,000 ft or higher.
   - Expect `sa6 SPAWNED` at 16.9 nm, about 15 nm east of the field, and SA-6 search radar on your RWR within seconds. That's the real check: the site is up before you're in its range.
   - **Turn back before you're within 13 nm of WP5.**
   - It despawns once you're beyond 20.9 nm.
7. **Restart persistence.** Restart the mission (LShift+R) with the demo still running. The terminal shows `HELLO mission … new_run=True`. near1 spawns again with **2** vehicles. Reload the cartridge and redo the MUMI transfer.
8. **Optional, look-ahead.** Stop the demo (Ctrl+C), which resets all losses, and restart it with `--lookahead 60`. Fly step 3 again at 350 kt (about 5.8 nm a minute). armor1 should spawn about **14 nm** away instead of 8: the radius plus 60 s of flight.

### What to send back
- **Terminal output.** The `MSG` lines and the `--- N player(s)` blocks are enough. Leave out the `INFO campaign.bridge` lines.
- **Any `spawn … failed` or `despawn … failed` line.** A failed spawn most likely means DCS doesn't know a unit type name.
- **Anything that looked wrong** when you flew past: vehicles in water or inside buildings, a messy layout, SA-6 launchers not facing outward, a stutter when a group spawned.

## Run 4: data cartridge waypoints (FA-18C)

The engine writes native `.dtc` files (`campaign/dtc.py`), and players import them with the in-sim DTC manager, so no `.miz` reload is needed. The format was read from the DCS 2.9.31 install but hasn't been loaded in game yet; that's what this run checks.

**The DTC window only opens on the ground or before you spawn.** You can open it from:
- the briefing screen's **Open DTC** button, before you press FLY (offered once per slot);
- the dynamic-slot spawn dialog;
- the cockpit, only on the ground: F8 → Ground Crew → DTC Interface, or RShift+RCtrl+K. It is a ground-crew request.

The smoke mission is an air start, so use the briefing button.

1. Start the demo so it writes the cartridge straight into your DTC folder:
   ```bash
   python3 -m tools.bubble_demo --dtc-dir "$HOME/Games/dcs-world/drive_c/users/tim/Saved Games/DCS/DTC"
   ```
   It writes `campaign_bubble.dtc` immediately, with every elevation set to 0: WP1 blue1, WP2 near1, WP3 armor1, WP4 mech1, WP5 sa6, WP6 Khalkhalah.
2. Load `campaign-smoke.miz`. On the briefing screen, click **Open DTC**, import `campaign_bubble.dtc`, and load it. Then press FLY.
3. **Transfer the data into the jet:** MENU (SUPT) → **MUMI**. Make sure **WYPT** is boxed, then let the transfer run. Loading in the DTC window only inserts the cartridge; until you do this, the HSI shows only waypoint 1.
4. On the HSI (WYPT, up and down arrows), step through WP1–6. WP1 is blue1, 2 nm south of Khalkhalah; WP2 is on near1's smoke; WP6 is the field.
5. Once the mission connects, the terminal prints `DTC written` again with real terrain elevations. That rewrite only affects the next load.

The demo also writes `campaign_bubble_seq.dtc`: the same points as route sequence 1, which hasn't been tested yet.

If the import fails or the waypoints are wrong:
- Check `dcs.log` for errors mentioning DTC or JSON.
- In the same DTC window, make a cartridge by hand with two waypoints and **Export** it, e.g. as `reference.dtc`. Send it to me so I can match the exact format your DCS writes.

## Run 5: AI flights (`campaign-smoke.miz` + `tools.air_demo`)

Single player, about 45 minutes. Time acceleration is fine while you wait; the scripts keep working. Distances are nm, altitudes ft, bearings magnetic.

This run answers the questions that decide how the campaign handles AI flights:
- Do air-started flights fly their route and fight?
- Do ramp starts work, both hot and cold-held-then-started?
- Do AI ramp spawns take aircraft out of the airbase warehouse, and does landing put them back?
- Do we get the takeoff, landing, engine-shutdown and loss events needed to collect a flight once it's home?
- Can an airborne flight be taken out of DCS and put back where it was?

### Setup
```bash
python3 -m tools.air_demo        # not the bubble demo; only one can listen on the port
```
Start `build/campaign-smoke.miz`. You don't need a cartridge for this run. Everything is in **F10 > Other > Campaign**: **Spawn**, **Control**, **Warehouse** and **Status**. Status lists every flight with its state, aircraft count, bearing and range from you, altitude and fuel, plus how many aircraft each squadron has ready.

A red target group (4 BTRs) sits 12 nm north of Khalkhalah for the CAS flight.

**The red CAP is armed and will attack you.** It engages anything within about 32 nm of its route, which runs from 40 nm north of the field to 20 nm north. Your jet only has a gun, so keep more than 35 nm from it (check Status) unless you want to test that.

### Steps
1. **Warehouse baseline.** Warehouse > **Hornet count at the field**. Note the number; it may well be 0.
2. **Hot ramp start.** Spawn > **Blue CAS**. Then:
   - If it fails, note the message (for example, no free parking or an empty warehouse). Then use Warehouse > **Stock 4 Hornets** and spawn again.
   - When it spawns, a warehouse message follows. **Did the count drop by 2?**
   - Orbit the field and watch: two Hornets start on the ramp, taxi and take off. Expect `took off` within about 5 minutes and note how long it took.
   - They fly to the BTRs 12 nm north. Expect `Target group lost a BTR-80`.
   - They come back and land. Expect `landed`, then `recovered: 2 aircraft back to the squadron` once both have shut down, and another warehouse message. **Did the count go back up?**
3. **Cold start, held.** Spawn > **Blue cold start, held**.
   - Two Hornets should appear parked and stay cold. Check on the F10 map or fly past.
   - After a minute, Control > **Start the held flight**. Expect a cold start-up, taxi and takeoff; note how long until `took off`.
   - They orbit 5 nm northwest of the field for 5 minutes, then land.
4. **Air-to-air.** Stay south of the field.
   - Spawn > **Blue CAP**: two Hornets appear over the field at 18,000 ft and orbit 10 nm north.
   - Spawn > **Red CAP**: two MiG-29s appear 40 nm north at 20,000 ft.
   - Expect a fight 10–25 nm north of the field, with messages such as `lost MiG-29A: 1 left`.
   - A side that loses its last aircraft is reported `destroyed`.
   - The blue CAP lands at Khalkhalah when its orbit ends.
5. **Out of DCS and back.** If the red CAP from step 4 is gone, spawn another.
   - Wait until Status shows it at least 10 nm from where it appeared.
   - Control > **Take the red CAP out of DCS**. The message gives its altitude and fuel, and the jets vanish from the F10 map.
   - Wait about 30 seconds, then Control > **Put the red CAP back**. They should reappear at the same place, heading and altitude, and carry on with the route.
   - **Watch for** a stall or sudden drop right after respawning, a wrong heading, or fuel that doesn't match.
   - Known limitation: they come back with a full weapon load.
6. **Leaving the map.** A red CAP that survives its 15-minute orbit flies 80 nm north. When it gets there, expect `left the map: 2 aircraft back to the squadron`. Skip this if you're short on time.

### What to send back
- The terminal lines starting with `MSG`, `event`, `parking` and `WARNING`.
- The warehouse numbers from steps 1–2, and the takeoff times from steps 2–3.
- Any `failed` message, and anything the AI did that looked wrong: stuck taxiing, taking off from the wrong place, never attacking, or landing somewhere else.

## Run 6: save and load (`campaign-smoke.miz` + `tools.bubble_demo`)

Single player, about 20 minutes. The same layout and cartridge as Run 3. This run checks that losses survive three kinds of restart:
- restarting the Python script while the mission keeps running;
- restarting the mission;
- restarting both.

Both demos save to `build/saves/<demo>.json` every 5 s while something changes, and when you stop them with Ctrl-C. They load that file at startup. `--new` starts over; the old save is kept as `<demo>.json.1`.

### Setup
```bash
python3 -m tools.bubble_demo --new --dtc-dir "$HOME/Games/dcs-world/drive_c/users/tim/Saved Games/DCS/DTC"
```
Start `build/campaign-smoke.miz` and load the cartridge as in Run 3. The terminal prints `SAVE new campaign`.

### Steps
1. **Take losses.** Fly to WP2 (near1, 3 nm north of the field) and destroy 2 of its 4 vehicles with the gun. F10 > Other > Campaign > **Status** should show near1 with 2 vehicles. Stay within 5 nm of near1 from here on, so it stays spawned.
2. **Restart the script while the mission runs.**
   - In the terminal, press Ctrl-C. Expect `SAVE saved ...`.
   - **While it's stopped**, destroy a third near1 vehicle. DCS queues that event.
   - Start the demo again with the same command, **without** `--new`. Expect:
     - `SAVE loaded ...`, then a status list with near1 at 2;
     - `mission connected ... resuming after seq N` (not `new run`);
     - the queued kill arrives once, and near1 drops to 1.
   - near1 must **not** respawn or duplicate: no `spawned near1` line, and its last vehicle hasn't moved.
3. **Restart the mission.** In DCS, restart the mission and load the cartridge again. Expect a `new run` line in the terminal. Fly back to WP2: near1 should spawn with **1 vehicle**.
4. **Restart both.**
   - Quit the mission to the main menu. Press Ctrl-C in the terminal.
   - Start the demo again, then the mission.
   - Status should still show near1 at 1, and every other formation unchanged.
5. **Start over.** Ctrl-C, then start the demo with `--new`. near1 is back to 4. `build/saves/bubble.json.1` holds the previous campaign.

**Optional, AI flights (Run 5 setup, `python3 -m tools.air_demo`):**
- Stock 4 Hornets and spawn **Blue CAS**.
- While it taxis, press Ctrl-C and start `python3 -m tools.air_demo` again.
- The flight should carry on without being respawned. Expect `took off`, the attack, `landed`, then `recovered: 2 aircraft back to the squadron`.
- Status should show the squadron count back up.

### What to send back
- The terminal lines starting with `SAVE`, `HELLO`, `mission connected`, `spawned` and `despawned`, plus any `WARNING` or `ERROR` lines.
- The near1 count after each step.
- Anything that respawned or doubled when it shouldn't have.

## Run 7: flight bubble (`campaign-smoke.miz` + `tools.flight_bubble_demo`)

Single player, about 35 minutes (less with time acceleration). Only the Hornet's gun is needed; nothing in this run shoots at you. In this demo, AI flights **spawn within 30 nm of you** and **leave DCS beyond 40 nm**, but only after at least 1 minute in DCS.

### Setup
1. Start the demo. It writes `campaign_flights.dtc` straight into your DTC folder:
   ```bash
   python3 -m tools.flight_bubble_demo --new --dtc-dir "$HOME/Games/dcs-world/drive_c/users/tim/Saved Games/DCS/DTC"
   ```
   Note the `scenario: far base <name> (<n> nm from Khalkhalah)` line. That's where Blue Ferry comes from.
2. Start `build/campaign-smoke.miz`. In the briefing, load `campaign_flights.dtc` (as in Run 4), press FLY, then MENU → **MUMI** → **WYPT**. The waypoints:

   | WP | Where | Used for |
   |---|---|---|
   | 1 | 8 nm north of Khalkhalah | where you watch Red Ferry spawn |
   | 2 | 35 nm north of Khalkhalah: **Red Ferry's orbit** | HSI range to WP2 = range to Red Ferry while it orbits |
   | 3 | Khalkhalah | |
   | 4 | 10 nm south of Khalkhalah (45 nm from WP2) | where you hold while Red Ferry is out of DCS |

3. You start about 4–5 nm south of Khalkhalah at about 10,000 ft. The mission clock starts at **T+0:00**. Status shows the campaign time as `T+h:mm:ss`.

**Where to look:**
- **F10 map:** a text marker for every flight, at the position the engine has for it: `[DCS]` = in DCS, `[engine]` = only the engine is flying it.
- **F10 > Other > Campaign > Status:** your bearing and range to Khalkhalah, then for each flight its state, magnetic bearing and range from you, altitude, and any orbit time left. Messages appear on screen and in the terminal.

### The scenario
| Flight | Plan | Armed? |
|---|---|---|
| **Red Ferry** | 2 MiG-29A. Takes off off-map at T+0:00, starting 60 nm north of the field at 20,000 ft. Flies south to WP2 (arriving about T+3:30) and orbits there for 15 min (until about T+18:30). Then flies 55 nm north and leaves the map (about T+26). | No, and weapons hold |
| **Blue Ferry** | 2 F/A-18C from the far base. Takes off at T+1:00 and flies to Khalkhalah to land. | No |
| **Blue CAS** | 2 F/A-18C, hot ramp start at Khalkhalah at T+5:00. Attacks the 4 BTRs 12 nm north of the field at 12,000 ft, then lands at Khalkhalah. | Yes, at ground targets only |

Times are approximate; they follow the AI's cruise speed of about 430 kt.

### Steps
**1. Takeoffs without DCS (T+0:00 to T+1:00).** Don't move yet.
- Within a few seconds of the mission starting, expect the message `Red Ferry: took off (not in DCS)`.
- F10 map: the `Red Ferry [engine] 20k ft` marker is 60 nm north of the field. Check it again after 30 s: it should have moved south about 3.5 nm.
- Status: `Red Ferry: 2x ENROUTE [engine] 355M ~64 nm 20,000 ft`.
- At about T+1:00, expect `Blue Ferry: took off (not in DCS)`, and a `Blue Ferry [engine]` marker appears over the far base.
- ✅ Pass: both took off without any jets appearing in DCS, and their markers move.

**2. Red Ferry spawns as you close (about T+1:00 to T+4:00).**
- Steer to **WP1**: about 355°M, 12–13 nm. Fly at 10,000 ft and about 350 KIAS, then orbit WP1.
- Check Status every 30 s or so. When Red Ferry's range drops below **30 nm**, around T+3:00, expect `Red Ferry: spawned`. Its marker changes to `[DCS]`.
- Check:
  - Status shows it at about **20,000 ft**, and later `orbiting ~15 min more` once it reaches WP2.
  - On the radar (RWS, 40 nm scale, nose north), there are 2 contacts about 27–30 nm north, high.
  - On the F10 map, the two MiG icons are at the marker.
- ✅ Pass: they appear where the marker was and fly on normally: no stall, dive or sudden turn.

**3. Blue CAS ramp start (T+5:00 to about T+12:00).** Keep orbiting WP1 at 10,000 ft. You're 8 nm from the field, so inside 30 nm.
- At T+5:00, expect `Blue CAS: spawned`. Two Hornets appear on the ramp at Khalkhalah (F10 map).
- Within about 5 minutes: `Blue CAS: took off from Khalkhalah`. They fly north past you to the BTRs, 4 nm north of WP1, and you'll see `Target group lost a BTR-80` messages.
- Note the time of `took off`.
- ✅ Pass: it started in DCS on the ramp, not as an engine-only takeoff.

**4. Red Ferry leaves DCS when you leave (about T+8:00 to T+12:00).**
- Steer to **WP4**: about 175°M, 18 nm. Watch the **range to WP2** on the HSI or in the UFC.
- When it passes **40 nm** (about 5 nm south of the field), expect `Red Ferry: aggregated in the air` within a few seconds. The MiGs vanish from the F10 map, and the marker stays at WP2 as `[engine]`.
- Orbit WP4. Check Status twice, a minute apart: Red Ferry's `orbiting N min more` should go down by 1.
- ✅ Pass: it left DCS at about 40 nm, not before, and its orbit timer kept running.

**5. Red Ferry comes back, then leaves again (optional, about 5 min).**
- Fly back toward WP1. When the WP2 range drops below **30 nm**, expect `Red Ferry: spawned` again, still orbiting at WP2, with the same orbit time left as Status showed.
- Return to WP4. It leaves DCS again past 40 nm.

**6. Blue Ferry arrives (about T+10:00 to T+25:00).** Hold at WP4, or anywhere within about 10 nm of the field.
- Watch Blue Ferry in Status. When it's within **30 nm** of you, expect `Blue Ferry: spawned`: two clean Hornets in the air, heading for Khalkhalah at about 12,000–20,000 ft.
- Then expect `Blue Ferry: landed at Khalkhalah`. After they taxi in and shut down, which can take up to 10 minutes, expect `Blue Ferry: recovered: 2 aircraft back to the squadron`.
- Status: `Blue Hornets (<far base>): 8 ready`.
- Blue CAS lands the same way, and its squadron goes back to 8.
- ✅ Pass: Blue Ferry spawned in the air on its way in, landed, and was recovered.

**7. Time acceleration, and leaving the map without DCS (about T+18:30 to T+26:00).** Stay at WP4.
- Note the T+ time in Status, then press **Ctrl+Z** twice for ×4 (Shift+Z returns to normal).
- One real minute later, Status should show about 4 minutes more.
- After about T+18:30, the Red Ferry marker starts moving north from WP2.
- At about T+26, expect `Red Ferry: left the map (not in DCS): 2 aircraft back to the squadron`, and `Red MiG-29s (off map): 8 ready`.
- ✅ Pass: the clock and markers sped up ×4, and Red Ferry finished its route without being spawned.

### What to send back
- The whole terminal output, or at least the lines starting with `MSG`, `event`, `FLIGHTS`, `scenario`, `WARNING` and `ERROR`.
- For each air spawn (step 2, step 5 if you did it, step 6): the time, your range when it happened, and whether the jets appeared at the marker, at a sensible altitude and heading, and flew on normally.
- The step 3 takeoff time, and anything that spawned or vanished when it shouldn't have.

## Run 8: dedicated server (`campaign-server.miz` + `tools.smoke_server`, then `tools.flight_bubble_demo`)

Proof P1 in the [roadmap](roadmap.md): does everything work on a dedicated server with you as an ordinary remote client? The server is a separate Linux machine running DCS under Wine/Proton; the engine runs **on the server** too, so the bridge and Hooks still connect to `127.0.0.1` and nothing campaign-related is exposed to the network. About 45 minutes.

With one person, two players far apart are stood in for by **ghost players**: fixed points the engine treats as extra players (`--ghost BRG/NM` from Khalkhalah, `--ghost <airbase>`, or `--ghost far` for Blue Ferry's base in this demo). They show on the F10 map as yellow `GHOST n` text.

Time acceleration isn't available on a server, so this run doesn't wait for Red Ferry to leave the map.

### One-time setup

Paths on the **server** (set to match it):
```bash
SDCS="/home/dcs/.wine/drive_c/DCS_server"                                  # its DCS install
SSG="/home/dcs/.wine/drive_c/users/dcs/Saved Games/DCS.server1"           # its Saved Games: the folder with Config/serverSettings.lua
REPO="$HOME/dcs-dynamic-campaign"                                 # where the repo goes on the server
```

1. **Copy the repo to the server** (from your PC, in the repo root). Nothing is committed yet, so copy rather than clone; re-run after code changes:
   ```bash
   rsync -a --exclude .git --exclude .idea --exclude build ./ <server>:dcs-dynamic-campaign/
   ```
2. **On the server:** Python 3.11 or newer, and the tests pass:
   ```bash
   cd "$REPO" && python3 --version && python3 -m unittest -q
   ```
3. **On the server:** let missions load LuaSocket, as in the one-time setup above but on the server's install:
   ```bash
   cp "$SDCS/Scripts/MissionScripting.lua" "$SDCS/Scripts/MissionScripting.lua.orig"
   sed -i "s/^\(\s*\)_G\['require'\] = nil/\1--_G['require'] = nil/; s/^\(\s*\)_G\['package'\] = nil/\1--_G['package'] = nil/" \
     "$SDCS/Scripts/MissionScripting.lua"
   grep -n "_G\['require'\]\|_G\['package'\]" "$SDCS/Scripts/MissionScripting.lua"   # both lines start with --
   ```
4. **On the server:** install the Hooks script, then restart the DCS server (Hooks load at DCS start):
   ```bash
   mkdir -p "$SSG/Scripts/Hooks" && cp "$REPO/lua/hooks/campaign_hooks.lua" "$SSG/Scripts/Hooks/"
   grep -n "CAMPAIGN" "$SSG/Logs/dcs.log"   # after the restart: "campaign hooks loaded"
   ```
5. **Build the server mission on the server**, straight into its Missions folder. It needs `luajit` (`sudo apt install luajit`) and your template mission copied over once (from your PC: `scp "$SG/Missions/a2a.miz" "<server>:$SSG/Missions/"`).
   - The loader needs a path DCS can see. Many headless Wine setups have no `Z:` drive (so `/home/...` is invisible to DCS); link the repo into `drive_c` and use a `C:` path:
   ```bash
   ln -sfn "$REPO" ~/.wine/drive_c/dcs-dynamic-campaign
   cd "$REPO" && python3 -m tools.build_test_missions --server 'C:\dcs-dynamic-campaign' \
     --template "$SSG/Missions/a2a.miz" --out "$SSG/Missions"
   ```
   - The last line must show `['C:\\dcs-dynamic-campaign\\']`. If the server does have a `Z:` drive, `--server "$REPO"` also works.
   - `campaign-server.miz` is `campaign-smoke.miz` with the FA-18C as a **Client** slot and the bridge loaded from the server's copy of the repo.
   - After rebuilding, load the mission fresh from the mission list; a restart can keep running the old copy.
6. **Server settings** (web GUI or `serverSettings.lua`): add `campaign-server.miz` to the mission list. Pausing is fine (the war pauses with the mission), but note what it's set to: paused until a client joins, or running.

Run the engine on the server in `tmux` (or `screen`), so it survives your SSH session, and keep a log with `tee`.

### Part A: slot gating (`tools.smoke_server`)
On a dedicated server you're an ordinary client, so this is the first real test of `onPlayerTryChangeSlot`.

1. On the server: `cd "$REPO" && python3 -m tools.smoke_server 2>&1 | tee run8a.log`
2. Start (or restart) `campaign-server.miz` on the server. Expect `HELLO mission ...` in the terminal.
3. On your PC: MULTIPLAYER → connect to the server's address. Stay in **spectators**. Expect:
   - `HELLO hooks ...`
   - `SLOT TEST: blue is now BLOCKED`
4. Pick the blue FA-18C slot. Expect it to be **refused**, the chat message "That side is AI-only in this campaign.", a `hooks player ... slot_denied` line, then `SLOT TEST: denial worked; blue is ALLOWED again`.
5. Pick the slot again. Expect to get in, and a `hooks player ... player_slot` line with `unit_type`.
6. ✅ Pass: the slot was refused once, then allowed. Disconnect, and stop the smoke server with Ctrl+C.

### Part B: bubbles, ghosts, joining and leaving (`tools.flight_bubble_demo`)
Same scenario as Run 7 (Red Ferry, Blue Ferry, Blue CAS), plus two ghosts:
- **GHOST 1** at `000/35`, on Red Ferry's orbit, 35 nm north of Khalkhalah.
- **GHOST 2** at `far`, on Blue Ferry's base about 100 nm away.

**Setup**
1. On the server:
   ```bash
   cd "$REPO" && python3 -m tools.flight_bubble_demo --new --ghost 000/35 --ghost far 2>&1 | tee run8b.log
   ```
2. On your PC, in the repo, write the cartridge (it doesn't depend on the server, so there is nothing to copy):
   ```bash
   python3 -c "from pathlib import Path; from tools.flight_bubble_demo import write_cartridge; write_cartridge(Path('$SG/DTC'))"
   ```
   Waypoints as in Run 7 (WP2 = Red Ferry's orbit = GHOST 1).
3. Restart `campaign-server.miz` on the server. Expect `HELLO mission`, `GHOST 1 (000/35) placed`, `GHOST 2 (far) placed` and the `scenario: far base <name>` line.

**Steps**
1. **Join as a spectator; ghosts at work (T+0:00 to T+2:00).** Connect, stay in spectators, open the F10 map.
   - Both `GHOST` labels are on the map: one 35 nm north of Khalkhalah, one on the far base.
   - `Red Ferry: took off (not in DCS)`, then within about 30 s **`Red Ferry: spawned`**, although no real player is anywhere near: GHOST 1 is within 30 nm. Its marker turns `[DCS]`.
   - At T+1:00, **`Blue Ferry: spawned`** on the **ramp at the far base** (GHOST 2 is there), followed by `event ... takeoff` lines in the terminal within about 5 minutes. A ramp start at a base other than Khalkhalah hasn't been tried in DCS before; if it fails, send the error.
   - Once Blue Ferry is more than 40 nm from both ghosts and you (about 10 minutes after takeoff), expect `Blue Ferry: aggregated in the air`.
   - ✅ Pass: flights spawned around the ghosts while you were nowhere near; you see the markers from your client.
2. **Take the FA-18C slot.** Load `campaign_flights.dtc` if the briefing offers it (or from the jet's DTC page), then fly to WP1 as in Run 7.
   - Status (F10 > Other > Campaign) works and shows your bearing and range to Khalkhalah.
   - At T+5:00, **Blue CAS** ramp-starts at Khalkhalah (you're within 30 nm): you should see two Hornets on the ramp, taxiing and taking off. This checks that what the server spawns reaches your client.
   - Fly towards Red Ferry (WP2): the two MiGs should be visible on radar and visually at the marker.
3. **Leave and rejoin.** Go back to spectators for 2 minutes, then take the slot again.
   - While you're out, nothing near GHOST 1 leaves DCS. Things near only you may leave once they're beyond 40 nm of everyone; the Hornets on the ground at Khalkhalah stay.
   - On rejoining, Status works again.
4. **Client crash.** While flying, kill your DCS client (Alt+F4, or kill the process). The server and engine keep running; in the terminal, no errors. Reconnect and take the slot again.
5. **Engine restart.** On the server, Ctrl+C the demo, then start it again **without** `--new`, same ghosts:
   ```bash
   python3 -m tools.flight_bubble_demo --ghost 000/35 --ghost far 2>&1 | tee -a run8b.log
   ```
   Expect `HELLO mission ... new_run=False`, the flights still in DCS kept (no duplicates, no respawns), and the clock carrying on.
6. **Mission restart.** Restart the mission from the server (web GUI). Expect `new_run=True`: flights that were in DCS come back where the engine had them, the ghosts are drawn again, and the clock carries on from where it was.
   Before this step, update the repo on the server: the restart loads the new bridge, which redraws flight markers under fresh ids. Expect the `Red Ferry [DCS]` / `Blue Ferry [DCS]` labels on your F10 map this time; before the fix they never reached the client.

### What to send back
- `run8a.log` and `run8b.log` from the server: paste them, or `sudo cp` them to `/tmp` and copy them as your own user.
- Part A: was the slot refused, then allowed?
- Part B: for each step, pass or what happened instead. In particular: did Blue Ferry's ramp start at the far base work, and did you see Blue CAS on the ramp and Red Ferry on radar from your client?
- The server's `dcs.log` if anything failed (`$SSG/Logs/dcs.log`; the `[campaign]` and `CAMPAIGN` lines).

### Results (2026-11-18/19, FA-18C, Linux dedicated server, one remote client)
All six steps passed, after two fixes.
- **Ghosts:** Red Ferry spawned at GHOST 1 and Blue Ferry ramp-started at Tiyas (GHOST 2, 101 nm out) with no player near either. Ramp starts away from Khalkhalah work.
- **Seen from the client:** Blue CAS on the Khalkhalah ramp, the MiGs on radar, kills and losses on both sides counted. Players in a coalition slot see only their own side's units on F10; spectators see everything.
- **Leave and rejoin, client crash:** nothing near a ghost left DCS; Status worked again after rejoining; no errors on the server.
- **Engine restart:** `new_run=False`, flights in DCS kept, no duplicates, clock carried on.
- **Mission restart:** `new_run=True`, Blue Ferry came back in the air where the engine had it, ghosts redrawn, clock carried on. Done twice.
- The server pauses with no players connected, so the mission bridge only reconnects once someone is in (fits D1).

Fixed during the run:
1. **Flight labels never reached the client.** Shapes drawn once (the ghosts) showed; shapes redrawn by removing and re-adding the same id didn't. The bridge now draws each update under a fresh DCS id and removes the old one after. Verified: `Blue Ferry [DCS]` showed and moved on the client.
2. **A flight passing near its landing field counted as landed.** Blue CAS came within 5 nm of Khalkhalah in the air, so the engine marked its LAND waypoint passed; after the mission restart it was sent home instead of respawning. A LAND waypoint is now passed only by landing. Covered by a test; not yet seen in DCS (needs a `--new` run).

## Run 9: dynamic slots (`campaign-slots.miz` + `tools.slots_probe`), proof P2
Same server and paths as Run 8. The question: can DCS 2.9's dynamic slots (pick an aircraft at an airbase, from its warehouse stock) replace fixed slots, with the engine in control? The mission has dynamic slots at three blue bases, and the probe sets them up differently:

| Base | Hornets | What it tests |
|---|---|---|
| Khalkhalah | 4 | The normal case: pick, spawn, fly, land. Does the stock go down and back up? |
| Tiyas | 2, refused by the Hooks | Does DCS ask `onPlayerTryChangeSlot` about a dynamic slot, and can the engine refuse it? |
| Marj Ruhayyil | 0, then 2 from the F10 menu | Is an empty base hidden or blocked? Does a stock change during the mission reach the slot list? |

The probe also spawns **Join Me**, two Hornets orbiting 3 nm north of Khalkhalah at 15,000 ft, with #2 a Client unit added at runtime. Can it be joined in the air?

The fixed FA-18C slot from Run 8 is still there, to compare.

**Setup**
1. Copy the repo to the server as before (new: `tools/slots_probe.py`; changed: the Hooks, the mission builder).
2. **Reinstall the Hooks** (they now report every slot attempt as `slot_try`) and restart the DCS server:
   ```bash
   cp "$REPO/lua/hooks/campaign_hooks.lua" "$SSG/Scripts/Hooks/"
   ```
3. Rebuild the missions; this now also writes `campaign-slots.miz`:
   ```bash
   cd "$REPO" && python3 -m tools.build_test_missions --server 'C:\dcs-dynamic-campaign' \
     --template "$SSG/Missions/a2a.miz" --out "$SSG/Missions"
   ```
   Expect `campaign-slots.miz: patched: ... dynamic slots at 18, 39, 23`. Add `campaign-slots.miz` to the server's mission list.
4. Start the probe, then load `campaign-slots.miz` fresh:
   ```bash
   cd "$REPO" && python3 -m tools.slots_probe 2>&1 | tee run9.log
   ```
   Expect `HELLO hooks`, `RULES slots at Tiyas ... refused`, then once someone is connected (the server may be paused until then) `HELLO mission`, `SPAWN Join Me ...` and `STOCK Khalkhalah: 4, Tiyas: 2, Marj Ruhayyil: 0`.

**Steps**
Note what the slot screen shows at each step; screenshots help.
1. **The slot list.** Connect and stay in spectators. Look for the dynamic slots tab (the slot screen's tabs, or the map view).
   - Which bases are listed, and with what Hornet counts? Is Marj Ruhayyil listed with 0, or missing?
   - Is **Join Me** listed among the normal slots? (Probably not: DCS may not allow adding slots at runtime.)
2. **Khalkhalah.** Pick a Hornet there (cold start is fine; try "Spawn Hot" too, later). Expect `HOOKS slot_try` and `HOOKS player_slot` lines with `entry`, and `EVENT birth` with `player` and `place`.
   - Where did you spawn: on the ramp or in a shelter?
   - Expect `STOCK Khalkhalah: 3`.
   - Can you choose the loadout, fuel and livery in the dialog? Are loadouts limited (munitions are unlimited in this mission)?
3. **Take off, fly a circuit, land at Khalkhalah, shut down.** Expect `takeoff`, `land` and `engine_shutdown` events. Does the stock go back to 4 on landing, on shutdown, or only when you leave the jet? Then go back to spectators and note what happens to the jet and the stock.
4. **Tiyas.** Try to pick a Hornet there. Expect a refusal with the chat message "P2 TEST: slots at Tiyas are refused by the campaign." and `HOOKS slot_denied`. If you get in anyway, that's a result too: send the `slot_try` line, which tells us how to recognise a dynamic slot.
5. **Marj Ruhayyil.** In a jet at Khalkhalah, pick F10 > Other > Campaign > **Marj Ruhayyil: add 2 Hornets**. Go back to the slot list: does Marj Ruhayyil now offer 2 Hornets? Pick one if it does.
6. **Khalkhalah empty.** F10 > Campaign > **Khalkhalah: set Hornets to 0**. Does Khalkhalah disappear from the list or show 0? What happens to someone already in a jet there?
7. **Join Me.** If it was listed in step 1, take it. Do you start in the air in #2's seat, next to the AI lead?

### What to send back
- `run9.log`.
- For each step, what the slot screen showed and what happened; screenshots of the dynamic slot list help.
- The server's `dcs.log` lines with `CAMPAIGN` or `[campaign]` if anything failed.

### Results (2026-11-19, FA-18C, Linux dedicated server)
- **Slot list:** dynamic slots have their own tab listing every blue base with dynamic spawn, including one with no stock (Marj Ruhayyil). An empty base offers no aircraft.
- **Stock controls what can be picked, live:** after the F10 menu gave Marj Ruhayyil 2 Hornets mid-mission, the slot list offered them and the spawn worked.
- **Spawning** at Khalkhalah: loadout, fuel and livery are chosen in the dialog; the jet spawned in a shelter; stock went 4 → 3.
- **Leaving the jet** (to spectators, on the ground) removes it and puts the stock back (3 → 4) at once.
- **What the engine sees:** `birth` with the player's name, the type, `place` (the base) and a group named `<base>_<type display name>_<n>` (e.g. `Khalkhalah_F/A-18C Lot 20_3`, unit `..._3-1`). The Hooks get `player_slot` with a runtime unit id as `slot` (e.g. `1000004`) that isn't in `getAvailableSlots`.
- **Refusing is not possible up front:** DCS does **not** call `onPlayerTryChangeSlot` for dynamic slots (no `slot_try`), so the Tiyas refusal never ran. Control has to come from the stock (0 = nothing to pick), or after the fact (e.g. `net.force_player_slot` back to spectators).
- **No Client slots at runtime:** `coalition.addGroup` rejects a group with a `Client` unit (Join Me), so joining a flight in the air needs slots written into the .miz.
- Not checked yet: when stock comes back after a flight (landing, shutdown, or leaving the jet), and what happens to a player in a jet when their base's stock is set to 0.

## Run 10: ground movement at scale (`campaign-smoke.miz` + `tools.convoy_probe`), proof P3
Single player on your PC. Questions: can the engine get road routes from DCS, do convoys drive them, can a convoy be taken out of DCS mid-route and put back without losing its way, and does it stay smooth with many convoys moving?

The probe picks road start and end points 8–20 nm apart within 30 nm of Khalkhalah, asks DCS for the road route (`land.findPathOnRoads`), and spawns red convoys (BTR-80, BMP-2, T-72B and Ural trucks) in a column on the road, ordered to drive to the end "On Road" at 50 km/h. Every 4 minutes half of them are despawned and respawned 5 s later where they were. Convoys that arrive turn around. A `SUMMARY` line prints every 30 s.

**Steps**
1. **Default load (24 convoys of 8 = 192 vehicles).** `python3 -m tools.convoy_probe 2>&1 | tee run10a.log`, then start `campaign-dev/campaign-smoke.miz` and take the FA-18C (put it on autopilot or pause-free orbit; the jet doesn't matter).
   - Expect 24 `ROUTE` lines with the time DCS took to answer, then `ROUTES ... median N ms`.
   - Look at a few convoys: F10 map, then F7/F10 views on the trucks. Are they in a column on the road, driving towards their `Cnn end` label? Do any stop, turn back, or leave the road?
   - Note your frame rate (Ctrl+Pause shows it) before the convoys spawn and once they're all moving.
   - Let it run 15 game minutes (the `SUMMARY` lines count them): 3 respawn cycles. Keep DCS focused: single player pauses when it loses focus. Each `RESPAWN` should be followed by `RESUMED ... moving again N s after respawn`.
2. **Heavier load.** Stop the probe (Ctrl+C), restart the mission, and run `python3 -m tools.convoy_probe --groups 60 --vehicles 10 2>&1 | tee run10b.log` (600 vehicles). Frame rate and `sim speed` again, for 10 minutes.

**What the summary means**
- `off route`: how far each lead vehicle is from the route DCS gave the engine. Tens of metres = same roads. Kilometres = DCS chose a different way, so the engine can't predict where a convoy is from `findPathOnRoads`.
- `sim speed`: model time against wall time. Below about 0.95x, DCS is struggling.
- `bridge`: how long a simple query takes; large numbers mean the mission scripting is busy.
- `avg km/h` in `ARRIVED` lines: the real speed of a convoy on a 50 km/h order, for the engine's movement model.

### What to send back
- `run10a.log` and `run10b.log`.
- Frame rates: before, 24 convoys, 60 convoys.
- What you saw: columns on roads, anything stuck, turning around, or driving cross-country; and whether respawned convoys looked the same as before.

### Results (2026-11-18, single player, FA-18C)
- **Road routes:** `land.findPathOnRoads` returns a detailed polyline (150–1,700 points; 50–350 corners after thinning to 5 m), 10–200% longer than the straight line.
- **Convoys drive that route:** lead vehicles stayed within 15 m of it the whole time, and arrived 1–3 m from the end. The engine can predict where a convoy is from the saved route.
- **Speed:** 41–46 km/h end to end on a 50 km/h order (8-vehicle convoys).
- **Respawn mid-route:** moving again 1–2 s after being put back, in a column on the road.
- **Route queries stall the sim:** about 100 ms each (mostly the bridge's poll interval), some 0.7–1.0 s, felt as stutter. Routes must be computed outside play.
- **Scale:** 24 convoys of 8 (192 vehicles) moving: smooth, no frozen frames, sim speed 1.00x. 60 convoys of 10 (600 vehicles): a burst of 2–3 frozen frames (~0.4 s) every ~4 s, with the bridge stopped too, so it's DCS's cost for that many moving vehicles; frame rate ~240 → ~130. Ordering the route as point-to-point waypoints (`--orders points`) instead of "On Road" didn't remove it.
- 5 of the 60 convoys failed to spawn; cause not checked yet.

## Run 11: rebuild and reload the mission (`tools.restart_probe`), proof P4
On your PC, with your DCS hosting the mission (MULTIPLAYER → New server): `net.load_mission` only works on a server, but a hosted one counts. Question: can the engine write a new .miz with the campaign's date, time and weather, have the server load it, and carry on, with nobody restarting anything by hand?

Each rebuild moves the campaign clock on 6 hours from where the mission is, builds `campaign-next-a.miz` / `-b.miz` (alternating) next to your `a2a.miz` with the next weather in a cycle (overcast with rain → scattered cloud and fog → clear and calm), counts down 15 s, and has the Hooks load it. When the new mission connects, the probe asks DCS what it loaded and prints one `CHECK` line per item.

(On the dedicated server: `--template "$SSG/Missions/a2a.miz" --repo-path 'C:\dcs-dynamic-campaign'`, and `campaign-server.miz`.)

**Setup**
1. **Reinstall the Hooks** (new: `load_mission`, `mission_load` events), then restart DCS:
   ```bash
   cp lua/hooks/campaign_hooks.lua "$SG/Scripts/Hooks/"
   ```
2. Start the probe:
   ```bash
   python3 -m tools.restart_probe 2>&1 | tee run11.log
   ```
3. MULTIPLAYER → New server → host `campaign-dev/campaign-smoke.miz`. Expect `HELLO hooks`, `HELLO mission` and `MISSION Syria 2020-06-01 08:...` with the template's weather.

**Steps**
1. **First rebuild, from a slot.** Take the FA-18C. F10 > Other > Campaign > **Rebuild: +6 h, next weather**. Expect `BUILD ...`, `REQUEST load ...`, the countdown message in DCS, then `LOAD begin/end/start` lines, `HELLO mission ... new_run=True`, `MISSION ... 14:00`, 8 `CHECK` lines and `RELOAD 1: 8/8 checks ok; ... took N s`.
   - What happened to you: back at the slot screen, or the briefing? Did you have to do anything besides picking the slot?
   - Does it look like 14:00 with overcast and rain?
2. **Second rebuild, from spectators.** Stay in spectators and rebuild (take a slot to use the menu, then go back to spectators during the countdown). Expect 20:00, scattered cloud and fog. Is the server still running, not stuck on a loading screen or paused?
3. **Third rebuild, unattended.** Ctrl+C the probe and restart it with `--every-min 3`, then leave everything alone in spectators. Expect a rebuild every 3 game minutes; let it do 2 (02:00 clear and calm at night, then 08:00 overcast the next day). If the server pauses when nobody is in a slot, note it: scheduled restarts with nobody on are part of the question.

### What to send back
- `run11.log`.
- For each step: pass or what happened, how long the reload took, and what you saw as a client (kicked to the slot screen? disconnected? anything to click?).

### Results (2026-11-19, FA-18C, DCS hosting on your PC)
All three steps passed: rebuilds from a slot, from spectators, and unattended every 3 game minutes. Each new mission had the requested date, start time and weather (all `CHECK` lines ok), the engine carried on, and nobody had to do anything but pick a slot again. The only manual step was unpausing, because the server is set to start paused; unattended restarts need that setting off, or the Hooks to unpause (`Sim.setPause(false)`) after a load the engine asked for.

## Run 12: kneeboard pages (`tools.kneeboard_probe`), proof P5
On your PC, hosting the mission (MULTIPLAYER → New server), so you can leave the slot and take it again without restarting. Question: how can the engine get kneeboard pages to players?

- **Built into the mission:** `campaign-kneeboard.miz` carries a page for every aircraft (`KNEEBOARD/IMAGES`) and one for the FA-18C only (`KNEEBOARD/FA-18C_hornet/IMAGES`). Any rebuilt mission (P4) could carry the next session's pages this way.
- **In your own Kneeboard folder:** DCS scans `Saved Games\DCS\Kneeboard\<type>` when a cockpit is created (`Scripts/Aircrafts/_Common/Cockpit/KNEEBOARD/indicator/init.lua`). A player could save their flight's page there, e.g. downloaded from the war overview. Does it show without restarting the mission?

The probe writes "P5 PAGE A, version 1" into `Kneeboard/FA-18C_hornet` when it starts, and removes its pages when you stop it (Ctrl+C). Every page says what it is and when it was written.

**Steps**
1. `python3 -m tools.kneeboard_probe 2>&1 | tee run12.log`. Expect `MISSION ... 2 pages built in` and `FOLDER page A version 1`.
2. MULTIPLAYER → New server → host `campaign-dev/campaign-kneeboard.miz`. Take the FA-18C. Open the kneeboard (K; [ and ] turn pages; or the kneeboard's page list).
   - Which P5 pages are there: MISSION (every aircraft), MISSION F/A-18C, PAGE A version 1?
3. F10 > Other > Campaign > **Kneeboard: rewrite page A**, then **Kneeboard: add page B**. Look at the kneeboard again without leaving the jet: is page A still version 1, and is page B missing?
4. Go to spectators, then take the FA-18C again. Now: page A version 2 and page B?
5. Ctrl+C the probe; expect `CLEANUP removed 2 test page(s)`.

### What to send back
- Which pages you saw at steps 2, 3 and 4 (a screenshot of each P5 page helps).

### Results (2026-11-19, FA-18C, DCS hosting on your PC)
- **Pages built into the mission** show: the all-aircraft page (`KNEEBOARD/IMAGES`) and the FA-18C page (`KNEEBOARD/FA-18C_hornet/IMAGES`).
- **A page in the Kneeboard folder that's already on the kneeboard updates live** when its file is rewritten (page A, version 2, without leaving the jet).
- **A new page in the folder appears only on the next spawn** (page B needed leaving the slot and taking it again); no mission restart needed.

## Run 13: theater data (`tools.theater_probe`), proof P6
Single player. Question: can the map layer (airbases, towns, roads, bridges) be extracted from DCS well enough to build objectives and the links between them? It's a one-off extraction per map, not play, so stutters while it runs are fine.

The probe lists every airbase (runways, parking), converts the 1,182 places in `Mods/terrains/Syria/map/towns.lua` to map coordinates with DCS's own projection, ranks the places within 60 nm of Khalkhalah by size (scenery objects within 1 km), searches for bridges within 30 nm (scenery whose type name contains "bridge" or "most"), picks objectives (airdromes and the 40 largest places), links each to its 3 nearest, and asks DCS for each link's road route. It writes `build/theater/syria-probe.json` and a map image, `build/theater/syria-probe.png`.

**Steps**
1. `python3 -m tools.theater_probe 2>&1 | tee run13.log`
2. Start `campaign-dev/campaign-smoke.miz`, take the jet and keep DCS focused until `WROTE ...` (a few minutes).
3. Look at `build/theater/syria-probe.png`: are the objective towns sensible (real towns and bases rather than reservoirs and racetracks), do the links follow roads you'd expect, and are bridges where rivers and roads cross?

### What to send back
- `run13.log` (the image and JSON are in `build/`, I can read them).

### Results (2026-11-19): passed, with clean-up rules for the build
Everything came out of DCS in about 2 minutes:
- **Airbases:** 225 on the map, all reported as category 0 (airdrome), in 117 ms. 132 of them are helipads with names like `HS27`, `HL07`, `H_med_orig_01` (no runways, 1 to 3 parking spots). So helipads have to be told apart by having no runways, not by category. Khalkhalah: 2 runways (15/33, 2514 m), 59 parking spots.
- **Towns:** all 1,182 places converted in 578 ms; 118 within 60 nm. The size search took 11.8 s.
- **Bridges:** 165 within 30 nm in 7.5 s: 89 two-lane road, 49 four-lane road, 19 wooden, 8 rail. They sit where you'd expect: the Yarmouk valley in the south, and the wadis west of Khalkhalah.
- **Roads:** 199 of 210 links have a road route. That took 76 s in all (median 113 ms per route, longest 922 ms), so routes are computed once when the map layer is built and saved. Most links follow the road the straight line suggests; 30 are more than twice as long by road. The 11 without a route all touch helipads off the road network (in Lebanon, Israel, Jordan, and `HS27`).

Things the map layer build has to handle:
1. **Places aren't towns.** `towns.lua` lists landmarks next to towns: Umayyad Mosque, Ministry of Expatriates, Four Seasons Hotel, Damascus Tower and Central Bank of Syria all sit in central Damascus. Each counts as its own place, so Damascus filled 10 of the 40 objective spots. Places within about 2 km of each other need merging into one town, named after its largest member or a known town name.
2. **Size counts clutter.** The object counts include trash cans, market stalls and walls (`TRASHCAN_EU_SMALL` 11,892, `STREET_MARKET_2` 4,950). Counting only buildings (`*HOUSE*`, `*BUILDING*`) would rank towns better.
3. **Helipads are not airbases.** They're FARP or landing site candidates at best, and most links that went the long way round or found no road start at one.

## Run 14: briefing room, no mods (`tools.briefing_probe`), proof P19 part 1
Same server and paths as Runs 8 and 9 (`$SDCS`, `$SSG`, `$REPO`), with your own DCS joining as a client. The question: how much of "see the war in DCS, pick a flight, end up in its jet, maybe walk there" works with nothing installed on the player's PC?

The probe parks three Hornets at Khalkhalah, one per mock flight (Colt 1, Uzi 2, Enfield 3), labels them on F10, and takes chat commands. Chat starting with `-` goes to the server, and the other players don't see it.

**Setup**
1. Get the new code onto the server (changed: the Hooks, the mission bridge; new: `tools/briefing_probe.py`).
2. **Reinstall the Hooks** and restart the DCS server:
   ```bash
   cp "$REPO/lua/hooks/campaign_hooks.lua" "$SSG/Scripts/Hooks/"
   ```
3. Start the probe, then load `campaign-slots.miz` (from Run 9; the mission loads the bridge from the repo, so no rebuild needed):
   ```bash
   cd "$REPO" && python3 -m tools.briefing_probe 2>&1 | tee run14.log
   ```
   Expect `HELLO hooks`, then once you're connected `HELLO mission`, three `FLIGHT` lines and `SETUP done`.

**Steps** (screenshots help)
1. **What a spectator sees.** Connect, pick blue, and stay in spectators. Open the F10 map near Khalkhalah.
   - Do you see `P19: drawn for ALL`? `P19: drawn for BLUE only`? The red line north of the base? The three flight labels?
   - Can you see the parked Hornets (zoom in)?
   - Try once more after choosing no side at all, if the slot screen allows it.
2. **Chat from spectators.** Type `-help` and then `-flights`. Expect `CHAT` lines in the log and the replies in your chat. Did the other chat (if anyone is on) see your `-` lines?
3. **The server puts you in a jet.** Still in spectators:
   - `-join 1`: expect `JOIN ... create_client_aircraft returned`, then `HOOKS player_slot` and `EVENT birth`. Are you in a cold Hornet at Khalkhalah? Check the loadout (2 AIM-9, 2 AIM-7, 2 tanks) and fuel (about 60%).
   - `-spec` to go back to spectators (`net.force_player_slot`). Did it work?
   - `-join 2 hot`, then `-spec`.
   - `-join 3 air`: an air start (`isInAir`). Where do you appear, if anywhere?
   - If `-join` returns false or nothing happens, try `-join 1 cat=plane`, and send the log; it shows exactly what was sent.
4. **What the F10 map can carry.** Watch the `FRAME` lines (server frame times every 10 s) and your own frame rate (RCtrl+Pause) with the F10 map open, then closed:
   - `-draw 100`, wait for two `FRAME` lines; then `-draw 500`, `-draw 2000`.
   - `-churn 10` (redraw all every 10 s), then `-churn 2`. Do shapes flicker or lag on your client?
   - `-clear`.
5. **Walking.** `-join 1`, open the canopy, and press LCtrl+E three times to climb out.
   - Can you walk? Expect `EVENT ejection` and `WALKER` lines as you move. If there are none, type `-where` and `-near 100`.
   - Walk to Uzi 2's parked Hornet (the label is on F10). Within 15 m expect `REACH`, and the probe tries to put you in Uzi 2's jet.
   - Can you see other parked jets and the base around you while walking? Anything janky?

### What to send back
- `run14.log`, and what you saw at each step.

### Results (2026-11-19, runs 14 to 14d)
- **Hooks only load when DCS starts.** The first tries ran Hooks from 2026-11-18 and chat commands didn't work; a full server restart fixed it. Reloading a mission isn't enough.
- **F10 for spectators:** the drawings for everyone and the parked Hornets show to spectators. The blue-only label shows only once you've picked blue.
- **Chat commands work** from spectators: `onPlayerTrySendChat` gets every `-` line and the server answers with `chat_to`. Whether other players see the lines is unchecked (needs a second player). The server's own chat comes through the same hook; replies start with `> ` and the probe ignores player id 1, which stopped a feedback loop. Chat commands are now live only: an engine reconnect replayed old `-join` commands (Run 14d), so they're no longer queued.
- **`net.force_player_slot` into a mission slot works** for blue spectators (side 2), as often as you like. It doesn't work from neutral (side 0). `-spec` now keeps you blue.
- **The server can't put a player in a dynamic slot.** `DCS.create_client_aircraft` on the server creates and assigns the slot (with warehouse stock; the mission ships with none), but the client never enters, not even with Briefing → Fly. Even the opt-in client script, which asks DCS on the player's PC as the slot screen does, gets `spawnLocalPlayer` logged and nothing more.
- **Joining a mission Client slot works, with one command, using the client script** (`lua/client/campaign_client.lua`, Run 14d): `-slot 3` moved you into the `Ramp` Client Hornet parked cold at Khalkhalah, and `-enter` (the client script's `net.spawn_player()`) put you in the cockpit: `EVENT birth ... Ramp-1 place=Khalkhalah`. The same worked for the airborne Aerial-1 slot. Without the client script, the player clicks Fly after the move. The client script can't load DCS's slot-screen modules (Hooks run apart from the UI), so it doesn't try.
- **F10 load:** 2,600 shapes redrawn every 10 s cost the server nothing visible (11 to 12 ms frames). On the client: outside F10 it holds its 239 fps cap with a one-frame dip about every 30 s; with F10 open it drops to about 185 fps with dips. Fine when only changed shapes are redrawn, and not often.
- **Walking works for the player, but the server can't see where they are.** In the parked Hornet, LCtrl+E ×3 fires the ejection seat (`EVENT ejection`, then `landing_after_ejection` for a `pilot_f15_parachute`), and on the ground the player walks in first person wherever they like (videos: down the taxiway, into a shelter, up to a parked Hornet). The server's pilot object goes its own way: in Runs 14d and 14f it walked the same dead-straight line from the same start, at a steady 1.5 m/s, whatever the player did. So the walk is local to the player's PC, and the server can't tell when they reach a jet. When the probe's check did fire (14e, near the jet the player ejected from), moving the walking player to spectators, into slot 3 and `-enter` put them back in a cockpit.
- **Combined Arms works for getting to the jet** (Runs 14g and 14h). Combined Arms can't occupy infantry ("wrong vehicle type"; the manual lists infantry among the units you can't control), so the test used an armed Humvee (`Walker`, `playerCanDrive`) from a blue Tactical Commander slot (`artillery_commander_blue_2`). A unit a player drives is a mission unit, so the server follows it (positions every 2 s), and `getPlayerName()` on it returns the player while they're in it (`none` before and after). Driving it to Colt 1's parked Hornet triggered `REACH`; `force_player_slot` moved the player straight from the Combined Arms slot into the Ramp slot (`ok: True`, no spectator step), and the client script put them in the cockpit.
- **Of the infantry, only the Stinger soldier can be occupied** (Run 14i). Ten blue soldier types with `playerCanDrive` all spawned (M4, M249, M4 GRG, JTAC, Stinger, RPG, AK, AK ver3, Paratrooper AKS-74, WWII British); only `Soldier stinger` could be taken over (Combined Arms counts it as an air-defence unit, not infantry), the rest gave "wrong vehicle type". The server sees it like any mission unit: `getPlayerName()` names the player and its position follows them (it moved 10 m when they walked). But the player looks through the Stinger's sight the whole time, so it's no good for walking to a jet (F1 and F2 don't change the view).
- **The campaign window (dxgui) works** (Runs 14k/14l, client script version 2). The server sends it over RPC (`campaignWindow`: a title, lines and buttons), built from the stock UI pieces (DialogLoader, the in-mission Esc menu's skins). A button press reached the server (`client_click`), and the "Ramp" button put the player in the jet (force_slot, then the client script entered). A window hidden with `setVisible(false)` stops getting its hotkey, so the script shrinks it instead; you drag it by its title bar, which needs the skin's header (`headerHeight` 0 can't be dragged).
- **DCS's built-in browser doesn't work under Wine/Proton** (Runs 14k/14l). DCS ships CEF 106 (Chromium 106.0.5249.91) as `WebViewWidget`, the widget the Start Here page uses, which is grey on this PC too. The browser starts and fetches pages (the probe's HTTP server saw the request), but the renderer, the process that draws the page, hits a deliberate Chromium crash (`BREAKPOINT` in `libcef.dll`) as soon as it starts and exits within 0.25 s, so the window stays grey. DCS also stutters (about 170 ms hitches) while the widget is open. Tried without success: `cef_use_gpu = false` in `autoexec.cfg` (the renderer still crashes); `--disable-features=RendererCodeIntegrity`; `--single-process` (DCS crashes: the same breakpoint on its in-process renderer thread). DCS passes its own command-line switches on to Chromium (put them in the game's arguments), but forces `--log-severity=disable`. DCS loads every page through its own file system, so `data:` URLs fail (`Can't open file: /data:...`); a page has to come from a mounted folder or over HTTP. Not pursued: another Wine/Proton build (it would upgrade the DCS prefix, which every Lutris entry for DCS shares), and naming the failed check with CEF 106's symbols. Decision: the in-game UI is the dxgui window; the full war map is a normal web page from the engine, outside DCS.

- **Port 8088 belongs to the dedicated server** (Run 14j). Its web GUI posts to `127.0.0.1:8088/encryptedRequest`. With the probe's test web server on 8088, DCS got the probe's answer instead, shut down, and its watchdog restarted it every two minutes until the probe stopped. Anything the engine serves over HTTP must use another port.

**Design that follows:** the engine builds a Client slot into the mission for each player flight. A blue spectator is moved into it with `force_player_slot`; with the client script they're in the jet straight away, without it they click Fly. Dynamic slots aren't used for joining.

## Run 15: the campaign server (`campaign-smoke.miz` + `campaign.server`), backlog item 6

Single player on your PC, FA-18C, about 40 minutes. The first run of the [campaign server](server.md) in DCS: everything the bubble and flight demos did, in one process, on the campaign clock, with saving. The scenario is `khalkhalah`.

Distances are the real settings from `config/campaign.toml`, not the demos' short ones:
- **Formations** spawn within **20 nm** of you (60 s look-ahead, so about 26 nm when flying straight at them at 350 kt) and leave DCS beyond **25 nm**, after at least 90 s in DCS.
- **The SA-6** spawns within **21.9 nm** and leaves beyond **26.9 nm**.
- **AI flights** spawn within **60 nm** and leave DCS beyond **80 nm**, after at least 2 min in DCS. A ramp start spawns if its airbase is within 60 nm.

Nothing on the ground can reach you above 10,000 ft, except the SA-6, which can reach about 13 nm. Stay at **10,000 ft or higher**, and never go closer than **20 nm** to the SA-6.

### The scenario
| What | Where | Plan |
|---|---|---|
| **blue1** | 4 M-1 Abrams, 2 nm S of Khalkhalah | holds |
| **near1** | 2 BTR-80, 2 Ural (red), 3 nm N | holds |
| **armor1** | 4 T-72B, 4 BMP-2 (red), 13 nm N | holds; Blue CAS's target |
| **mech1** | 6 BTR-80, 2 Ural (red), 24 nm N | holds |
| **sa6** | SA-6 battery (red), 32 nm E | holds |
| **Red Ferry** | 2 MiG-29A, unarmed, weapons hold | T+0:00 air start 60 nm N at 20,000 ft. Orbits 35 nm N from about T+3:30 to T+18:30, then flies north and leaves the map at about T+26. |
| **Blue Ferry** | 2 F/A-18C, unarmed | Ramp start at the airbase closest to 100 nm away (Status names it in `Blue Hornets (<base>)`) at T+1:00. Flies to Khalkhalah and lands. |
| **Blue CAS** | 2 F/A-18C with Mavericks | Ramp start at Khalkhalah at T+5:00. Attacks armor1, then lands at Khalkhalah. |

**Where to look:**
- **Messages on screen:** the server announces every change, e.g. `Red Ferry: spawned` or `mech1: spawned, 8 vehicles`. They're also in the terminal as `FLIGHT` and `GROUND` lines.
- **The F10 map:** a label for every formation and flight. `[DCS]` means it's in DCS, and coloured red or blue; `[engine]` means it's data only. Grey means a formation not in DCS.
- **F10 > Other > Campaign > Status:** the campaign time `T+h:mm:ss`, and the magnetic bearing and range from you to every flight and formation, with altitudes, orbit time left, and each squadron's ready aircraft.

### Setup
1. In a terminal, from the repo (`~/PycharmProjects/dcs-dynamic-campaign`), write the waypoint cartridge into your DTC folder:
   ```bash
   python3 -m campaign.scenarios.khalkhalah "$HOME/Games/dcs-world/drive_c/users/tim/Saved Games/DCS/DTC"
   ```
   It writes `khalkhalah_test.dtc` with these waypoints:
   - **WP1:** 8 nm N of Khalkhalah. Watch Blue CAS from here; armor1 is 5 nm further north.
   - **WP2:** mech1, 24 nm N.
   - **WP3:** Red Ferry's orbit, 35 nm N.
   - **WP4:** Khalkhalah.
   - **WP5:** 12 nm E of Khalkhalah, which is 20 nm from the SA-6.
2. Start the server, with a fresh campaign. Leave the terminal open:
   ```bash
   python3 -m campaign.server --new --scenario khalkhalah
   ```
   Expect `new campaign: build/saves/campaign.json (scenario khalkhalah)`, then `listening on 127.0.0.1:7341; waiting for DCS`.
3. In DCS, open **Mission** and load `campaign-dev/campaign-smoke.miz` from your Missions folder (the same mission as Runs 5–7; `build/campaign-smoke.miz` copied there).
   - On the briefing screen, click **Open DTC**, import `khalkhalah_test.dtc`, and load it. Then press **FLY**.
   - In the jet, go to MENU (SUPT) → **MUMI**, check that **WYPT** is boxed, and let the transfer run. On the HSI you can then step through WP1–5.
4. You start about 4 nm south of Khalkhalah at about 10,000 ft. Turn on the autopilot or settle into a slow orbit right away.

### Steps
**1. Connect and start (T+0:00 to T+1:00).** Stay near the start point.
- Terminal: `HELLO mission run=... new_run=True map=Syria`, then `scenario khalkhalah set up: ...` and `saved build/saves/campaign.json`.
- On screen:
  - `Campaign server: scenario khalkhalah...` (already seen);
  - `blue1: spawned, 4 vehicles`, `near1: spawned, 4 vehicles` and `armor1: spawned, 8 vehicles`;
  - `Red Ferry: took off (not in DCS)`.
- Within about a minute: `Red Ferry: spawned`. It starts 64 nm from you and flies toward you, so it crosses 60 nm quickly.
- Status:
  - `Red Ferry: 2x FERRY enroute [DCS] 355M ~58 nm 20,000 ft`;
  - `Blue CAS: 2x CAS planned at T+5 min`;
  - `mech1: 8 veh [engine] 355M 28 nm`;
  - `sa6: 4 veh [engine] 077M 32 nm`.
- F10 map: blue1, near1 and armor1 labelled `[DCS]` in colour, with the vehicles on the map next to them. mech1 and sa6 are grey `[engine]`.
- ✅ Pass if: the formations within 20 nm are in DCS and the others aren't, and Red Ferry spawned in the air at its label.

**2. Pause (1 minute).** Note the `T+` time in Status, press **Pause** for one real minute, unpause, and check Status again.
- ✅ Pass if: the campaign time moved on by only the few seconds you were unpaused, not a minute.

**3. Blue CAS and Blue Ferry (T+1:00 to about T+12:00).** Fly to **WP1**: 355M, about 12 nm. Orbit there at 10,000 ft or higher.
- Just before you pass over the field: `mech1: spawned, 8 vehicles` (you're about 26 nm from it, heading at it).
- At T+1:00: `Blue Ferry: took off (not in DCS)`. Its `[engine]` label appears at its base, about 100 nm away, and moves toward Khalkhalah.
- When Blue Ferry comes within 60 nm of you, about 4–6 minutes later: `Blue Ferry: spawned`. Two clean Hornets appear in the air at its label. Check that they're at a sensible altitude, heading for Khalkhalah, and flying on normally.
- At T+5:00: `Blue CAS: spawned`. Two Hornets appear on the ramp at Khalkhalah (look on the F10 map, or F7 for AI aircraft views).
- Within about 5 minutes: `Blue CAS: took off from Khalkhalah`. Note the time.
- Blue CAS flies north past you and attacks armor1. Terminal: `EVENT unit_killed ...` lines. Status shows armor1's vehicle count going down.
- ✅ Pass if: Blue Ferry spawned in the air on its way in, and Blue CAS started on the ramp, took off, and attacked.

**4. Red Ferry, and formations leaving DCS (about T+12:00 to T+17:00).** Fly to **WP2** (mech1, 355M 16 nm), then on to **WP3** (Red Ferry's orbit, 355M 11 nm).
- Passing WP2: `blue1: aggregated, 4 vehicles`, once you're more than 25 nm from it and it has been in DCS for at least 90 s. Its label turns grey `[engine]`.
- Before WP3: `near1: aggregated, 4 vehicles`, at about 28 nm north of the field.
- Near WP3: radar (RWS, 40 nm scale) shows 2 contacts at about 20,000 ft. The MiGs are at their label.
- Status: `Red Ferry: 2x FERRY enroute [DCS] ... orbiting N min more`. Check it twice, a minute apart: N goes down by 1.
- ✅ Pass if: the formations behind you left DCS, and Red Ferry is orbiting where Status says.

**5. The SA-6, and Red Ferry leaving (about T+17:00 to T+27:00).** Fly to **WP5**: about 156M, 37 nm. **Stop at WP5 and orbit there.** Don't go further east.
- Along the way, armor1 and mech1 leave DCS: `armor1: aggregated, N vehicles` (with N being what Blue CAS left), and `mech1: aggregated, 8 vehicles`.
- About 11 nm before WP5, when you're about 26 nm from the SA-6 (21.9 nm plus the look-ahead): `sa6: spawned, 4 vehicles`. The RWR shows a `6` search radar off to the east (about 085M from WP5). At WP5 you're 20 nm away, outside its reach.
- After T+18:30, Red Ferry's orbit ends and it flies north. Once it's more than 80 nm from you: `Red Ferry: aggregated in the air`. The MiGs vanish, and the label stays as `[engine]` and keeps moving north.
- At about T+26: `Red Ferry: left the map (not in DCS): 2 aircraft back to the squadron`. Status shows `Red MiG-29s (off map): 8 ready`.
- ✅ Pass if: the SA-6 was up before you were within 22 nm, and Red Ferry finished its route in the engine.

**6. Landings (any time after about T+15:00).** Don't wait for these; check them when they happen.
- `Blue Ferry: landed at Khalkhalah`, then, after taxi and shutdown (up to 10 minutes), `Blue Ferry: recovered: 2 aircraft back to the squadron`.
- The same for Blue CAS, after its attack.
- Status: both Blue Hornets squadrons back to `8 ready`, or fewer if a Hornet was lost.
- ✅ Pass if: both flights landed and were recovered.

**7. Engine restart.** While flying, stop the server in the terminal with **Ctrl+C**. Expect `stopping` and `saved ...`. Within 20 s, start it again **without** `--new`:
```bash
python3 -m campaign.server
```
- Expect `loaded build/saves/campaign.json (scenario khalkhalah)`, then `HELLO mission ... new_run=False`.
- Nothing is duplicated: no second copy of the SA-6 or of any flight on the F10 map. Nothing vanishes.
- Status works, and the `T+` time carries on from where it was.
- ✅ Pass if: everything carried on as if the server hadn't stopped.

**8. Mission restart.** Restart the mission with **LShift+R**, or from the Esc menu. Reload the cartridge and do the MUMI transfer again if you want the waypoints.
- Terminal: `HELLO mission ... new_run=True`.
- On screen, again: `Campaign server: scenario khalkhalah...`.
- The formations near the start point spawn again: blue1 with 4 vehicles and near1 with 4. armor1 comes back with only what Blue CAS left of it. The `T+` time carries on and doesn't restart at 0.
- Flights that were airborne in DCS come back in the air where the engine had them. Flights that had finished stay finished.
- ✅ Pass if: losses and the clock survived the mission restart.

**9. Stop.** Exit the mission, wait 10 s, and then press Ctrl+C in the terminal.

### What to send back
- Tell me it's done. `build/logs/server.log` is on this PC, so I'll read it myself.
- For each step: pass, or what happened instead.
- For each air spawn (Red Ferry in step 1, Blue Ferry in step 3, any in step 8): did the jets appear at their label, at a sensible altitude and heading, and fly on normally?
- Anything that spawned, vanished or got stuck when it shouldn't have, with the `T+` time from Status.

### First try (2026-11-23): stopped at step 5, three bugs found
- **Worked:** set-up and messages; blue1, near1, armor1 and mech1 spawned and left DCS where expected; Red Ferry spawned in the air at T+0:03; Blue Ferry spawned in the air at T+3:12, landed, and was recovered.
- **Ctrl-C hung the server.** Since Python 3.12, `Server.wait_closed()` waits for every open connection, and DCS keeps its connections open. The server saved, stopped listening, and then waited forever. That also broke the mission restart: the restarted mission had nothing to connect to. Ctrl-C also killed `tee`, so the log ends at T+0:03:51. Fixed: connections are closed first; a second Ctrl-C quits at once; the server writes `build/logs/server.log` itself.
- **A target left DCS while an AI flight was attacking it.** The engine follows a flight in DCS by the waypoints it comes near (within 9 km). Blue CAS's egress point was 3 nm from its ingress point, so the engine counted the attack as done before the flight reached the target. Nothing kept a target in DCS for an AI attacker anyway; only players did. Fixed: an attack waypoint is passed only by reaching it; enemy formations in an attack zone spawn when the attacking flight in DCS comes within their spawn distance, and stay until it leaves the attack behind. Blue CAS now egresses 8 nm west.
- **Blue CAS's lead (Colt 2-1) went after the SA-6** (from your Tacview recording). You were orbiting across the SA-6's spawn and despawn distances, so it spawned and despawned five times, each time after the 90 s minimum. On its first spawn (T+6), Colt 2-1 left its route and flew to within 8 nm of the SA-6. The SA-6 left DCS at T+10:40, and Colt 2-1 turned back and fired its Mavericks at armor1 (T+14 to T+15). Colt 2-2 attacked armor1 all along. Nothing had tasked the lead against the SA-6: with ROE weapons free, the AI goes for targets of opportunity. Fixed, to check in DCS: flights other than fighters (CAS, SEAD, strike) now use ROE open fire, so they only engage what their tasks name; fighters stay weapons free, ferries weapons hold.

### Second try (2026-11-23): passed, through step 8
From `build/logs/server.log` and Tacview. The run ended at T+14, so Red Ferry leaving the map and the landings weren't seen this time; Blue Ferry's landing and recovery passed on the first try.
- **Ctrl-C:** three clean stops (`saving and closing`, `saved`), each within a second.
- **Engine restarts** (T+0:33 and T+12:21): `new_run=False`, mission and Hooks resumed their runs, and a kill that happened while the server was down (armor1's second BMP-2) was replayed and counted.
- **Mission restart** (T+13:51): `new_run=True`; every formation and flight was taken back into the engine and respawned: armor1 with its 2 surviving vehicles, Blue CAS with 1 aircraft, all three flights in the air where the engine had them. The clock carried on.
- **ROE open fire:** both Colt 2 jets flew straight to armor1; neither went for the SA-6, which was up from T+6:30 to T+10:51 while you were within 22 nm of it.
- **The target stayed in DCS:** armor1 stayed spawned while you were up to 30 nm from it (beyond its 25 nm) with Blue CAS attacking. Blue CAS killed 6 of its 8 vehicles.
- **Colt 2-2 flew into the ground** at T+10:22: a 55° dive from 9,500 ft, Maverick fired at 2,800 ft above the ground, no pull-out. Not shot down. A DCS AI attack-profile problem; logged in [air](air.md#not-done-yet).

## Run 16: build the Syria map layer (`tools.extract_theater`), backlog item 7
Single player, about 10 minutes. Builds `data/theaters/syria/map.json` from DCS: airbases, towns, military sites, bridges and road links within 100 nm of Khalkhalah ([theater](theater.md)). It's a one-off extraction, so stutters are fine.

1. In the repo: `python3 -m tools.extract_theater`. It prints `listening on 127.0.0.1:7341`.
2. In DCS, start `campaign-dev/campaign-smoke.miz` and take the jet. Keep DCS focused until the terminal prints `WROTE` and DCS shows "Map layer: done." (a few minutes).
3. Exit the mission.

### What to send back
- Tell me it's done. I'll read the terminal output you paste (or it's enough that `WROTE` appeared), `map.json` and the picture, `build/theater/syria-map.png`. Have a look at the picture yourself: are the towns real towns, and do the links look like the roads you'd drive?

### Results (2026-11-23): passed
`map.json` came out with 31 airbases, 60 towns, 26 military sites and 75 bridges within 100 nm of Khalkhalah, and 136 links, 6 of them without a road route (all touching Zarqa, Al Qusayr or Shayrat airbases: their nearest road point is probably on a road that doesn't join the network). The links follow the real road network; Damascus is one town; the landmarks are gone. Place names with stray symbols (`SAA 171th Brigade%`) are now cleaned up.

## Run 17: the whole map, with a scenery survey (`tools.extract_theater` again)
Run 16 only covered 100 nm around Khalkhalah and the 60 largest towns there. This run covers the whole map (Turkey to the Negev, Cyprus to H3), every town with at least 800 buildings, and airfields DCS reports no runways for (At Tanf, Kharab Ishk). Its scenery search also counts every scenery type and keeps the positions of anything that looks like infrastructure, for the next step. **Expect about 30 minutes**; progress lines show how far it is. If it's interrupted, run it again: what DCS already answered is kept.

1. `python3 -m tools.extract_theater`
2. Start `campaign-dev/campaign-smoke.miz`, take the jet, keep DCS focused and unpaused until `WROTE` (the bridge only answers while the mission runs).
3. Tell me it's done; I'll read `map.json`, `build/theater/syria-map.png` and `build/theater/syria-scenery.json`.

### Results (2026-11-24): passed
The whole map in about 15 minutes: 81 airbases (every one labelled on the F10 map, plus Ercan, Lakatamia, Nicosia and Kedem), 283 towns, 30 military sites, 345 bridges and 446 links (14 without a road route: the sea to Cyprus, Zarqa, Kahramanmaras). The survey found 144 infrastructure-looking scenery types; the rules now make 217 sites of them (refineries, power plants, substations, fuel depots, dams, cement works and industrial areas), and Natural Earth adds borders, rivers and lakes. An offline rebuild takes 2 s.

## Run 18: water (`tools.extract_theater` once more)
Same steps as Run 17. Everything else comes from the cache; this run only asks DCS for its surface type on a 1 km grid over the whole map (about 760,000 points in 22 queries, a short stutter each). It writes `data/theaters/syria/water.json`, and the picture then shows the sea and lakes as DCS has them.

### Results (2026-11-24): passed
873 × 873 cells of 1 km, 26 % water: the Mediterranean coast (matching the F10 map), Lake Assad, the Sea of Galilee, the Dead Sea, the Atatürk reservoir, and small reservoirs inland. `water.json` is 20 KB. The first picture was drawn before `water.json` was written; fixed.

## Run 19: Southern Syria on the campaign server (`campaign.server --scenario southern_syria`)
Single player, FA-18C, about 15 minutes. The first run of a scenario built from the map layer: do its formations spawn where the scenario puts them, and do the Syrian SAM types (SA-2, SA-3, SA-11, SA-8, EWR) spawn and work? The smoke mission starts you 4 nm south of Khalkhalah, which is a red airbase inside the IADS, so **turn on Options > Gameplay > Immortal** (single player only) for this run.

1. From the repo: `python3 -m campaign.server --new --scenario southern_syria --ghost 331/28`
   The ghost sits on Damascus (331°M, 28 nm from Khalkhalah), so the Damascus SAM belt spawns although you're nowhere near.
2. Start `campaign-dev/campaign-smoke.miz`. Expect the message `Campaign: Southern Syria. Coalition holds 120 objectives, Syria 140.` and the `GHOST 1` label over Damascus.
3. Within a minute the terminal lists `GROUND ... spawned` lines: around you (Khalkhalah's SA-8 and AAA, the SyADF 159th Regiment's SA-3 and SA-6 6 nm away), and around Damascus (SA-2s, SA-3s, the SA-11). The bubble caps DCS at 300 vehicles, so not everything near Damascus will fit; that's expected.
4. Open the F10 map: are the SAM sites spread out around their sites (not stacked, not in water), and do they look complete (launchers plus radars)?
5. Fly toward Damascus (about 331°M) at 20,000 ft for a few minutes: your RWR should light up with SA-2, SA-3, SA-6, SA-11 and SA-8 radars, and with Immortal on, missiles may come at you; that's the point.
6. F10 > Other > Campaign > Status: the squadrons, and the formations in DCS with bearing and range.
7. Exit the mission, then Ctrl-C the server.

### What to send back
Tell me when it's done; I'll read `build/logs/server.log`. Anything that failed to spawn, sat in a strange place, or didn't fire.

### Results (2026-11-24)
- The scenario loaded, the message showed, and the ghost was placed on Damascus. In 1.5 minutes 44 formations spawned, which hit the 300-vehicle cap: the Damascus SA-2s (2 of 3, the third didn't fit under the cap), SA-3s and SA-11, the 159th Regiment's SA-3 and SA-6, the Shahba SA-2, SA-8s, AAA, the Shebaa EWR and the SAA brigades. No spawn failures.
- Nothing spawned in water, **but formations in towns spawned inside buildings** (an SA-2 launcher and a P-19 radar among houses). The map layer knows where towns are, not where their buildings stand. Fixed: before its first spawn, a formation moves to the nearest clear ground (Run 20).

## Run 20: formations on clear ground
Single player, FA-18C, about 10 minutes. Before a formation first spawns, the engine asks DCS for the nearest spot with room for its layout: no buildings, statics or units in it, no water or runway under it, and not steep. It searches up to 3 km away. The formation stays at that spot from then on. The search runs in the mission, so how long it takes matters: watch for a stutter when the first formations spawn. Immortal on again.

1. From the repo: `python3 -m campaign.server --new --scenario southern_syria --ghost 331/28`
2. Start `campaign-dev/campaign-smoke.miz`. The terminal now shows `sited <formation> <n> m from its placement (clear ground)` before most town formations spawn. A `no clear ground within 3000 m` warning would mean a site that couldn't be fixed.
3. Did the first seconds stutter (the searches)? A short hitch is fine; a freeze of several seconds is not.
4. F10 map, then F11/external view on a few: `Damascus defence`, `Mezzeh air defence`, the Damascus SA-2s and SA-3s, `Khalkhalah air defence` (it was at the airbase's centre: now not on a runway). Are they all on open ground, not in or touching buildings?
5. Exit the mission, then Ctrl-C the server. Then do steps 1 and 2 again **without** `--new` (the campaign loads from the save): they should spawn at the same spots, with no `sited` lines.

### What to send back
Tell me when it's done, with a screenshot of anything still inside a building and whether there was a stutter. I'll read `build/logs/server.log`.

### Results (2026-11-24)
- Nothing in buildings any more, and the formations came back to the same spots after a restart.
- **But nothing stayed in the city proper.** The group needed a circle with room for its whole layout: 350 m across for a SAM or AAA site. A city has no such hole, so city defences moved 1–1.8 km out to the edge, and the SyADF 159th Regiment's SA-3 moved off its own revetments. Fixed: each vehicle now looks for clear ground by itself (Run 21).
- One vehicle stood in an orchard. DCS has no scripting query for trees, so siting can't see them. A vehicle under trees can still drive and shoot.

## Run 21: each vehicle on clear ground
Single player, FA-18C, about 10 minutes, Immortal on. Siting works per vehicle now. Each vehicle of a formation's layout moves to the nearest spot within 1 km that has nothing within 20 m: no building, static or unit, no water or runway under it, not steep, and no other vehicle's spot within 15 m. City defences should stay in town, in streets, squares and lots.

1. From the repo: `python3 -m campaign.server --new --scenario southern_syria --ghost 331/28`
2. Start `campaign-dev/campaign-smoke.miz`. The terminal shows `sited <formation>: <n> vehicles, moved up to <m> m` lines. A `found no clear ground` warning means some vehicles stayed where the layout put them.
3. Was there a stutter at the start? There are more searches now, though each is smaller.
4. F10 map, then F11/external view: `Damascus defence`, `Mezzeh air defence`, `Harasta defence`, the Damascus SA-2s and SA-3s. Are they in town now, with no vehicle inside or touching a building?
5. Exit, Ctrl-C the server, then steps 1 and 2 again **without** `--new`: same spots, no `sited` lines.

### What to send back
Tell me when it's done, with screenshots of anything inside a building or anything that looks wrong for a city. I'll read `build/logs/server.log`.

### Results (2026-11-24)
Passed. 44 formations were sited in their first 30 s with no stutter reported. Each formation's farthest-moved vehicle went a median of 50 m and at most 500 m, and every vehicle found clear ground. City defences stayed in town. One SA-13 overlapped a roadside fence: siting doesn't see fences and walls, just as it doesn't see trees (backlog: minor improvements).

## Run 22: the ground war in DCS (`campaign.server --scenario southern_syria`)
Single player, FA-18C, about 30 minutes, Immortal on. This is the ground war's first time in DCS. The coalition's 3rd BCT at King Hussein Air College attacks El Taebah, a Syrian border town, from the start. In DCS its battalions should:
- drive north on the road at about 25 km/h;
- stop about 1 nm short of El Taebah;
- fight the town's garrison, which exists only in the engine for now;
- take the town and drive in.

A ghost on El Taebah keeps it all in DCS. The war runs on DCS's clock, so time acceleration speeds it up.

1. From the repo: `python3 -m campaign.server --new --scenario southern_syria --ghost 201/35`
   The ghost is on El Taebah, 201°M 35 nm from Khalkhalah.
2. Start `campaign-dev/campaign-smoke.miz`. Within a minute the terminal logs `Coalition attacks town_el_taebah with ...` (the order) and `spawned` lines for the 3rd BCT's battalions (`blue_tank_bn_*`, `blue_mech_bn_*`).
3. F10 map: find the blue groups south of El Taebah, near the `GHOST 1` label. They should start moving north within a minute or two. Leave the F10 map open, then **Ctrl+Z** for time acceleration, to ×4 or ×8 (Ctrl+Shift+Z steps back down).
4. Watch until the terminal and a message say `Coalition attacks El Taebah` (the battalions arrived and stopped short), then, an hour or two of campaign time later, `Coalition wins at El Taebah` and `Coalition takes El Taebah`. Note whether the groups drove on the road, where they stopped, and whether they drove into town after the capture.
5. F10 > Other > Campaign > Status: the `Front:` and `Moving:` lines at the end.
6. Back to ×1, then fly over (201°M about 31 nm from where you start) and look at the column from the outside view: driving in a column on the road, not stuck, not off in the fields.
7. Exit, Ctrl-C the server. Run step 1 again without `--new`, start the mission again, and check on the F10 map that the battalions are where they were (or further on), not back at their start.

### What to send back
Tell me when it's done, with anything that looked wrong: groups not moving, stuck, off the road, or messages that made no sense. I'll read `build/logs/server.log`.

### Results (2026-11-24)
- **Passed:** the groups spawned and drove along the road. The first attack was the 1st BCT's armoured battalion from Ramtha, not the 3rd BCT.
- **Save bug:** every autosave failed (`can't save a dict key of type method`): `GroundWar.name()` hid the system's `name`. Fixed, with a test that saves the whole scenario.
- **No capture:** the attack started at T+1:27 and the run ended at T+1:37, before the first hourly fight. The town looked empty because garrisons weren't in DCS yet. Continued offline, the coalition takes El Taebah at T+4:00.
- **Found offline:** Syria sent 30 battalions to El Taebah, most on loops of 10–15 towns.
- **Movement in DCS** (backlog item 8): the artillery led; one 15-vehicle column per battalion; everyone on the same road; on the road to the end.
- **Changed after the run:** garrisons are formations that spawn in DCS (within 8 nm); defenders deploy on the edge of a threatened objective; attackers stop 3 km short; reserves are the nearest few, by road, with no long detours; hourly battle reports in messages and Status.

## Run 23: a defended town (`campaign.server --scenario southern_syria`)
Single player, FA-18C, about 40 minutes, Immortal on. Same attack as Run 22, now with El Taebah's garrison in DCS. Start a new campaign: saves from before this run have no garrison formations.

1. `python3 -m campaign.server --new --scenario southern_syria --ghost 201/35`. The ghost is on El Taebah.
2. Start `campaign-dev/campaign-smoke.miz`. Expect `spawned gar_town_el_taebah_1` and, in Status (F10 > Other > Campaign > Status), a `Garrisons: 121, N in DCS` line. There should be no `save failed` lines; there should be a `saved` line about every 30 seconds.
3. Fly to El Taebah (201°M, 35 nm). From the outside view (F2 on red units, or F10 with all units shown), check the garrison: 4 strongpoints of mixed infantry (AK, AKS-74, RPG) with a BTR-80 or ZU-23, round the edge of town about 750 m (0.4 nm) from the centre and facing out, not inside buildings. A save from before this change moves its garrisons to the edge on their next spawn.
4. Ctrl+Z to ×8. When the Coalition battalion comes near, Syria's formations at El Taebah, if any, should move to the town's southwest edge, facing it. The attackers should stop about 1.6 nm (3 km) short, then drive into town across country.
5. Each game hour you should get `El Taebah, hour N: Coalition attackers at X%, Syria defenders at Y%`, and Status should show the battle with both percentages. Note whether the battalion and the garrison actually shoot at each other in DCS, and who loses what.
6. Wait for `Coalition takes El Taebah` (about 3 game hours after the attack starts; ×8 is about 25 minutes). The Syrian garrison should disappear and a small Coalition one appear.
7. Exit, Ctrl-C, run step 1 without `--new`, start the mission: the town should still be the Coalition's, with its garrison.

### What to send back
Anything that looked wrong, especially: the garrison inside buildings or missing, attackers driving through the town without fighting, and reinforcements coming from far away. I'll read `build/logs/server.log`.

### Results (2026-11-24)
- **Passed:** El Taebah's garrison spawned and was sited (24, moved up to 50 m). The coalition battalion fought it in DCS and killed all 24 between T+1:28 and T+1:40, and the town was taken at T+2:00 with a coalition garrison in its place. The hourly battle reports came up as messages. The run saved cleanly, and after a restart without `--new` the town was still the coalition's, with its garrison.
- **Changed during the run:** the garrison spawned in the middle of town, because the edge layout applied only to battalions. Garrisons now stand in strongpoints round the town's edge (estimated from its size), and were on the edge after a restart. A town is now taken as soon as nothing defends it, not at the next hour. Garrison infantry is a mix (riflemen, RPG, machine guns); MANPADS were tried and taken out again (backlog item 8).
- **Not yet seen in DCS:** the mixed infantry, and battalions deploying on the edge of a threatened town (El Taebah had none).

## Run 24: planned AI flights in DCS (`campaign.server --scenario southern_syria`)
Single player, FA-18C, about 45 minutes, Immortal on. The planner and the air war are now in the campaign server: both sides plan flights from the war, and the AI flies them. Flights near a player (or a ghost) are in DCS; the rest fly in the engine. Two ghosts: one on Prince Hassan, the Hornets' base, to see ramp starts, and one on El Taebah, at the front, to see missions arrive.

1. `python3 -m campaign.server --new --scenario southern_syria --ghost "Prince Hassan" --ghost 201/35`
2. Start `campaign-dev/campaign-smoke.miz`. Expect `PLAN blue T+0h: ...` and `PLAN red ...` lines listing missions (CAP, CAS, BAI, SEAD, STRIKE). Open F10 > Other > Campaign > Status: the first lines list the next flights to take off (e.g. `CAS 4: 2x CAS FA-18C_hornet from Prince Hassan, takeoff T+0:20`), and the end has `Air:` and `Planned flights:` lines.
3. **Ramp starts.** On the F10 map, look at Prince Hassan (147°M, 63 nm from Khalkhalah) when a Hornet flight from there is due. It should appear on the ramp, taxi and take off within about 10 minutes. Expect `spawned fl:blue_..._N` and `takeoff` events in the terminal.
4. **Missions at the front.** Ctrl+Z to ×4. Watch flights arrive near El Taebah (201°M, 35 nm): CAS and interdiction flights attacking formations, SEAD flights going for SAM sites, CAPs orbiting. Note whether they attack what they were sent at, and whether red fighters come up to meet them.
5. **Landing.** Follow one flight home on the F10 map (or from the outside view): it should land at its own base and be recovered (`recovered: N aircraft back to the squadron`). A flight whose base DCS doesn't know flies there instead (a warning names it).
6. Fly yourself too: take off and fly towards El Taebah, so flights spawn around you as they come within 60 nm. Check they appear in sensible places, at sensible altitudes and headings, not on top of each other.
7. Exit, Ctrl-C. Restart without `--new` and the two `--ghost` options, start the mission: flights should carry on (planned ones still planned, airborne ones in the air where they were).

### What to send back
Anything that looked wrong: flights stuck on the ground, flying odd routes, attacking the wrong things, landing at the wrong base, or messages that made no sense. I'll read `build/logs/server.log`.

### Results (2026-11-25)
About 20 minutes (to T+0:54), from a new campaign.
- **Passed:** both sides planned from the start (blue: CAP over the front, CAS at El Taebah, then strikes on Sayqal, Tha'lah and Khalkhalah; red: CAPs) and planned again each hour. The Status page listed the next flights. Flights spawned on the ramp when due and took off within 2 to 11 minutes: CAP 2 (F-15C, Ramat David), CAP 19 (MiG-29A, Sayqal), SEAD 5 (Hornets, Prince Hassan) and CAS 4 (A-10C, King Hussein Air College). Saves had no errors. Nothing in the plan looked wrong.
- **Seen:** the first CAPs met over the front. Both MiG-29s were shot down, then both F-15Cs a minute later; the log doesn't say what killed them.
- **Not yet seen in DCS:** SEAD and CAS flights reaching their targets (the run ended before they got there), landing and recovery, and the restart (step 7).

## Run 25: cartridges in the mission, and AI wingmen (`tools.wingman_probe`), proof P20
Host from your DCS (MULTIPLAYER > New server), FA-18C, about 30 minutes. The jets start hot (engines running), as player flights will; `--cold` parks them cold instead. For the mission rebuilt each session ([planner](planner.md) section 5): each Client unit's cartridge is built into the .miz and should load itself, and seats players don't take are AI.

1. `python3 -m tools.wingman_probe`. It builds `campaign-dev/campaign-p20.miz`. Host it.
2. **Cartridge, mixed group.** Take **P20 Mixed** (Enfield 2-1; Enfield 2-2 is AI in your group). Open the DTC window (RShift+RCtrl+K): is cartridge `p20_mixed` listed and loaded? Count the waypoints on the HSI: **4** (east of the field) means the cartridge loaded itself on a hot start, **2** (north) means only the mission route. Does your AI #2 spawn beside you with its engines running, and answer F1 wingman commands? Take off and see whether it joins you.
3. **Cartridge, solo group.** Leave the slot, take **P20 Solo** (Enfield 3-1). The same check, expecting **6** waypoints (south) and cartridge `p20_solo`.
4. **Separate wingman, ramp.** Still on the ramp in P20 Solo: F10 > Other > Campaign > *Wingman on the ramp, Follow*. Where does it park? It spawns hot: does it take off before you, wait for you, or join you once you're airborne? Then *Remove wingman* and try *Wingman on the ramp, Escort*.
5. **Separate wingman, air.** Airborne, at about 15,000 ft: *Wingman in the air, Follow*. Does it hold formation through turns and climbs? Then *Wingman in the air, Escort*: the same checks. (Whether an escort engages enemy fighters is left for a campaign run.)
6. Ctrl-C the probe.

### What to send back
For each step: the waypoint count and whether the DTC window showed the cartridge; how each wingman behaved (positions, formation, takeoff timing, whether it fought). The probe prints births, takeoffs and losses of P20 aircraft.

### Results (2026-11-26, first try)
Hosted from your DCS, hot start.
- **Changed before it worked:** the Client slots were greyed out in the slot list. Khalkhalah had limited aircraft and no Hornets in stock; the probe now gives airdromes with Client groups unlimited aircraft (the real build must stock them). Board numbers were the same for both groups (101) and are now unique.
- **Passed:** a cartridge built into the .miz loads itself on a hot start: P20 Mixed showed `p20_mixed` loaded with 4 waypoints, P20 Solo `p20_solo` with 6. The AI #2 in a Client group spawned beside the player and answered wingman commands normally. A separate AI wingman spawned in the air beside the player with Follow stayed with them and kept formation.
- **Failed:** the ramp wingman spawned in an empty hardened shelter on the far side of the field, too small for a Hornet (part of the wing through the wall), and was destroyed at once, three times (`dcs.log`: engine startup, then "ai abort mission" 5 s later, then ejection). Players have hit the same with some slots. DCS's parking list doesn't give sizes, and the campaign server preferred shelters for fighters. Now spots are checked as the Mission Editor does: the Hooks `stands` command reads each stand's width, length and height, the mission `type_size` query the aircraft's, and only spots it fits are used (no shelters if the sizes can't be read). The probe prints what it chose. To fly again: step 4, after copying the new `campaign_hooks.lua` into Saved Games.
- **Second try (ramp wingman):** the Hooks read Khalkhalah's stands with their sizes (55 stands; the Hornet's box is 11.8 m span, 17.1 m long, 5.5 m high), and 32 of the 56 free spots fit it. The wingman spawned hot in an open spot 99 m from the player, and wasn't lost.
- **Ramp wingman, Follow:** both it and the player were in open spots, not shelters. It taxied to the runway, lined up, and held there waiting for the player instead of taking off.
- **It blocks the runway** while it holds (Khalkhalah's runway is narrow): the player, other players and AI flights at that base can't use it until the player passes.
- **Not reported yet:** whether it takes off after the player and joins up, and the Escort variants. Next: *Wingman parked, starts when you move*: parked cold and switched off, told to Follow, and started when the player starts to taxi (faster than about 4 kt). Does it start up, taxi and take off behind the player, and join?

## Run 26: a war map in a native DCS window (`tools.warmap_probe`), proof P21
Host from your DCS (MULTIPLAYER > New server), about 15 minutes. The opt-in client script (version 3) opens a window with DCS's own map widget, the one Instant Action's editor and the in-game DTC manager use, and draws a made-up war around Khalkhalah on it. It now also works for the host of a server hosted from DCS: the Hooks call it directly, since DCS can't send RPC to itself.

1. Install the new Hooks and client scripts, then restart DCS (both load at DCS start):
   ```bash
   cp lua/hooks/campaign_hooks.lua lua/client/campaign_client.lua "$SG/Scripts/Hooks/"
   ```
2. `python3 -m tools.warmap_probe`. Host `campaign-dev/campaign-p20.miz` (any mission works). On load the probe should print `CLIENT player id 1 has the client script (version 3)`.
3. **Open it.** Take a slot (or stay a spectator) and type `-map` in chat. Does a window open with DCS's terrain map, centred east of Khalkhalah? On it, look for:
   - blue and red shaded ground either side of a front line;
   - three red tank symbols facing three blue ones along the front;
   - a battle label and marker, and the Khalkhalah airbase symbol;
   - an SA-6 with a dashed, shaded ring and an SA-10 with a solid red ring;
   - Enfield 1's dashed yellow route with a fighter symbol on it, and Colt 2's dotted blue route;
   - the side panel's text.
   Which are missing or look wrong?
4. **Use it.** Drag to pan (left or right button), wheel to zoom. Click a symbol: does its details text appear in the panel? Try the Map, Terrain and Satellite buttons, and *Back to the front*.
5. **Show and hide.** Close it, then Ctrl+Shift+F8 twice: does it come back and go again? Do this in the cockpit too. Does the cockpit still take the mouse and keys while the map is hidden, and while it's open?
6. **Refresh.** `-map move` a few times: does the front move west without your view jumping, and without a flicker? `-map live 2` moves Enfield 1 every 2 s; watch your frame rate with the map open and closed, then `-map off`.
7. If you fly VR: is the window usable there at all?
8. Ctrl-C the probe.

### What to send back
The probe's `MAP` lines: each step, then `shown in N ms: ...`, with counts by kind and any `FAILED` parts. Also its `CLICK` lines, and what you saw for steps 3 to 7. If the window never opened, send the `CAMPAIGN-CLIENT` lines from `dcs.log`. A screenshot of the map helps most.

### Results (2026-11-26, first try)
Hosted from your DCS (2.9.32, which had reset `MissionScripting.lua`; re-applied the one-time setup).
- **Changed before it worked:** the Hooks ignored chat from the server's own player, which on a server hosted from DCS is the host, so `-map` never reached the probe. They now go by the `> ` prefix of replies instead.
- **Passed:** the window opens in the cockpit with DCS's terrain map (built in 12 ms, redrawn in 2 ms), the unit, SAM and airbase symbols, and the drawing layer's lines, dashed and shaded ring and text box. The Map, Terrain and Satellite buttons respond, and the side panel shows.
- **Fixed:** clicks on symbols found nothing (the click radius was measured along the wrong axis; icons now have their own hit test). The editor's own line symbols (`RouteLine`, `ThreatRangeBorder`) didn't show, so routes and rings are all on the drawing layer. The shading either side of the front was too faint to see (now 20%). Coloured labels were hard to read (now white with a dark outline).
- **Passed (later):** pan and zoom work, and Ctrl+Shift+F8 shows and hides the map in the cockpit.
- **Not tried yet:** `-map move` and `-map live` (refresh without the view jumping, frame rate), VR, and the click and drawing fixes above.

## Run 27: the real war on the war map (`campaign.server --scenario southern_syria`), P21
Host from your DCS (MULTIPLAYER > New server), about 20 minutes. The campaign server now draws the war itself on the P21 map window: ground held, the front, battles, formations, airbases, SAM sites and flights, as blue knows them (see "The war map" in [server](server.md)). Client script version 14 (after the first try, below): the server sends the map as you load in, hidden, and refreshes it every 30 s.

1. Install the new client script (the Hooks are unchanged since Run 26), then restart DCS:
   ```bash
   cp lua/client/campaign_client.lua ~/Games/dcs-world/drive_c/users/tim/"Saved Games"/DCS/Scripts/Hooks/
   ```
2. `python3 -m campaign.server --new --scenario southern_syria --ghost 201/35`. Host `campaign-dev/campaign-p20.miz` (from Runs 25 and 26: it has Client slots and the mission bridge); take a slot or stay a spectator. The log should show `MAP player 1 has the client script (version 14)`, then `MAP sent to player 1 (refresh, N objects, N kB, built in N ms)` and a chat line saying the map is ready.
3. **Opens by itself, then the hotkey.** Choose blue (a slot, or blue's spectators) and close the slot screen: about a second later the map should open by itself (dcs.log: `map: now side 2:...`, then `map: opens in a moment (blue)`). Close it with Ctrl+Shift+F8 and open it again. Does it open on the front south of Damascus? Look for:
   - blue ground to the south and west, red to the north and east, and a front line between;
   - the airbases on both sides with their names;
   - only the long range SAMs (the SA-2s around Damascus) with their rings. Zoom in on the front (under 40 nm across): the rest should appear (SA-3s, SA-6s, the Hawks and Avengers, short range air defence in contact), and go again when you zoom out;
   - blue formations near the front.
   What's missing, wrong or cluttered?
4. **Click around.** Click a blue formation, an airbase on each side, a SAM (a rough strength), the front line, a red depot and a blue one. No routes should show until you select a flight. Press each button under **Upcoming flights**: the view should move to that flight's plan without zooming in (it zooms out only if the plan doesn't fit). The plan: its route in white with each waypoint's time (`WP1 00:42 +0:12`, `TGT ...`), a white target marker, its package's other flights as yellow dashed routes labelled with their time on target, and the enemy air defence along the way (also listed in the details). Click empty map: they go. **Join:** select a player flight; the button under the list should say `Join <callsign> (2 of 2 seats free)`. Press it: a chat line `> You're <callsign>-1: ...`, the server log `MAP player 1 (<your name>) joins <callsign>-1`, and the button turns to `Leave <callsign> (you're <callsign>-1)`; the flight's details list you in seat 1 and its list button says `1/2 seats`. Join another flight: you leave the first. Press Leave. Are the flights called by callsigns (`Uzi 4`, not `SEAD 5`; start with `--new`), and do their units in DCS have the same callsign (F10 map, F2 view: Uzi 4-1)? Is the selected flight marked `>` in the list and its package's other flights `+`? Do the buttons' labels fit, and does each one's `T-0:19:32` count down every second (on mission time: faster at ×4, stopped when paused)? The first map is sent as you load in, before the planner has planned: it may say `YOUR FLIGHTS: none in the next 3 h` until the first refresh (30 s). Can you tell the white route from the white front line? Is the text in the panel useful? Is anything there that blue shouldn't know?
5. **Watch it move.** Ctrl+Z to ×4 and leave the map open for a few minutes. Every 30 s (real time) it should redraw in place: your view shouldn't jump. Do battles appear (yellow marker and label), and enemy formations once they're in contact? Do the flights' symbols move? Watch your frame rate as it redraws.
6. **Hidden refresh.** Close it (Ctrl+Shift+F8), wait a minute, open it again: it should show the latest war, where you left the view.
7. **The panel.** Is the side panel readable: the time of day (as on your kneeboard/F10 clock, not the campaign's 00:00 start), the weather line (does it match the mission's weather?), the front, news? Does the scale bar show on the map's bottom-left corner (a white strip with `25 nm` or so beside it, on a dark backing; dcs.log `map: map step scale bar over the map`), and does it follow the zoom? Are lines or button labels cut off? The window should fill the screen.
8. `-map` in chat: it should open (centred again).
9. Ctrl-C the server.

**First try (version 4):** the map drew (2 territory areas, 121 icons, 19 rings; 26 kB, built in 2 ms cached, redrawn in about 12 ms) and clicks worked, but: Ctrl+Shift+F8 never fired (dcs.log has no press; both opens were `-map`), so the hotkeys sat on a campaign window shrunk to 0x0 that didn't get them; routes couldn't be clicked (findDrawObjects doesn't find polylines, and no flight was airborne to have a symbol); a click inside a threat ring showed the ring, not what was there; short range air defence cluttered; the window was 1800x1100 on a 3440x1440 screen and the Close button's label was cut off. Versions 5 to 14: the hotkeys on a 1x1 window of their own; routes and the front hit-tested by the script; rings without info; long range SAMs only by default (the rest zoomed in, and a selected flight's route threats), with a rough strength; the panel's flights as buttons that show a flight's plan, target and route threats; then (version 8, after a screenshot) a button no longer zooms in (the view's scale isn't metres across, so 30 nm zoomed in far too far: the zoom threshold and the fit are now measured on the map), no routes by default, the selected route white with waypoint times, its package in yellow; then (version 9) the flight buttons count down to take-off (`T-0:19:32`), and "none" fits on one line (the first map, sent before the planner ran, had no flights and the two-line text was clipped); then (version 10) real callsigns, and the list marks the selected flight and its package. A screenshot of STRIKE 9's plan went straight through the Damascus air defence: backlog item 45; then (version 11) the map opens by itself on taking a blue slot, the panel has the time of day and weather instead of the threat summary, a scale bar, and "Upcoming flights"; then (version 12) it didn't open by itself (the host choosing blue got only a coalition change, no slot change: now both are watched, with the pause), the hint texts went, and a flight's target marker is its own target (Colt 4, the SEAD of a CAS package, had the CAS's target, 21 km from its SA-6); then (version 13) the scale bar moved from the panel onto the map (it works); then (version 14) a join button that gives the player a seat in the selected flight (nothing more yet). Confirmed in DCS with version 14: the join button, the map opening by itself on going blue, the time of day and weather in the panel, and callsigns on the DCS units; both sides' depots; the window fills the screen (rebuilt if the resolution changes), with full-width buttons.

### What to send back
The server log's `MAP` lines (`build/logs/server.log`), the `CAMPAIGN-CLIENT` lines from `dcs.log` (each hotkey press is logged as `hotkey Ctrl+Shift+F8`), a screenshot of the map at the start and one after a few minutes at ×4, and anything that looked wrong, cluttered or too much (or too little) for blue to know.

## Run 28: into the jet: Commit and Fly (`campaign.server --scenario southern_syria`), backlog item 2
Host from your DCS (MULTIPLAYER > New server), FA-18C, about 40 minutes. Client script version 15 and new Hooks. Join a flight on the war map, press **Commit** as its lead: the server builds the session mission with your flight's jets (your seat a Client slot, the other an AI wingman in your group) and reloads it. Then **Fly** (from 20 min before take-off) puts you in your own jet on the ramp, hot, with the planned loadout and your cartridge. See "Into the jet" in [server](server.md). DCS doesn't allow time acceleration in multiplayer, so pick a flight due off soon.

1. Install the new client script and Hooks, then restart DCS (the mission bridge loads from the repo):
   ```bash
   cp lua/client/campaign_client.lua ~/Games/dcs-world/drive_c/users/tim/"Saved Games"/DCS/Scripts/Hooks/
   cp lua/hooks/campaign_hooks.lua ~/Games/dcs-world/drive_c/users/tim/"Saved Games"/DCS/Scripts/Hooks/
   ```
2. `python3 -m campaign.server --scenario southern_syria` (the Run 27 campaign carries on; `--new` is fine too). Host `campaign-dev/campaign-p20.miz` as in Run 27. The log should show `MAP player 1 has the client script (version 15)`.
3. **Join and Commit.** Choose blue spectators: the map opens. Under **Upcoming flights**, pick a Hornet flight due off in 20 to 30 minutes (its button says `T-0:2x:xx`) and press **Join**: you're `<callsign>-1`, and the button under it says `Commit <callsign> (restarts the mission)`. Press it. Expect:
   - chat `> Committing Uzi 4: 1 player, 1 AI ...`, then a DCS message to everyone, `Campaign: Uzi 4 BAI is committed. The mission restarts in 15 s ...`;
   - the log: `SESSION built .../campaign-dev/campaign-session-a.miz: Uzi 4 BAI`, `SESSION bai_... committed by player 1; loading campaign-dev\campaign-session-a.miz in 15 s`;
   - the reload: back at the slot screen. If the server starts paused, unpause (Run 11). The log: `HELLO mission ... new_run=True`, then `SESSION Uzi 4 has its jets: Uzi 4-1 slot N`.
4. **The slot list.** Before choosing anything, look at blue's slots: is there a group named for your flight (`Uzi 4 BAI`) at its base, with one seat, `Uzi 4-1`, and no other aircraft slots? Try to take `Uzi 4-1` there: it should be refused with `Uzi 4-1 opens at HH:MM, 20 min before take-off` (if take-off is already less than 20 min away, it lets you in: that's step 5 without the button, so go back to spectators).
5. **Fly.** Choose blue spectators; the map opens. Select your flight: the second button says `Fly Uzi 4-1 from HH:MM`, greyed out, until 20 min before take-off, then `Fly Uzi 4-1`. Press it. Expect the map to close and you in the cockpit (log: `SESSION player 1 (...) into Uzi 4-1`, `EVENT birth Uzi 4-1 ... player <you>`, `SESSION Uzi 4 is in DCS`). Check:
   - hot, engines running, on an open stand the Hornet fits (not a shelter), at the flight's base;
   - the loadout the plan gave it, full internal fuel, callsign Uzi 4-1 (and your board number);
   - your AI wingman, Uzi 4-2, beside you, answering F1 wingman commands;
   - the DTC window (RShift+RCtrl+K): cartridge `uzi_4_1` loaded, and on the HSI the flight's waypoints (the map's route for it), not just the mission route.
6. **Fly it.** Take off (`takeoff` event; the map shows your flight in the air in yellow). Either fly the mission, or fly a few minutes and land back at the base. Landed and shut down: `recovered: 2 aircraft back to the squadron`, and you stay in your jet. Then leave to spectators.
7. Exit, Ctrl-C the server.

### What to send back
Pass or what happened for each step, a screenshot of the slot list (step 4) and of the cockpit with the HSI (step 5), and anything odd: wrong stand, wrong loadout, no wingman, no cartridge, the reload stuck or needing anything besides choosing a slot. I'll read `build/logs/server.log` (the `SESSION` lines) and `dcs.log`.

### Results (2026-11-27)
Everything passed: Join, Commit (the session mission built and loaded, its group in the slot list, the seat refused before it opened), Fly into Uzi 4-1 hot with the planned loadout, cartridge and AI wingman, and flying it. One change asked: the war map opened by itself at the start of the real mission, over the cockpit (the slot change into the jet looked like taking a blue slot). Version 16: it opens by itself only for a blue spectator, never in a jet. Install it again:
```bash
cp lua/client/campaign_client.lua ~/Games/dcs-world/drive_c/users/tim/"Saved Games"/DCS/Scripts/Hooks/
```

## Run 29: ride to the jet (`tools.ride_probe`), proof P22
Host from your DCS (MULTIPLAYER > New server), about 20 minutes. Client script version 17. Instead of going straight into the cockpit, you ride from a "briefing room" spot at Khalkhalah to your jet in an AI Humvee: the server drives it, and your client script keeps your camera in its passenger seat each frame (`Export.LoSetCameraPosition`), then puts you in the jet. The questions: does DCS let the script move a spectator's camera, does the camera stay put or fight DCS's own, does it look right, and does the Humvee get there?

The script finds the Humvee in DCS's own world objects when your PC may read them (always for the host; a client needs the server's `allow_object_export`), and otherwise follows the positions the server sends 4 times a second, smoothed. Hosting tests the first way; a second player joining would test the second.

1. Install the new client script, then restart DCS (the mission bridge loads from the repo; the Hooks haven't changed since Run 28):
   ```bash
   cp lua/client/campaign_client.lua ~/Games/dcs-world/drive_c/users/tim/"Saved Games"/DCS/Scripts/Hooks/
   ```
2. `python3 -m tools.ride_probe 2>&1 | tee run29.log`. Host `campaign-dev/campaign-p20.miz` (any Syria mission with the bridge). The probe picks an open spot at Khalkhalah a Hornet fits, builds `campaign-dev/campaign-p22.miz` with one Client Hornet there (`P22 Ride`, hot), and loads it after 10 s (`BUILD` lines). If the server starts paused, unpause. Expect `MISSION P22 Ride parked at ...` and `CLIENT player id 1 has the client script (version 17)`.
3. **The ride, seat view.** Pick blue and stay in spectators (the war map doesn't open in this probe: no campaign server). Type `-ride`. Expect a Humvee to drive from another parking spot, 300 to 600 m off (1,000 to 2,000 ft), to just short of the jet at 20 km/h, about a minute, with your view in its passenger seat. Then the view cuts to the cockpit of `P22 Ride-1`.
   - Did your view move at all? If not, try pressing F2, F11 or F10 (map off) once the ride's started, and say which one made it work.
   - Is it smooth, or does it jitter or fight back when you move the mouse or press view keys? Is the height right (eye at the windscreen, not in the roof or the floor)? `-eye 0.3 0.45 1.75` moves it (forward, right, up, metres); try a few and say which looks best.
   - Did the Humvee drive straight there, or get stuck, swerve or hit anything?
4. **Chase view.** Back to spectators (blue), `-ride chase`: the camera 12 m behind and 5 m above the Humvee, looking ahead of it. Same questions. `-ride 40` drives faster.
5. If a ride misbehaves: `-stop` ends it, `-in` puts you straight in the jet, `-rebuild` builds the mission again with other spots.
6. Optional, a second player with client script 17: their ride follows the server's positions (`RIDE ... following from server`) unless the server allows object export. Is it as smooth as yours?
7. Ctrl-C the probe.

### What to send back
`run29.log`: the `RIDE` lines (where the script got the Humvee from, and `held N, moved by DCS M`: frames the camera stayed where the script put it, and frames something else moved it), `DRIVE`, `STUCK` and `ARRIVE` lines. And what you saw: did the view move, which keys you needed, the seat height, smoothness, and how the hand-off into the cockpit looked. A short video helps most. If nothing happened, the `CAMPAIGN-CLIENT` lines from `dcs.log`.

### Results (2026-11-27, first try, client version 17)
Hosted from your DCS. The probe built and loaded `campaign-p22.miz`, the Humvee spawned and drove towards the jet, and the client script found it in DCS's world objects (object export is allowed for the host).
- **The view following a spectated aircraft keeps the camera.** In it, `LoSetCameraPosition` did nothing: about 5,000 frames, none held, and the camera read back where DCS had it (on the aircraft, 370 km off).
- **F4 lets the script place the camera.** After F4 it held (about 600 frames), and the view rode in the Humvee.
- **F5 mid-ride crashed DCS** (`ACCESS_VIOLATION`, with Lua on the stack): taken to be the script's camera calls in that view.
- `-ride stop` and `-ride -stop` started new rides (the command was `-stop`); they stop the ride now.

**Changed for the second try (client version 18):** while waiting, the script places the camera only once a second until it holds, then every frame; the moment DCS moves it (the view changed) it lets go and makes no more camera calls that ride (`-ride` to retry). The probe tells you to press F4 or F11 when the ride starts, and says in chat when you're in the Humvee and when the ride lets go.

### Second try (client version 18)
1. Install the client script and restart DCS:
   ```bash
   cp lua/client/campaign_client.lua ~/Games/dcs-world/drive_c/users/tim/"Saved Games"/DCS/Scripts/Hooks/
   ```
2. `python3 -m tools.ride_probe 2>&1 | tee run29b.log`, host `campaign-dev/campaign-p22.miz` directly (it's built; `campaign-p20.miz` works too and rebuilds it). Expect `MISSION P22 Ride parked at ...` and `CLIENT ... (version 18)`.
3. Blue spectators, `-ride`. When chat says *Press F4*, press **F4**. Expect chat *You're in the Humvee* and your view in its passenger seat until it reaches the jet, then the cockpit. **Don't press F5.**
   - Smooth, or jittery? Does the mouse or a view key fight it?
   - Seat height: try `-eye 0.3 0.45 1.75` (forward, right, up, metres) and a few others.
   - The hand-off into the cockpit: clean, or a jump through something odd?
4. Back to blue spectators, `-ride`, and press **F11** this time. Same questions.
5. Once more with **F4**, then mid-ride press F2: expect chat *the ride let go* and your view left alone (no crash). `-stop` and `-in` if needed.
6. `-ride chase` with F4 or F11: the camera behind and above the Humvee.
7. Ctrl-C the probe.

Send back `run29b.log` (the `RIDE` lines say `waiting`, `holding` or `let go`, with frames held) and what you saw at each step; a short video helps most.

### Results (2026-11-27, second try, client version 18)
- **The ride works.** With F4 the camera held from the third try (2 s) to the jet: 20,659 frames held, 2 moved by DCS. The Humvee drove there in 98 s, the probe moved you into `P22 Ride-1` and the client script put you in the cockpit (video).
- **You couldn't look around:** the script set the view direction every frame, over the mouse.
- **The seat was too high:** the cab roof filled most of the view (eye 1.75 m above the ground).
- **`-ride` from inside the jet** (you were still in `P22 Ride-1`) drove the Humvee into your own jet: it stuck 5 m short, and `-in` failed (DCS doesn't move a player into the slot they're in, as in Run 14). The chase view looked wrong until you picked the Humvee yourself, since F4 from the jet looks at the jet.
- "You're in the Humvee" was repeated every 2 s (the probe matched the progress lines too).

**Changed for the third try (client version 19):** you can look around. While the camera holds, the script keeps the view direction DCS has (with your mouse or head tracking in it), turned with the Humvee, and sets only the position (`-ride fixed` for the old behaviour). The seat is lower (0.2 m forward, 0.45 m right, 1.4 m up). `-ride` refuses while you're in a jet, and the chat messages come once.

### Third try (client version 19)
1. Install the client script and restart DCS:
   ```bash
   cp lua/client/campaign_client.lua ~/Games/dcs-world/drive_c/users/tim/"Saved Games"/DCS/Scripts/Hooks/
   ```
2. `python3 -m tools.ride_probe 2>&1 | tee run29c.log`, host `campaign-dev/campaign-p22.miz`. Expect `CLIENT ... (version 19)`.
3. Blue spectators, `-ride`, then **F4** when asked. Once chat says *You're in the Humvee*, look around with the mouse (and head tracking, if you use it): left, right, behind, up. Does the view follow you, and does it stay with you as the Humvee turns? Does it ever jump back ahead, or let go (`let go` in chat)?
4. The seat height: better now? Try `-eye 0.2 0.45 1.3` or `1.5` if not.
5. Once in the jet, go back to blue spectators before the next ride. Then `-ride` and **F11**: the same questions.
6. `-ride chase`, F4: behind and above the Humvee. Can you look around there?
7. `-ride fixed`, F4: the old view, for comparison.
8. Ctrl-C the probe.

Send back `run29c.log` and what you saw; a short video helps.

### Results (2026-11-27, third try, client version 19)
- **Looking around was janky.** In the clip the view tilted, swung down to the ground and turned to odd angles inside the cab. Keeping the direction DCS read back fed DCS's own camera changes back into the next frame, and they piled up.
- **Every ride let go** of the camera: once on a real view change (499 m), twice after DCS's camera moved itself 2 to 3 m from where the script put it (the let-go limit was 2 m).
- No crash.

**Changed for the fourth try (client version 20):** `Scripts/Export.lua` documents a camera request (`LoCreateCameraRequest`, `LoForceCamera`) that puts DCS's own object camera ("CameraObject", RCtrl+F2) on an object, optionally at a position, though it says it's "not enabled yet for general public release". If this DCS has it, the ride asks for that camera on the Humvee: from outside for 3 s (like F4), then at the passenger seat, first as a position in the Humvee's own axes, then in world coordinates. DCS then follows the Humvee itself and you look around as usual; the script stops placing the camera. If neither seat position works, it stays outside on the Humvee. Without the camera request, the ride places the camera as in the second try (view fixed ahead) and presses F4 for you (the command's number from DCS's input tables). `F4` is "camera mounted on object" (`iCommandViewRear`); Ctrl+F4 is the chase view.

### Fourth try (client version 20)
1. Install the client script and restart DCS:
   ```bash
   cp lua/client/campaign_client.lua ~/Games/dcs-world/drive_c/users/tim/"Saved Games"/DCS/Scripts/Hooks/
   ```
2. `python3 -m tools.ride_probe 2>&1 | tee run29d.log`, host `campaign-dev/campaign-p22.miz`. Expect `CLIENT ... (version 20)`.
3. Blue spectators, `-ride`, and **don't press anything**. The first `RIDE` line says `camera request available` or `n/a`. Watch the chat:
   - *Your view goes to the Humvee*: DCS's camera. Does your view jump to the Humvee from outside? Can you look around (mouse, head tracking) and does it follow the Humvee? After 3 s, *You're in the Humvee's passenger seat*, or *DCS's camera stays outside*: where is your view, and can you still look around?
   - *No DCS camera for the script*: the second try's ride (fixed view). Did your view move without pressing F4?
4. At the jet: the hand-off into the cockpit as before?
5. `-ride place`: the second try's way, for comparison (press F4 yourself if your view doesn't move).
6. Ctrl-C the probe.

Send back `run29d.log` (the `RIDE ... native` lines give where the camera sat relative to the Humvee at each step) and what you saw; a clip helps.

### Results (2026-11-27, fourth try, client version 20)
- **DCS's camera request isn't usable as is:** `LoCreateCameraRequest` exists but returned nil in DCS 2.9.32, so every ride placed the camera itself.
- **The script pressed F4 itself** (`LoSetCommand(359)`, `iCommandViewRear` from DCS's input tables): the camera held within 1 to 2 s with no key pressed.
- **`-ride chase` worked well:** the camera held behind and above the Humvee the whole way (12,033 of 12,034 frames), then into the cockpit (clip).
- **The seat view let go after 15 to 25 s,** with the camera 3 to 4 m from where it was put: DCS nudging F4's camera, likely with the mouse.
- **The spectated object didn't change:** F4 is the camera mounted on what you were spectating, and the bottom-left label still named that aircraft. The script moves the camera, not what DCS thinks you're watching.

**Changed for the fifth try (client version 21):** chase is the default view. Holding, the ride lets go only if DCS moves the camera 25 m or more (a real view change). Since `LoCreateCameraRequest` gives nil, the script builds the request itself (the fields Export.lua documents) and tries `LoForceCamera` once; if the view doesn't come to the Humvee within 0.5 s, it places the camera as before.

**Wanted (2026-11-27):** the inside view, from the passenger seat; chase stays as a test option. So version 22, before the fifth try was run, makes the seat the default again and tries to make the Humvee the object DCS watches, which version 21 couldn't:

**Version 22:** after the camera request (if it fails), the script presses F7 (ground unit view, `iCommandViewGround`) until the camera is within 80 m of the Humvee (each press moves to the next ground unit; at most 8), then F4 (camera mounted on the watched object, now the Humvee). It puts the camera at the seat once and watches for 2 s: if DCS keeps it at the seat as the Humvee drives, DCS follows it and you look around as usual. If not, the script places it every frame, keeping DCS's view direction (which now belongs to the Humvee, not the old aircraft: Run 29's third try tumbled with the aircraft). If F7 never reaches the Humvee, it places the camera on F4 as in the fourth try. `-ride chase` leaves DCS's F4 camera on the Humvee alone.

### Fifth try (client version 22)
1. Install the client script and restart DCS:
   ```bash
   cp lua/client/campaign_client.lua ~/Games/dcs-world/drive_c/users/tim/"Saved Games"/DCS/Scripts/Hooks/
   ```
2. `python3 -m tools.ride_probe 2>&1 | tee run29e.log`, host `campaign-dev/campaign-p22.miz`. Expect `CLIENT ... (version 22)`.
3. Blue spectators, `-ride` (the seat), no keys. Chat: *Getting your view onto the Humvee*, then one of:
   - *You're in the Humvee's passenger seat: look around as you like*: DCS's own camera on the Humvee. Does the bottom-left label name the Humvee? Look around with the mouse (and head tracking): smooth, and does it stay in the seat to the jet?
   - *... (the script holds the camera there)*: placed every frame, with your view direction. Can you look around, and does it stay level (no tumbling like the third try)?
   - *Couldn't get the view onto the Humvee*: the fourth try's way. What did F7 show (the `RIDE ... F7 landed` lines say where)?
4. At the jet: into the cockpit as before?
5. `-ride chase`: DCS's F4 camera on the Humvee, from behind. Does the bottom-left label name the Humvee, and can you look around?
6. `-ride fixed`: the seat, view straight ahead, for comparison.
7. Ctrl-C the probe.

Send back `run29e.log` and what you saw; a clip helps.

### Results (2026-11-27, fifth try, client version 22)
- **The seat ride works with DCS's own camera.** The built camera request did nothing (the view stayed 360 km off), so the script pressed F7: the first press landed on the Humvee (10 m), F4 mounted the camera on it, and the camera placed once at the seat stayed there by itself as the Humvee drove (checked at 0.5 to 2 s: 0.2 m ahead, 1.4 m up, 0.4 to 0.5 m right). DCS followed the Humvee to the jet with no further placing, you could look around, and the hand-off into the cockpit worked. "Worked very well."
- **The seat view sat low and turned a little to the side** (clip): the camera was placed once with the Humvee's heading only, and DCS then keeps it fixed to the Humvee, so the Humvee's pitch and bank at that moment (it starts on uneven ground) stayed in the view.
- **`-ride chase` worked:** DCS's F4 camera on the Humvee, from behind.
- The chat said *Your view goes to the Humvee* when the camera request was only sent, not when it worked.

**Changed (client version 23):** the seat is placed with the Humvee's whole attitude (heading, pitch and bank from `LoGetObjectById`), logged as `placed at the seat, the Humvee at heading ..., pitch ..., bank ...`; the eye is a little higher (0.2 m forward, 0.45 m right, 1.55 m up); that chat line comes only when DCS's camera came. To try: as the fifth try, with version 23 (`run29f.log`): is the seat view level and straight ahead now, and the height right? `-eye` adjusts it.

**For the campaign version** (asked 2026-11-27, not now): the ride may need to drop several players at their jets in turn; the parked jets should be visible as the Humvee drives up (Client jets aren't in the world until someone takes the slot: statics, or AI jets parked there, standing in for them); the Humvee drives off after the last drop; it follows the base's roads and taxiways instead of driving straight across the ground; and it starts somewhere sensible (by the base's buildings, not on a random parking spot). See backlog item 2.

### Results (2026-11-27, sixth try, client version 23)
- **The seat view is level now and points ahead** ("pretty good placement"; clip).
- **The camera sits inside the passenger figure:** the Humvee model has a driver and a passenger, and looking down shows the passenger's arms and legs around the camera, slightly off from where its head is. The figures can't be removed from the model.

**Changed (client version 24):** `-eye` and the new `-nudge` (relative) move the seat camera during the ride, at once, so it can be tuned to the passenger's head while looking at it. `-car TYPE` picks the vehicle for the next ride and `-cars` lists some to try: the Humvee family (`Hummer`, `M1043 HMMWV Armament`, `M1045 HMMWV TOW`, one body), the airfield vehicles (`P20_drivable` utility truck, `TugHarlan_drivable` and `B600_drivable` tugs, `r11_volvo_drivable` fuel truck), and `Willys_MB`, `UAZ-469`, `Tigr_233036`, `VAZ Car`, `tacr2a` (Range Rover fire truck). Which have crew figures, and what their cabs look like, is for the try to find out.

### Seventh try (client version 24)
1. Install the client script and restart DCS (`cp` as before). `python3 -m tools.ride_probe 2>&1 | tee run29g.log`, host `campaign-dev/campaign-p22.miz`; expect `CLIENT ... (version 24)`.
2. **Tune the Humvee seat.** Blue spectators, `-ride`. Once in the seat, look down and to the right and `-nudge` until your view sits at the passenger's eyes (e.g. `-nudge -0.1 0 0` 10 cm back, `-nudge 0 0.05 0` 5 cm right, `-nudge 0 0 0.05` 5 cm up). Each reply gives the whole setting: note the best one.
3. **Other vehicles.** Back to blue spectators between rides. `-car P20_drivable`, `-ride`; then `-car TugHarlan_drivable`, `-car Willys_MB`, and any other from `-cars` you like. For each: does it drive to the jet, is there someone in the seat, and how does the cab look from the seat (`-nudge` to fit)?
4. Ctrl-C the probe.

Send back `run29g.log` (it has every `-eye`/`-nudge` setting), which vehicle you liked and its best seat setting; a clip helps.

## Run 30: the ride, for real (`tools.ride_probe`), proof P22 continued
Host from your DCS (MULTIPLAYER > New server), about 20 minutes, client script version 24 (from Run 29; no new client script). The ride as asked on 2026-11-27, in the probe first:
1. **Several jets:** a two-ship (`P22 Ride-1` and `-2`, side by side). The Humvee stops beside each jet in turn (the nearest next), and each rider gets out at their own; at a jet nobody rides to it stops for a moment all the same (in the campaign, an AI jet's pilot).
2. **Jets you can see:** a Client jet isn't in the world until its player takes the slot, so a static Hornet stands on each empty spot. It's removed just before the player is put in, and put back 3 s after they leave. Its heading is a guess (nose along the stand's way out to the taxiway); when a jet spawns, the log says how far off the guess was, and later stand-ins use the real heading.
3. **Driving off:** after the last jet, the Humvee drives back to where it started and is removed.
4. **Taxiways:** it drives along the base's taxiways, read from DCS's own taxiway file for the base (`Mods/terrains/Syria/AirfieldsTaxiways/Khalkhalah.rn5`, campaign/taxinet.py), keeping off the runway except to cross it, and stops 14 m out along each jet's stand lead-in, in front of its nose.
5. **The start:** beside the tower, or else the building nearest the jets, from the scenery around them (`SCENERY` lists what's there, `START` what it chose).

The Humvee waits 8 s at the start after the first `-ride` so others can board: front passenger seat, then the back seats. A second player isn't needed: `-ride` alone rides to jet 2, stopping at jet 1 on the way.

1. Client script version 24 (install it if `CLIENT` says 23, and restart DCS). `python3 -m tools.ride_probe 2>&1 | tee run30.log` and host `campaign-dev/campaign-p20.miz`: the probe builds `campaign-p22a.miz` (or `-p22b`, whichever isn't loaded) with the two-ship and loads it (`BUILD` lines). First try: hosting the old `campaign-p22.miz` made the probe rebuild that same file and DCS refused to reload it ("Attempt to restart server with an invalid mission": it won't reload the loaded file once it's rewritten), so the probe now alternates two names, as the campaign's session missions do. Expect `TAXI Khalkhalah: 156 ... segments`, two `JETS` lines, `SCENERY`, `START`, and two `STATIC` lines.
2. **Before riding**, as a blue spectator, look at the jets (F10 map, then F7/F11 near them): are both stand-in Hornets there, on their spots, facing sensibly? Where is the start (F10: near the tower or a building)?
3. **Ride:** `-ride` (to jet 2). Your view goes into the Humvee at the start. Then, after 8 s:
   - does it follow the taxiways (not across the sand), cross the runway at a crossing, and avoid hitting things?
   - does it stop in front of jet 1 for a few seconds (chat: *Stopping at jet 1 (not yours)*), then go on to jet 2?
   - at jet 2: into the cockpit; is the jet where the stand-in was, facing the same way (`STATIC jet 2 spawned heading ...` gives the difference)?
   - in the cockpit, watch the Humvee drive away (it goes back to the start and disappears).
4. **Back again:** leave the jet to blue spectators: does jet 2's stand-in come back after a few seconds? `-ride 1` for the other jet.
5. With a second player (optional): both `-ride` within 8 s, one each to jet 1 and jet 2: both in the Humvee (front and back seat), each dropped at their own.
6. `-eye`/`-nudge` still tune the front seat during a ride; send the best setting if you found one in Run 29.
7. Ctrl-C the probe.

Send back `run30.log` and what you saw; a clip helps. If it went somewhere silly, the `DRIVE ... legs` line and where it got stuck (`STUCK`) say which leg.

### Results (2026-11-27, first try)
- **Good progress:** the probe built and loaded `campaign-p22a.miz` (after the file-name fix), started beside a covered hangar (`HANGARCOVERED2`, 190 m from the jets; no tower within 1,500 m of these stands), drove the taxiways to the jets with the camera in the seat, stopped at jet 1, went on to jet 2 and put you in it. The stand-in's heading was 6 deg off the real jet's.
- **Too much fuss at each jet:** it drove 14 m into each stand to face the jet's nose, then looped back out. Wanted: drive along the row and stop beside each jet, turning towards it at most (sketch: in and down the row in front of the noses, then on).
- **It stopped in front of the last jet and stayed:** after you went into jet 2, DCS took more than 5 s to answer a position query while it spawned your jet; the probe took the timeout as the end of the ride (`DRIVE ride failed:` with an empty message) and never sent the drive-off route or removed it.

**Changed:** each stop is 15 m ahead of the jet's centre, beside its nose, side on; the Humvee comes straight off the taxiway to the first stop and straight down the row between stops close together (within 150 m). After the last stop it drives on to the junction most nearly ahead (if the way there turns back anyway, as at these two stands, which are at the end of the apron, it doesn't first drive on 15 m) and is removed 250 m later. Slow answers from DCS no longer end the ride (positions are skipped, route changes retried), and a ride that fails removes the Humvee.

### Second try
As the first (`python3 -m tools.ride_probe 2>&1 | tee run30b.log`, host `campaign-dev/campaign-p20.miz`, `-ride`). Watch: does it come in and stop beside jet 1's nose without driving into its stand, go straight down to jet 2, and, once you're in the cockpit, drive off and vanish? Send `run30b.log`; a clip or a sketch like the last one helps.

### Results (second try, run30b.log)
- Stopping beside the noses and going straight down the row to jet 2 worked; the hand-off into jet 2 worked.
- **Still the wrong way in and out.** These jets are on an open ramp past the end of the taxiway, not at the end of a stand spoke as I'd assumed. The route went down the taxiway past jet 1, then turned back up to it, and the drive-off went round the end of the taxiway and back up it. The log ends there, because the mission was stopped.

**Changed:** the Humvee leaves the taxiway and joins the row 35 m before the first stop, so it arrives driving along the row. After the last stop it carries straight on along the row. It takes a taxiway only if one lies ahead and the way there doesn't turn back (no more than 1.5 times the straight line). Otherwise it goes straight on, over the open ramp, for 250 m, and is removed.

### Third try
As before (`python3 -m tools.ride_probe 2>&1 | tee run30c.log`, host `campaign-dev/campaign-p20.miz`, `-ride`). Watch two things. Does it join the row north of jet 1 and drive down past both noses? After you're in jet 2, does it carry on south-east, past your nose, and vanish without hitting anything? Nothing checks the ramp beyond the jets for buildings yet.

### Results (third try)
- **Worked as wanted:** the Humvee joined the row before jet 1 and drove down past both noses, stopping beside each. After the hand-off it carried straight on and vanished.
- Run 30 is done: multi-stop, visible stand-ins, driving off, taxiways, and a start beside a building.

## Run 31: the ride in the campaign (`campaign.server --scenario southern_syria`), backlog item 2
Fly is now the ride from Run 30 (campaign/rides.py), for your whole flight. You need client script version 24, which you already have, and no new Hooks. See "The ride to the jet" in [server](server.md). Pick a Hornet flight due off in 20 to 30 minutes, as in Run 28.

1. `python3 -m campaign.server --scenario southern_syria 2>&1 | tee run31.log` (the campaign carries on). Host `campaign-dev/campaign-p20.miz`.
2. **Join and Commit** as in Run 28, steps 3 and 4. After the reload, the log has `SESSION Uzi 4 has its jets`, then `RIDE <base>: N taxiway segments`, then `RIDE Uzi 4: 2 jets at <base>, stand-ins up`.
3. **Before Fly:** as a blue spectator, look at your flight's spots (F10, then F7 or F11 near them). Are there two Hornets standing there, facing sensibly?
4. **The ride comes by itself.** Pick blue spectators after the reload. Don't press Fly.
   - If take-off is more than 20 min away, chat says `> Your ride to Uzi 4-1 comes at HH:MM` and the map stays.
   - From then on, within a few seconds, you're boarded with no click needed. The log shows `RIDE player ... boards for Uzi 4-1 by themselves`. The log also has `RIDE player <ucid>: ...` lines saying what it was waiting for.

   First try (2026-11-28): after the reload the map opened and nothing came. The ride started only from Fly, and nothing pressed it. Expect:
   - chat: `> To Uzi 4-1 by Humvee: front passenger seat beside <building>...`;
   - your view in the Humvee's seat, where you can look around;
   - `Off we go` after 15 s;
   - taxiways to the row, beside each nose, into your jet, then on past;
   - the log: `RIDE start beside ...`, `RIDE Ride Uzi 4 at ...: 3 legs`, `RIDE player 1 into Uzi 4-1`, `SESSION Uzi 4 is in DCS`, `RIDE Ride Uzi 4 done`.
5. **In the jet:** is your AI wingman, Uzi 4-2, there in place of its stand-in, with no double jet and no explosion? Then the usual checks: hot, loadout, cartridge.
6. Fly or not as you like. Once you've taken off, the remaining stand-ins go.
7. Ctrl-C the server.

Send back `run31.log` and what you saw. Possible trouble spots:
- the start, at a base other than Khalkhalah;
- the AI wingman spawning on top of its stand-in;
- a Fly pressed by someone in a jet. They go straight in, without the ride.

### Results (second try)
- **The ride came by itself and worked,** but the Humvee started inside a building. The start was 20 m from the centre of the tower (`AIRPORT_BUILDING1_TOWER`, Prince Hassan), which is part of the large terminal, so that point was inside it. Nothing knew how big a building was.

**Changed:** the mission bridge has a new `footprints` query: each object near the chosen building, with its bounding box turned as it stands. The start is now the nearest spot that's 4 m clear of everything, with a clear straight line onto the taxiways, preferring the side towards the taxiways. Without one, it starts on the taxiway in front of the building. The ride probe uses the same code. The log line is `RIDE start beside <building>: x .. y .., N m from its centre, clear of N objects (M with boxes)`.

### Third try
As the second. Restart the server and the mission; the bridge loads from the repo. Check two things:
- Where does the Humvee start? It should be next to the tower building, in the open.
- What does the `RIDE start` line say? `0 with boxes` means DCS gives no boxes for scenery, so it fell back to rough sizes by building type.

### Mapping the starts (asked 2026-11-28)
The tower is rarely the right place for the ride to start. Each base's start is now mapped by hand with F10 marks (campaign/ride_starts.py, saved in `data/ride_starts.json`). The guess for unmapped bases is now the building nearest the jets, with no tower preference. To map one:

1. Restart the server and the mission (the bridge loads from the repo).
2. On the F10 map, add a mark where the Humvee should wait (the briefing room's door, the squadron's building) and type `ride start` as its text. Chat says `Ride start saved for <base> (1 there now)`. A read-only `Ride start (<base>, saved)` mark appears there; delete your own mark if you like.
3. Big base with several ramps: add one per ramp. Each ride uses the one nearest its jets.
4. Wrong spot: add a `ride start delete` mark on top of it.
5. Then commit a flight there as before. The log says `RIDE start at the mapped start x .. y .., N m from the jets`, and the Humvee should be waiting right there.

Things to check:
- Do marks from a spectator reach the server? The log shows `RIDE starts at <base>: ...`.
- Does the confirmation appear in chat?

## Run 32: which airfield buildings are administrative? (`tools.airfield_buildings`)
The ride should start at an administrative building on the airfield's grounds (an office, operations or squadron building), not the tower. DCS's type names (`SYRIA_BLOCK_BUILDING_04`) don't say what a building is. So this run surveys the buildings and you identify some by eye, and from those I'll work out a rule. Anything at least 8 m long or wide, within 600 m of the taxiways, counts.

1. `python3 -m tools.airfield_buildings 2>&1 | tee run32.log` (default: Prince Hassan and Khalkhalah; `--airbase "<name>"` for others, repeatable). Host `campaign-dev/campaign-p20.miz`. Stop the campaign server first: it uses the same port.
2. The log lists each base's buildings as `B1`, `B2`... (nearest the parking first), with type, size and distances, then a count per type. Each label is on the F10 map in yellow. `build/airfield_buildings/<base>.json` has the same.
3. In DCS, look at them: F10 to find a label, then F11 (free camera) or F7 to fly over. For each base, note:
   - which numbers are **offices, operations, squadron or terminal-like buildings**, where pilots would plausibly be briefed. A best guess is fine.
   - a few that are clearly **something else** (hangar, aircraft shelter, warehouse, fuel, barracks, housing), so I can rule their types out;
   - which one you'd pick as **the** start for each ramp.
4. `-label` / `-unlabel` in chat redraws or hides the labels, and `-survey` surveys again. Ctrl-C when done.

Send back `run32.log` and your notes, for example "Prince Hassan: B4, B9 offices; B2 hangar; start at B9".

### Results (2026-11-28)
- **First try:** only fuel tanks were labelled. DCS gives almost no scenery a bounding box (only the tanks had one), so the 8 m size filter dropped every real building. Now buildings without a box are kept and clutter is dropped by type name, matching whole name parts (matching inside words also dropped `STREET_MARKET`, because of "tree").
- **Second try, Prince Hassan (411 buildings):**
  - Look like briefing buildings: B205 (`AIRFIELDS_BUILDING_13`) and B199 (`AIRFIELDS_BUILDING_2`).
  - Look administrative: B196, B204, B206, B228, B246 and B262, all `AIRFIELDS_BUILDING_19_2`, a block of six 220 to 360 m from the parking.
  - Apartments: B225 (`SYRIA_CITY_HOUSE_10`).
  - Every pick is in DCS's shared `AIRFIELDS_BUILDING_<n>` set. Next: sort the set's other numbers (questions to the user).
- **Numbering:** the labels were numbered nearest the parking first, which looked random on the map. Now they read like a page: rows 200 m deep, north to south, and west to east within each row. `STREET_MARKET` stalls are clutter. The saved surveys were renumbered the same way; `old_n` keeps the old numbers. `--saved` labels the saved survey without surveying again, so the numbers match the questions. In the new numbers, the picks so far are B194 (`_2`) and B195 (`_13`), and the block of six `_19_2` is B95, B161, B163, B164, B166 and B168.
- **Third look (screenshots):**
  - **Admin:** `AIRFIELDS_BUILDING_21` is the headquarters (Pri B303, Kha B44). `_7` is an office (Pri B23, Kha B4), `_6` an operations building with a radar mast (Pri B7), and `_27` the tower's annex (Pri B184).
  - **Not admin:** `CG_BUILDING07` is a red-roofed warehouse (Pri B5). `_24` is a shed (Pri B26). `_33` and `_23` are aircraft shelters (Pri B212, Kha B32). `INDUSTRIAL_UN` and `CONCORD_FACIL_02` are warehouses or garages (Pri B49, B112).
- **Sizes:** the terrain's model files have every scenery model's box (campaign/model_boxes.py; 1,405 for Syria, in `data/theaters/syria/model_boxes.json`). They match the screenshots: HQ 25 × 77 m, office 15 × 21 m, shed 7 × 22 m, shelter 59 × 66 m. The tower itself is only 13 × 13 m; Run 31's Humvee was inside its annex (`_27`, 29 × 34 m). The bridge's `footprints` now gives each object's axes, so a model's box is turned as it stands.

**Changed:** the ride starts beside an administrative building (`ADMIN_MODELS` in campaign/ride.py, best first, each with an allowance in metres against its distance from the jets). The spot is clear of every object's real footprint. On the survey data it picks Khalkhalah's office (Kha B4) for the Run 30 jets, and at Prince Hassan the briefing building or the north office, depending on the ramp.

### Run 33: the ride from the admin building
As Run 31 (campaign server, commit a flight at Prince Hassan or Khalkhalah, pick blue spectators). The log says `RIDE start beside AIRFIELDS_BUILDING_<n>: x .. y .., N m from its centre, clear of N objects (M with their size)`. Check three things:
- Is the Humvee beside that building, outside it, and not clipping anything?
- Is it a sensible building to walk out of?
- Is the drive to the jets reasonable?
