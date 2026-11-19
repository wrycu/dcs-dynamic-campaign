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
