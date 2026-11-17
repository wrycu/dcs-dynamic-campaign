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
