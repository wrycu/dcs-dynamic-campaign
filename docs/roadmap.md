# Roadmap

How the [backlog](backlog.md) gets done. Each item is either:

- **Prove:** there is a real unknown, usually what DCS allows, or a model we don't know will
  play well. Do a small, cheap test first (a spike), and change the design if it fails.
- **Build:** the approach is clear; it's engineering work.

Many items are both: a small proof, then a build. The order is by risk and by what's needed for
a playable campaign. Risky proofs that the playable campaign depends on come first, because a
failed proof can change everything built after it.

## What "playable" means

A group of players can, over several sessions on a dedicated server:

1. see the state of the war and the flights their side has planned;
2. pick a flight, get its route and details into the cockpit, and fly it with AI flights and
   ground forces around them;
3. see the war move: front line, losses, captures, all saved between sessions;
4. come back next session to a mission whose date, time and weather follow the campaign.

## Phase 0: decisions

| | Decision | Why now |
|---|---|---|
| D1 | **Decided: the war pauses between sessions.** The campaign clock only runs while a mission is running. | Item 17 (war without DCS) is a testing and balancing tool, not part of normal play. |
| D2 | **Decided: superpower vs minor country first.** | Fewer enemy aircraft, so the planner and air defence can start simpler. |
| D3 | **Decided: both.** A map layer (airbases, towns, roads and links, bridges) generated from DCS once per map, and a scenario layer (ownership, front line, order of battle) authored on top, with rules filling in routine placements. The map layer starts with what the first scenario needs and grows over time; it doesn't have to cover every important place up front. | Decides P6 and item 7. |

## Phase 1: proofs the playable campaign depends on

In order of risk. Each is a short spike with a pass/fail question.

| | Proof | Items | Question |
|---|---|---|---|
| P1 | Multiplayer on a dedicated server | all | Do the bridge, Hooks, bubbles, slot gating and saves work on a dedicated server with two or more clients, players far apart, joining and leaving? Everything so far was tested in single player. **Passed (Run 8, 2026-11-18/19)** on a Linux dedicated server with one remote client and ghost players standing in for others: bridge and Hooks, slot gating, bubbles around players far apart, joining, leaving, client crash, engine and mission restarts. Two clients for real is still worth a check when a second player is around. |
| P2 | Player slots | 2, 11 | DCS 2.9 has dynamic slots (players pick an aircraft at an airbase, limited by the warehouse; seen in `MultiplayerSelectDynamicDialog.lua`). Can they replace fixed slots? Can the engine control them per base, and match a player who spawns to the flight they picked? Can anyone join a flight that is already airborne? **Mostly answered (Run 9, 2026-11-19):** yes, dynamic slots can replace fixed ones. The engine controls them per base through warehouse stock, live; DCS doesn't ask the Hooks before a dynamic spawn, so stock is the control. A spawn is matched by the `birth` event (player, base, type). Joining in the air can't use slots added at runtime (DCS rejects Client units in `addGroup`); it would need air-start slots written into the .miz (P4). Still open: when stock returns after a flight. |
| P3 | Ground movement at scale | 8 | Can moving ground formations follow roads in DCS, despawn and respawn mid-route, and stay smooth with many groups moving? Is `land.findPathOnRoads` (or similar) usable from the engine? **Passed with limits (Run 10, 2026-11-18):** convoys follow DCS's road routes to within 15 m, despawn and respawn mid-route cleanly, and 192 moving vehicles are smooth. Two limits shape the ground war: route queries stall the sim (compute routes between objectives with the map layer, P6, and save them), and 600 moving vehicles hitch every few seconds, so only convoys near players are in DCS, under a cap somewhere between 192 and 600 vehicles. |
| P4 | Mission rebuild and restart | 12, 18 | Can the engine write a .miz with the campaign's date, time and weather, have the server load it (Hooks `net.load_mission`), and carry on with no one restarting by hand? **Passed (Run 11, 2026-11-19, hosted from DCS on your PC):** the engine builds a .miz with the campaign's date, time and weather, the Hooks load it, and the campaign carries on; players only pick a slot again. A server set to start paused needs that off for unattended restarts, or the Hooks unpause after the engine's own loads. |
| P5 | Kneeboard delivery | 39 | How do kneeboard pages reach players? Pages built into the .miz (for all aircraft, per type), and pages in each player's own `Saved Games/DCS/Kneeboard` folder: does DCS pick up new ones mid-mission, on the next spawn, or only at mission start? **Passed (Run 12, 2026-11-19):** pages built into the .miz show, for all aircraft and per type; folder pages show on the next spawn, and existing ones update live when rewritten. The server can only deliver through the .miz (at each rebuild, P4); live pages need something on the player's PC to rewrite them. |
| P6 | Theater data | 7 | Can airbases, towns, roads and bridges be extracted from DCS for Syria (`map/towns.lua`, airbases and road paths from a mission script, map objects, `MissionGenerator/nodes.lua`) well enough to build objectives and links? Includes computing and saving road routes between objectives (P3). **Passed (Run 13, 2026-11-19):** airbases (runways, parking), all 1,182 places, bridges (scenery search) and road routes between objectives (199 of 210 links) all come out of DCS in about 2 minutes, and are saved as JSON. The build must merge landmarks into towns (central Damascus is 10 "places"), rank by buildings only, and treat runway-less "airbases" (132 of 225) as helipads. `nodes.lua` and `roads/*.rn4` aren't needed. |
| P7 | Abstract combat | 8, air combat | Does a simple model of ground and air fights give believable results? Test by running the war offline (an early version of item 17). **Passed (offline, 2026-11-19):** hourly ground attrition and per-mission air fights (`campaign/combat.py`) match real-world references in direction and roughly in size, and the force balance works as a setting: a quick win, a contested war, or a stall. A 14-day war runs in under a second. What needs work is above the fights: a ground commander that keeps a front line and guards its rear, a red air commander that keeps its air force back, and replacements. See [combat](combat.md). |
| P19 | Briefing room in DCS | 41, 2, 4 | How much of "see the war in DCS, pick a flight, end up in its jet" works? (1) No mods: what spectators see on F10, chat commands from spectators, and whether the server can spawn a dynamic-slot aircraft for a player (`create_client_aircraft`) with the planned loadout, on the ground and in the air (`isInAir`). (2) How many F10 drawings, redrawn how often, before the server or clients slow down. (3) Opt-in client script: does a custom DCS window (dxgui) work under Linux, does the stock `RPC` channel carry campaign data, can the script spawn the player into a flight? (4) Walking to the jet: can a pilot who climbs out of a parked jet walk, can the server follow them, and can walking up to a flight's parked jet put them in it? Fallbacks are a unit the engine moves for them, or Combined Arms for owners. **Run 14 done for parts 1 to 3** ([testing in DCS](testing-in-dcs.md)): spectators see F10 drawings, chat commands work, the F10 load is fine, and the server moves blue spectators into a mission Client slot (`force_player_slot`); the opt-in client script then spawns them, one command in all. Dynamic slots can't be joined that way. The dxgui window is untested. Part 4: players can walk after a ground ejection (LCtrl+E ×3), but only their own PC knows where they are; the server's pilot object walks a fixed line. Walking to a jet needs the client script to report the player's position. With Combined Arms (Runs 14g/14h), driving a vehicle to a parked jet works: the server follows the vehicle and moves the player into the jet's slot. Of the infantry, only the Stinger soldier can be occupied (Run 14i), and it's stuck looking down its sight. Decided: three viable ways to the jet: Combined Arms driving, walking with the client script reporting the position, and a unit the engine moves; `-join` stays for everyone. |

## Phase 2: build the playable campaign

In order. Items in brackets are only the part needed now; the rest comes later.

1. **6** One campaign server, and the parts of **18** needed to run it (save on stop, status). **Done** ([server](server.md)); Run 15 passed in DCS (2026-11-23).
2. **5** Per-type cruise speed (small; do it while touching the flight model). **Done** (2026-11-23).
3. **7** Theater data for Syria: the generated map layer, cleaned up for the first scenario's area, plus that scenario's authored layer.
4. **8** Ground war: movement, abstract fights, captures, front line.
5. **1** [one scenario, D2]
6. **2** Planner (ATO) and choosing a flight.
7. **11** Players in the campaign: slots (P2), losses, pilot roster [basic].
8. **39** Kneeboard pages, and **16** [briefing only].
9. **9** [replacements only, so the war doesn't run dry].
10. **12** Date, time and weather between sessions, with scheduled restarts.
11. **3** War overview UI [read-only].
12. **19** Victory and defeat [one simple condition].
13. Discord notifications [session start and end, flights needing players].

**At this point the campaign is playable.**

## Phase 3: proofs for depth

In order of value for the playable campaign, then risk.

| | Proof | Items | Question |
|---|---|---|---|
| P8 | Air picture and intercepts | 23 | Can the engine model radar coverage for sites and AWACS that aren't in DCS, and have DCS AI intercept well when vectored? |
| P9 | SAM emission control | Also queued, 23 | Do alarm state and `setEmission` behave reliably on spawned sites? |
| P10 | Carriers | 13 | Can a carrier group with player slots move with the campaign? Slots on a ship must exist in the .miz, so this probably needs P4's rebuild. Does AI launch and recovery work reliably? |
| P11 | Lasting damage | 15 | Can destroyed scenery, bridges and runways be put back at mission start (e.g. by explosions at saved positions) without side effects? |
| P12 | CSAR | 30 | Can an ejected pilot become a ground unit, and can a pickup by helicopter be detected reliably? |
| P13 | Loadouts | 26, munitions | Can the engine limit or set a player's weapons (warehouse stock, dynamic slots), or only advise? Map loadout pylons (CLSIDs) to warehouse weapon names. |
| P14 | DCS's built-in browser | 4 | (The F-4E's Virtual Browser, the same Chromium, doesn't open under Proton: 2026-11-19.) Does the CEF window render under Proton, open in-game with a key, work in VR, and load the engine's page? **Run 14l (2026-11-20): not under Proton.** The client script can open DCS's `WebViewWidget` and it fetches pages, but its renderer crashes on a Chromium check (CEF 106) as soon as it starts, so the window stays grey (the Start Here page too). The dxgui campaign window works instead. Not pursued: trying another Wine build would upgrade the shared DCS prefix. Decision: in-game UI = dxgui window; the full war map is a web page outside DCS. |
| P15 | Helicopter operations | 31 | Do AI transport and troop drops work well enough; can players carry campaign cargo? |
| P16 | Spoken AWACS calls | 36 | Can the server speak on SRS with text-to-speech? |
| P17 | Target imagery | 29 | Can images be made automatically, or only descriptions? |
| P18 | Electronic warfare | 33 | What does DCS model, and is it worth doing? |

## Phase 4: build depth

Roughly in this order; proofs gate their items.

1. **10** What each side knows, then **28** BDA (with **24** diverts for re-strikes).
2. **23** Air defence that fights back (P8, P9) and **25** mobile SAMs.
3. **14** Support aircraft.
4. **9** full: supply lines, repairs, munitions logistics (P13).
5. **3** overview UI becomes interactive: picking flights, downloading cartridges and kneeboards.
6. **16** full: debriefs and campaign history; **35** campaign news; Discord notifications in full.
7. **20** Commander priorities, **21** mission requests, **22** ground offensives.
8. **13** Carriers (P10).
9. **15** Lasting damage (P11).
10. **30** CSAR (P12).
11. **26** Editing flight plan and loadout (P13), then **27** building your own package.
12. **1** remaining scenarios, **38** difficulty settings, **19** full, **40** deeper combat.
13. **17** Running the war without DCS (mostly done by P7 and D1), **18** full server operation.
14. **34** Squadron life, stats page, time-lapse replay.
15. **36** AWACS calls (text first; P16 for voice).
16. **4** Overview inside DCS (P14, plus F10 drawings).

## Phase 5: long tail

**31** Helicopters (P15), **32** naval war, **29** target imagery (P17), **33** electronic
warfare (P18), **37** scenario editor.

## Every item at a glance

| Item | Type | Phase |
|---|---|---|
| 1 Scenarios | Build | 2 (one), 4 (rest) |
| 2 Planning and choosing a flight | Prove (P2) + build | 1, 2 |
| 3 War overview UI | Build | 2 (read-only), 4 |
| 4 Overview inside DCS | Prove (P14) + build | 3, 4 |
| 5 Per-type cruise speed | Build | 2 |
| 6 One campaign server | Build | 2 |
| 7 Theater data | Prove (P6) + build | 1, 2 |
| 8 Ground war | Prove (P3, P7) + build | 1, 2 |
| 9 Supply and replacements | Build; munitions prove (P13) | 2 (basic), 4 |
| 10 What each side knows | Build | 4 |
| 11 Players in the campaign | Prove (P1, P2) + build | 1, 2 |
| 12 Date, time and weather | Prove (P4) + build | 1, 2 |
| 13 Carriers | Prove (P10) + build | 3, 4 |
| 14 Support aircraft | Build | 4 |
| 15 Lasting damage | Prove (P11) + build | 3, 4 |
| 16 Briefings and debriefs | Build | 2 (briefing), 4 |
| 17 War without DCS | Build | 1 (P7), 4 |
| 18 Server operation | Prove (P4) + build | 2 (basic), 4 |
| 19 Victory and defeat | Build | 2 (simple), 4 |
| 20 Commander priorities | Build | 4 |
| 21 Mission requests | Build | 4 |
| 22 Ground offensives | Build | 4 |
| 23 Air defence that fights back | Prove (P8, P9) + build | 3, 4 |
| 24 Diverts | Build | 4 |
| 25 Mobile SAMs | Build | 4 |
| 26 Editing flight plan and loadout | Prove (P13) + build | 3, 4 |
| 27 Building your own package | Build | 4 |
| 28 BDA | Build | 4 |
| 29 Target imagery | Prove (P17) + build | 5 |
| 30 CSAR | Prove (P12) + build | 3, 4 |
| 31 Helicopters | Prove (P15) + build | 5 |
| 32 Naval war | Build | 5 |
| 33 Electronic warfare | Prove (P18) | 5 |
| 34 Squadron life | Build | 4 |
| 35 Campaign news | Build | 4 |
| 36 AWACS calls | Build; voice prove (P16) | 4 |
| 37 Scenario editor | Build | 5 |
| 38 Difficulty settings | Build | 4 |
| 39 Kneeboard pages | Prove (P5) + build | 1, 2 |
| 40 Deeper abstract combat | Build | 4 |
| 41 Briefing room in DCS | Prove (P19) + build | 1, 4 |
| Discord notifications | Build | 2 (basic), 4 |
| Time-lapse replay | Build | 4 |
| Stats page | Build | 4 |
| SAM emission control | Prove (P9) + build | 3, 4 |
| Air combat and fuel outside DCS | Prove (P7) + build | 1, 2 |
