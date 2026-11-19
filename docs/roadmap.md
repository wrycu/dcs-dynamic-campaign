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
| P1 | Multiplayer on a dedicated server | all | Do the bridge, Hooks, bubbles, slot gating and saves work on a dedicated server with two or more clients, players far apart, joining and leaving? Everything so far was tested in single player. **Part A passed (2026-11-18):** bridge and Hooks on a Linux dedicated server, slot gating for a remote client. |
| P2 | Player slots | 2, 11 | DCS 2.9 has dynamic slots (players pick an aircraft at an airbase, limited by the warehouse; seen in `MultiplayerSelectDynamicDialog.lua`). Can they replace fixed slots? Can the engine control them per base, and match a player who spawns to the flight they picked? Can anyone join a flight that is already airborne? |
| P3 | Ground movement at scale | 8 | Can moving ground formations follow roads in DCS, despawn and respawn mid-route, and stay smooth with many groups moving? Is `land.findPathOnRoads` (or similar) usable from the engine? |
| P4 | Mission rebuild and restart | 12, 18 | Can the engine write a .miz with the campaign's date, time and weather, have the server load it (Hooks `net.load_mission`), and carry on with no one restarting by hand? |
| P5 | Kneeboard delivery | 39 | Does DCS pick up new images in `Saved Games/DCS/Kneeboard` mid-mission or only at mission start? Which modules can load a .dtc cartridge? |
| P6 | Theater data | 7 | Can airbases, towns, roads and bridges be extracted from DCS for Syria (`map/towns.lua`, airbases and road paths from a mission script, map objects, `MissionGenerator/nodes.lua`) well enough to build objectives and links? |
| P7 | Abstract combat | 8, air combat | Does a simple model of ground and air fights give believable results? Test by running the war offline (an early version of item 17). |

## Phase 2: build the playable campaign

In order. Items in brackets are only the part needed now; the rest comes later.

1. **6** One campaign server, and the parts of **18** needed to run it (save on stop, status).
2. **5** Per-type cruise speed (small; do it while touching the flight model).
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
| P14 | DCS's built-in browser | 4 | Does the CEF window render under Proton, open in-game with a key, work in VR, and load the engine's page? |
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
12. **1** remaining scenarios, **38** difficulty settings, **19** full.
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
| Discord notifications | Build | 2 (basic), 4 |
| Time-lapse replay | Build | 4 |
| Stats page | Build | 4 |
| SAM emission control | Prove (P9) + build | 3, 4 |
| Air combat and fuel outside DCS | Prove (P7) + build | 1, 2 |
