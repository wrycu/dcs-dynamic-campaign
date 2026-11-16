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

- Both missions come from your `a2a.miz`: Syria, an FA-18C at about 10,000 ft, about 4–5 nm south of Khalkhalah, and Khalkhalah set to blue with a limited warehouse.
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
