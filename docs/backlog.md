# Backlog

The big items. Items 1–5 were asked for; 6–13 are needed for a working campaign; 14–19 are
worth having; 20–38 are further features, many from BMS; 39 (kneeboards) is needed; 40 deepens the P7 combat model; 41 is the briefing-room idea; 42 speeds up the war while pilots pick a flight; 43 makes players' airfields look alive. The order
they're done in, and which need proving first, is in the [roadmap](roadmap.md). Smaller known
gaps live in each area's "Not done yet" section ([air](air.md#not-done-yet),
[bubble](bubble.md#not-done-yet)).

## 1. Scenarios

Different kinds of war from the same engine. A scenario is a data file (sides, orders of battle,
doctrine, goals); the engine reads it to start a new campaign instead of the hand-built demo state.

- **Peer conflict.** Both sides have a modern air force, a layered IADS and a front line. Air
  superiority is contested; both sides plan offensive and defensive packages.
- **Superpower vs minor country.** Blue has many aircraft and stand-off weapons; red has few, older
  fighters and relies on SAMs, AAA and MANPADS, dispersed and hidden. Red air shows up rarely
  but can still hurt.
- **COIN.** No enemy air force. The enemy is small, mobile ground groups (technicals, mortars,
  MANPADS) that move, hide among civilians and attack bases and convoys. Missions are CAS, armed
  reconnaissance and convoy escort; the threat is low-level fire. Success is control of areas over
  time, not destroying a list of targets.

To decide:
- the scenario file format and what it covers (theater, sides, squadrons and bases, ground order
  of battle, IADS, doctrine knobs, victory conditions);
- how the planner's priorities change per scenario (e.g. COIN has no DCA/OCA, needs ISR and
  quick-reaction CAS);
- what COIN needs from the ground war that doesn't exist yet: insurgent groups that move and
  hide, attacks that happen without players, an area-control score.

## 2. Mission planning (ATO) and choosing a flight

Nothing plans flights yet: the demos build them by hand. Needed:

- **Planner.** Each side looks at the war (front line, threats, targets, its own aircraft) and
  builds an ATO each cycle: packages (strike, SEAD, escort, CAP, CAS…) with flights, routes,
  times on target and loadouts. Packages go where the fighting is, so players' choices are where
  it matters.
- **Choosing a flight.** Players see the flights their side has planned and pick one to fly,
  BMS-style: by mission, target, takeoff time and base. The flight is held for them; if nobody
  takes it before its deadline, the AI flies it or it is cancelled (`Flight.player_preferred`
  is already in the model).
- **Where to join.** Join a flight on the ramp before takeoff, or one that is already airborne.
  To decide: joining in the air needs a spawn at the flight's position, i.e. a player slot that
  exists there; with one .miz and no client mods, the realistic options are fixed slots per
  base (ramp starts) and moving the AI flight to meet the player. Needs a short design note.
- Players get the route as a `.dtc` cartridge (as in the demos), since the server can't push
  waypoints into their jet. To find out: which modules can load a cartridge (the FA-18C can);
  other modules need the route on the kneeboard instead (item 39).

## 3. War overview UI

A BMS-style view of the war: map with the front line, bases, squadrons, known threats, packages
and flights (planned, airborne, in DCS or not), losses and objective status, with time
controls hidden for multiplayer.

- A local web page served by the engine (it already holds the state), updated live over a
  WebSocket. Works on a second monitor, a laptop or a phone on the same network, and for the
  server operator.
- Read-only first; later the place where players pick flights (item 2) and download cartridges.
- To decide: the map (an offline tile set per theater vs. a plain projected outline), and how
  much of the enemy's state each side sees (fog of war: only what has been detected).

## 4. The overview inside DCS

Explore what can be shown in the sim. Options to try:

- **DCS's built-in browser (CEF).** DCS ships Chromium Embedded (`bin/libcef.dll`) and a GUI
  widget for it (`dxgui/bind/WebViewWidget.lua`). The Mission Editor's "Start here" dialog uses it
  (`MissionEditor/modules/me_start_here_missions_dialog.lua`). It loads a local HTML page and
  talks to Lua both ways: the page sends requests (`edQueryCallback`) and Lua answers them
  (`edQuerySuccess`). A window holding the overview page from item 3 could be opened in-game.
  - **Catch:** the widget exists only in the GUI Lua environment (Hooks), not in the mission
    script. Each player would need a small Hooks script in their Saved Games. That's a client
    install, though not one that touches the DCS install or breaks integrity checks (like SRS
    or Tacview). It would be opt-in; players without it still get the F10 options below.
  - To find out: does it render under Proton/Wine (does the "Start here" dialog work?), can an
    in-game window be toggled with a key, does it work in VR, and can it load
    `http://<server>:<port>` or only local files.

- **F10 map drawings** from the mission script: text, lines, circles and polygons
  (`trigger.action.textToAll`, `lineToAll`, `circleToAll`, `markupToAll`), per coalition. Already
  used for the flight markers in Run 7. Could show the front line, threat rings, package routes
  and objective status, refreshed periodically.
- **F10 radio menu** (`missionCommands`) for lists and actions: available flights, join a flight,
  ask for a status summary as a message.
- **Kneeboard:** images in the .miz reach clients, but only as they were when the mission
  started, so they can't follow the war. Possibly useful for a daily briefing if the engine
  rebuilds the .miz between sessions.
- **OpenKneeboard** (a separate app many players already use, VR-friendly): it has web page
  tabs, so it might show the overview page from item 3 in the cockpit with no DCS changes at
  all. To find out.
- **The F-4E's Virtual Browser** (RCtrl+V; `Mods/aircraft/F-4E/UI/VirtualBrowser`): a real web
  browser in the cockpit, with a start page linking to live websites, so it could show the
  engine's own overview page to F-4E crews with nothing installed. `tools/browser_probe.py`
  serves a test page (JavaScript clock, live polling, a mock flight card) to check it. **Doesn't
  open under Linux/Proton** (2026-11-19), which suggests DCS's built-in Chromium doesn't work
  there at all; that also counts against the Hooks-script option above (P14) for Linux players.
  Still worth trying from Windows: can you type an address, does the page run scripts?
- **Live kneeboard pages** (item 39): existing pages update when their files are rewritten.
- To find out: how many drawings DCS handles before the F10 map gets slow, and whether drawings
  can be shown to one group or player instead of a whole coalition.

## 5. Per-type cruise speed for simulated flights

Flights outside DCS fly at one fixed speed (`CRUISE_MS`, 220 m/s), and DCS routes use the
same value. Instead:

- a cruise speed (and maybe a cruise altitude) per aircraft type in the catalog, with a default
  for types not listed;
- optionally a speed per waypoint/leg, set by the planner (e.g. slow for a CAS stack, fast for a
  strike ingress), used both by `fly()` and in the DCS route;
- a package moves at the speed of its slowest flight, so its flights still meet their times on
  target.

## 6. One campaign server

Today each part runs in its own demo (`bubble_demo`, `air_demo`, `flight_bubble_demo`). A real
session needs one engine process that runs everything together: bridge and Hooks connections,
ground and flight bubbles, the planner, the war clock, autosave, and later the overview UI.
Everything below plugs into it, so it comes first.

- One entry point (e.g. `python -m campaign.server --scenario … --save …`) using
  `config/campaign.toml`.
- A clear order for each tick: telemetry and events in, clock advance, war simulation, planning,
  bubbles, save.
- The demos become thin scripts that set up a scenario on top of it, or get retired.
- **Built (2026-11-20):** `campaign/server.py` ([server](server.md)), with `[server]` settings, plug-in
  points for the war simulation and the planner, scenarios in `campaign/scenarios/` (the first,
  `khalkhalah`, is the bubble and flight demos together), save on stop and a status line and
  F10 status. Checked locally against a fake mission; not yet run in DCS. The demos stay until it
  has been.

## 7. Theater data

Objectives, airbases, links (roads, rail, front sectors) and the starting order of battle per
map, starting with Syria. The model has `Theater`, `ObjectiveDef` and `Link`, but no real data;
the demos place a few things by hand. Scenarios (item 1) are built on top of this.

- Decided (roadmap D3): a map layer generated from DCS once per map (airbases, towns, roads
  and links, bridges), cleaned up by hand, and a scenario layer authored on top (ownership,
  front line, order of battle), with rules filling in routine placements. The map layer starts
  with what the first scenario needs and is filled in over time.

## 8. Ground war

Ground formations hold position and nothing changes hands, so there is no front line yet.

- Movement along links, from each formation's `GroundOrder` (attack, defend, move, retreat), both
  in the engine and in DCS (a spawned formation gets a route).
- Fights resolved without DCS when no player is near (strength, type, posture, terrain, supply),
  and real DCS fights when one is.
- Objectives and airbases change hands. A captured airbase changes its squadrons, warehouse and
  player slots.
- The front line follows from who holds what.
- A ground commander per side that sets orders from the scenario's goals.

## 9. Supply and replacements

- Squadrons get replacement aircraft and pilots at a rate set by the scenario. Damaged aircraft
  (`Squadron.aircraft_damaged`) are repaired over time.
- Ground units use and receive supply over links from depots. Cut links (bridges, rail, convoys)
  and destroyed depots weaken the front, which is what makes interdiction worth flying.
- Munitions logistics: limited weapon stocks per base (see [air](air.md#not-done-yet)).

## 10. What each side knows

Each side sees only what it has detected: recon flights, AWACS and ground radar, players
flying over, contact with the enemy. Detections age and lose accuracy.

- The planner (item 2) targets only what its side knows.
- The overview UI (items 3 and 4) shows each side only its own knowledge.

## 11. Players in the campaign

- Slots open and close as bases are captured and aircraft run out. The slot gate in
  `lua/hooks/campaign_hooks.lua` already exists; it needs rules from the engine.
- A player's lost aircraft is taken off its squadron, like an AI loss.
- A pilot roster: killed, ejected, captured, rescued, with the effect on that player (a new pilot,
  a wait, or nothing) set by the scenario.
- Players joining and leaving mid-mission, and what happens to a flight when its player leaves.

## 12. Date, time and weather between sessions

DCS can't change a mission's date, time of day or weather once it is running. Without action,
the campaign clock and the sky drift apart.

- Before each session the engine rebuilds the .miz (via `tools/miz`) with the campaign's date,
  time and weather, plus whatever else must be in the mission file (e.g. slots, carrier positions).
- A weather model for the campaign, so weather changes between sessions and the planner can
  account for it.
- Scheduled mission restarts to go with it (see item 6).

## 13. Carriers

Players fly the Hornet, so carrier groups are needed.

- Carrier groups as campaign units that move, with their own squadrons and stock.
- Carrier operations in DCS: the group exists while players or its flights need it, sailing into
  the wind for recoveries. Carrier slots move with the ship (another reason for item 12).
- AI flights launching from and recovering to the carrier, and planned cycles.

## 14. Support aircraft

- AWACS and tankers planned on the ATO like other flights, on station where the packages need
  them, and in DCS while players or AI flights are close enough to use them.
- Their frequencies, TACAN and orbits in the flight briefing and cartridge (item 16).
- AWACS feeds what its side knows (item 10). Losing one is a real loss.
- JTAC for CAS: a ground or airborne controller that marks targets for players near the front.

## 15. Lasting damage

- Destroyed buildings, bridges, depots and runway cuts stay destroyed across respawns and
  sessions, until repaired (item 9).
- In DCS: scenery objects destroyed again at mission start (by position), destroyed statics put
  back as wrecks, runway damage from the campaign state.
- Damage feeds the war: a cut bridge breaks a link (item 8), a damaged runway stops takeoffs.

## 16. Briefings and debriefs

- Per-flight briefing: mission, target, threats known to the side, package and support
  frequencies, TACAN, times, weather. Delivered with the cartridge and in the overview UI.
- Debrief after a flight or session: what the flight did, kills and losses, mission success.
- Pilot stats and a campaign history (events log, losses over time).

## 17. Running the war without DCS

The whole campaign runs with no DCS connected, as fast as the computer allows: every flight and
ground fight resolved in the engine.

- For balancing scenarios: run weeks of war in minutes and look at the outcome.
- As a regression test: a fixed seed must give the same war.
- Needs the abstract fights from items 8 and "Air combat and fuel" below; most of the engine
  already works without DCS.

## 18. Server operation

- Admin commands (from the Hooks chat or a CLI): pause, save, skip time, force a new plan,
  give or remove aircraft, show status.
- Scheduled mission restarts that rebuild the .miz (item 12) and bring players back.
- Recovery when either side crashes: the engine restarting against a running mission is done
  (see [saves](saves.md)); DCS crashing mid-session needs the reverse.
- Server settings for a campaign: pause when no players are connected (the campaign clock only
  runs while people play, D1), and F10 showing only your own side (already the case for
  coalition slots on the Run 8 server). Document them, and check them at connect.
- After a mission load the engine asked for, unpause if the server starts missions paused
  (`Sim.setPause(false)` in the Hooks), so scheduled restarts run unattended.

## 19. Victory and defeat

- End conditions per scenario: objectives held, enemy strength, time limit, or for COIN, area
  control over time.
- A campaign-over summary, and starting a new campaign from it.

## 20. Commander priorities (BMS)

Players set what their side's planner favours: air superiority, SEAD, interdiction, CAS, strike
by target type, or areas of the map. One player can be the theater commander; otherwise the
scenario's defaults apply. Shown and set in the overview UI (item 3).

## 21. Mission requests (BMS)

A player asks for a mission on a target ("strike that bridge", "CAS at that town"). The planner
builds a package around it at the next planning cycle, or sooner if the request is urgent.

## 22. Ground offensives (BMS)

Planned operations: an objective, a start time and a buildup of ground forces. Before the push,
the air plan prepares the battlefield (interdiction, SEAD, strikes on defences); during it, the
air plan provides CAS and CAP over the front. Depends on the ground war (item 8).

## 23. Air defence that fights back (BMS: GCI)

- Early-warning radars and AWACS detect aircraft, including players, and feed a per-side air
  picture (item 10).
- A GCI/sector controller scrambles alert aircraft (QRA flights held on the ground) or redirects
  airborne CAP to intercept, by threat and priority.
- SAM sites are cued by the network: they can stay dark until the network says a target is coming
  (ties into SAM emission control under "Also queued").
- Losing radars or command posts degrades the network.

## 24. Diverts (BMS)

Airborne flights get new tasks when something important comes up: a SAM site that just showed
itself, troops in contact needing CAS, a moving convoy, an intercept (item 23).

- AI flights: new route and task, in DCS or in the engine.
- Player flights: a message with the new task and a new cartridge to load; accepting or declining
  is up to the player.

## 25. Mobile SAMs that move

Mobile systems (SA-6, SA-8, SA-11, SA-15 and similar) relocate after firing, after being
detected, or on a schedule, so an old intel picture goes stale and SEAD needs fresh information.

## 26. Editing the flight plan and loadout (BMS)

Before takeoff, players edit their flight's waypoints, times and weapons in the overview UI.
Loadouts are limited by the base's stocks (item 9). The cartridge and briefing are built from the
edited plan, and the DCS slot gets the chosen loadout where DCS allows it.

## 27. Building your own package (BMS)

Players put together a package themselves (e.g. strike + escort + SEAD) from available
squadrons, with AI filling the flights players don't take. The planner checks it against what's
available and fits it into the ATO.

## 28. Battle damage assessment (BMS: recon)

A strike's result is unknown to its side until someone checks: a recon flight, a player flying
over, ground reports. Until then the target shows as "struck, result unknown". The planner
schedules recon and re-strikes targets that are still working. Part of what each side knows
(item 10).

## 29. Target imagery (BMS: recon)

Recon results become pictures or descriptions in the briefing (item 16): target layout, defences,
damage. To find out: whether images can be made automatically (e.g. map renders with the target
marked) or only descriptions.

## 30. CSAR

Downed pilots, AI or player, are on the ground at their ejection point. They can be rescued by a
CSAR mission (helicopter, with escort) or captured by nearby enemy ground units over time.
Outcomes feed the pilot roster (item 11).

## 31. Helicopters

Transport, air assault (moving troops to objectives), attack helicopters at the front, CSAR
(item 30), and moving supplies by helicopter (item 9, using DCS cargo where possible). Planned
on the ATO like other flights, with players able to fly them.

## 32. Naval war beyond carriers

Surface groups and ports as campaign units: ships that move, anti-ship strikes, naval SAMs as
threats, ports as supply points (item 9). Carriers are item 13.

## 33. Electronic warfare

Stand-off jamming and escort jammers in packages, against the enemy's radars. DCS models
jamming only partly, so first find out what's possible in DCS; the engine side could still
reduce detection ranges in the air picture (item 23) while jammers are present.

## 34. Squadron life (BMS)

- Players join a squadron and appear on its roster.
- AI pilots gain skill over a campaign (DCS skill levels from experience); new replacements are
  green.
- Logbook per pilot (sorties, hours, kills, losses), ranks and awards.

## 35. Campaign news (BMS)

Daily headlines and intel summaries from the campaign's events log: "Red armour massing near X",
"Bridge at Y destroyed", "Airbase Z captured". Shown in the overview UI and as messages at
mission start.

## 36. AWACS calls

Threat and picture calls from the side's air picture (item 23): bullseye and BRAA to nearby
threats, "picture" on request through the F10 menu. As text messages first; optionally spoken
radio through SRS text-to-speech for players who use SRS.

## 37. Scenario editor (BMS: Tactical Engagement editor)

Build and edit scenarios (item 1) in a UI instead of by hand: place units and bases on the map,
set sides, doctrine, goals and starting time. Writes the scenario file.

## 38. Difficulty settings

Per campaign: AI skill, how much intel players get (fog of war strictness), how forgiving losses
are (pilot deaths, replacements), and each side's relative strength.

## 39. Kneeboard pages per flight (needed)

Players need their flight's details in the cockpit, and not every module can load a `.dtc`
cartridge (only the Hornet is known to work so far). The engine generates kneeboard images per
flight:

- route card (waypoints with bearings, distances, altitudes and times; nm, ft, magnetic),
- comms card (package, support, tanker and AWACS frequencies, TACAN),
- threats and target page (known SAMs and their ranges, target description, imagery from item
  29 when available),
- airfield page for takeoff, landing and divert bases.

Players get them like cartridges: download from the overview UI (item 3) or the engine's folder
and put them in their Saved Games kneeboard folder (`Saved Games/DCS/Kneeboard/`, per aircraft
or shared). Kneeboard images embedded in the .miz are fixed when the mission starts, so they
can only carry session-wide pages (e.g. a theater overview from the .miz rebuild, item 12).

Run 12 (P5): DCS collects the folder's pages when a cockpit is created, so a new page shows on
the next spawn; but a page already on the kneeboard **updates live** when its file is
overwritten. So a fixed set of campaign pages (flight, target, threats, package) that already
exist in the folder could follow the war during the mission, if something on the player's PC
rewrites them: an opt-in helper that fetches them from the engine, or a fresh download.
Goes with briefings (item 16) and is needed as soon as players pick real flights (item 2).

## 40. Deeper abstract combat

P7 showed the fight model in `campaign/combat.py` is believable but simple ([combat](combat.md)).
Deepen it once the playable campaign runs on it:

- **Settings:** move the type tables and constants (power, toughness, SAM chances, CAS rates)
  into scenario settings, and give `tools/combat_tables.py` a way to check a scenario's values.
- **Ground:** defenders in a town break later (2:1 at even quality wins 91% now; the 3:1 rule
  says about 50%); damaged vs destroyed vehicles (the war sim's repairs, made part of the model);
  artillery counter-battery; infantry that matters in towns; rivers and bridges as obstacles
  (P6 found the bridges); night and weather.
- **Air:** missiles and fuel limit a fight; escorts screen strikers; GCI for the side without
  AWACS; a flight that is jumped jettisons and aborts; helicopters (item 31).
- **Air defence:** SAM sites as formations that can be hit by strikes, not only SEAD; radar
  repairs that use supply; MANPADS with ground units.
- **Balance:** blue air losses run 0.5-1.2% per sortie (1973 rather than 1991). Decide what the
  game wants, maybe per difficulty (item 38).
- Not in this item: the ground commander that keeps a front line and secures its rear (item 8),
  red's air commander keeping its air force back (item 2/20), replacements (item 9).

## 41. Briefing room in DCS

The idea (not settled yet): players join the server, look at the war on a map inside DCS, pick a
flight there, and end up in its aircraft. Maybe even walk out to the jet. Overlaps items 2
(choosing a flight), 3 (overview) and 4 (overview inside DCS). Proof P19 finds out how much works.

What DCS offers (checked in the 2.9 install, 2026-11-19):
- **Getting to the jet, decided after P19 (2026-11-19):** three ways, all viable, offered side by
  side:
  1. **Drive with Combined Arms** (players who own it): drive a vehicle to the flight's parked jet;
     the server follows it and moves the player into the jet's slot. Proven in Run 14h.
  2. **Walk, with the opt-in client script:** eject from a ready-room jet and walk; the client
     script reports the player's position (the server can't see it otherwise). Not built yet.
  3. **A unit the engine moves:** no mods; a soldier or vehicle stands in for the player and the
     engine moves it on their commands.
  `-join` (teleport into the jet) stays for anyone who just wants to fly.
- **Walking:** three ways to try, roughest last.
  - **As the pilot after ejecting.** An ejected pilot can walk in first person, and on the ground
    that may be as simple as leaving a parked jet (LCtrl+E three times). The player would start in
    a "ready room" jet at the briefing area, climb out, and walk to the jet of the flight they want.
    Unknowns: does the server see the walking pilot's position (other players reportedly don't see
    it move), and does a player who walked up to a parked jet get moved into it?
    **Runs 14d to 14f:** in a parked Hornet, LCtrl+E ×3 fires the ejection seat; after the
    parachute landing the player walks in first person. But the walk is local to their PC: the
    server's pilot object walks its own fixed straight line, so the server can't tell where the
    player is. Only the player's own DCS knows (e.g. the client script reading the camera position
    and reporting it over RPC). A walking player can be moved back into a jet (spectators, slot,
    `-enter`).
  - **Runs 14g/14h, Combined Arms: works.** A vehicle the player drives is a mission unit, so the
    server follows it and `getPlayerName()` names the driver; driving an armed Humvee to a parked
    Hornet moved the player straight from the Combined Arms slot into the jet's slot, and the
    client script spawned them. Needs the Combined Arms module and blue Combined Arms slots.
  - **Run 14i, Combined Arms infantry:** of ten soldier types, only `Soldier stinger` can be
    occupied (the rest: "wrong vehicle type"). The server follows it as it walks, but the player
    is stuck looking down the Stinger's sight (F1/F2 don't change it), so it's not usable for walking.
    Combined Arms means driving.
  - **A unit the engine moves.** A soldier or vehicle stands in for the player, moved by the engine
    from F10 menu or chat commands ("walk to Hornet 2"), watched from outside. Janky, but needs
    nothing.
  - **Driving with Combined Arms,** for players who own it: they control a soldier or vehicle
    themselves.
- **No-mod route:**
  - **Spectators** can open the F10 map. In PvE every player is blue, so the campaign's
    drawings can go to everyone.
  - **Chat** reaches the server Hooks (`onPlayerTrySendChat`) from spectators, so `-flights` and
    `-join 2` could work with nothing installed. The server answers with `net.send_chat_to`.
  - **Server-side join:** the slot dialog spawns a dynamic-slot aircraft with
    `DCS.create_client_aircraft(spawn)`. The spawn data has the type, base, fuel, pylons, livery,
    hot or cold, and an `isInAir` flag the dialog always sets to false. If the server can call it
    for another player, a flight pick could put that player straight into the flight's jet with
    the planned loadout and fuel. `isInAir` might even allow joining a flight in the air (P2's
    open question). `net.force_player_slot` moves a player into a fixed slot.
- **Opt-in client script** (a Hooks file on the player's PC, like SRS or the Scratchpad mod;
  nothing in the DCS install changes):
  - **A real in-game window.** DCS's own UI toolkit (dxgui: windows, lists, buttons, hotkeys) is
    available to Hooks scripts and isn't Chromium, so it may work on Linux where the browser
    didn't. DCS even has a map widget (`NewMapView`, used by the slot screen), possibly reusable
    for a war map.
  - **Data both ways with no network setup:** DCS ships an `RPC` module that its own multi-crew
    Hooks use to call functions between the server's GUI and a client's
    (`RPC.sendEvent(player_id, method, ...)`).
  - **One-click join:** the script could call `create_client_aircraft` itself, exactly as the
    slot dialog does, with the flight's loadout.

**A basis for the room itself: VNAO Ready Room.** The user picked this (2026-11-19) as a strong
starting point for the actual briefing room, since it already does much of what we want. Read from
the v0.181 download (readme and Lua only; nothing run):
- **What it is:** a squadron ready room by VNAO Simulations (Particleman), v0.18 from June 2020.
  It's a flyable aircraft mod (`Saved Games\DCS\Mods\aircraft`), built as a helicopter with its own
  small flight model DLL (`bin/VNAO_RR.dll`, closed source). It's a 25-seat multicrew aircraft
  (`crew_size = 25`, roles "seat 01"...), so pilots slot into its seats like any multicrew jet.
  It's placed in the mission like a helicopter, on land or on a carrier, for red or blue.
- **What it does:** VR, TrackIR and 2D; seat 1 runs the slideshow and a dimmable light, and has
  the comms menu (audio, mission triggers). Ambient sound, squadron liveries for posters and
  chairs, and the number keys move you between seats.
- **How the slides work:** 20 fixed images, `DROP_SLIDES_HERE/VNAORR_slide_01..20.tga`, drawn on
  the screen by the cockpit scripts. Only the slide *number* is shared in multiplayer (an
  animation argument, 399, which seat 1 steps). So **every player shows their own local copy** of
  the images: the server can't send pictures. A campaign briefing (war map, the flight's route and
  loadout) would need the images on each player's PC before they look, e.g. written there by the
  opt-in client script, and DCS may only load them when the cockpit loads.
- **Fits with what Run 14 found:** the room is a real slot, so pilots sit in a 3D room instead of
  on the spectator F10 map, and the engine could move them on to their flight's mission-built
  Client slot with `net.force_player_slot`.
- **To check before building on it:**
  - Does it still load in DCS 2.9 (its plugin version string says 0.16; built for 2.5)?
  - Every player needs the mod installed, and so does the dedicated server (it's an aircraft
    type). So it's opt-in, and the spectator F10 map stays as the no-mod fallback.
  - Does `force_player_slot` move a pilot from a ready-room seat straight into a flight's slot,
    or only from spectators? Run 14 only proved the move from blue spectators.
  - Can the screen show engine-made images: does DCS reload the `.tga` files when a pilot enters
    the room, or only when DCS starts? Could the cockpit scripts draw a live map instead?
  - The readme has no license. Ask VNAO before shipping it or a changed copy.
- **Links:** [the mod](https://www.digitalcombatsimulator.com/en/files/3310351),
  [the VR ready room thread](https://forum.dcs.world/topic/186044-vnao-vr-ready-room-mod).

## 42. Faster war while pilots pick a flight (BMS: time compression)

Like the BMS campaign UI: while the players on the server are looking at the war (the overview,
item 3/4/41) and choosing a flight, the war runs at a multiple of real time. Once a pilot
commits to a flight, it drops to real time. Asked for 2026-11-20.

- **Fast:** players are connected but nobody has committed (spectators, the war UI, the
  briefing room). The clock runs at `[server] pick_speed` (e.g. 10x; 1 turns it off). Flights
  move, takeoff times come up, the ATO turns over, so there is always something about to launch.
- **Real time:** from the moment anyone commits to a flight (picks it in the UI, item 2), before
  their jet spawns, so the bubble around them fills at real time. One war for everyone: one
  committed pilot slows it for all. It goes fast again when nobody is committed or flying
  (after a short hold, so it doesn't flap).
- **Fits D1:** the war still only moves while people are on the server. Nobody connected
  still pauses it.
- **Committing to a flight far in the future:** at 1x a pilot could wait an hour for takeoff.
  Either only offer flights taking off soon (BMS lists those and the pilot waits in the UI), or
  keep fast time until the committed flight's takeoff is a few minutes away, then drop to 1x.
- **Safe because of the bubbles:** before anyone commits, nothing is near a player, so every
  flight and formation is engine-only and nothing in DCS has to keep up. Until the abstract
  fights exist (item 8, "Air combat and fuel" below), fast time only moves flights and the
  clock.
- `WarClock` multiplies the model-time step, and its 120 s cap scales with it.
- **DCS time of day stays at 1x,** so the sun falls behind the campaign clock during a session.
  The next mission restart (item 12) catches it up. Show it in Status, and maybe cap the
  speed-up per session.
- **Show the speed** in Status and on the war map, and announce changes ("War at 10x" / "Viper 1
  committed: real time").

## 43. Living airfields

An airfield that a human flight departs from or recovers to (including diverts, item 24) should
look in use while players are there: parked aircraft, ground crew and vehicles, fuel trucks, and
air defence. Not for AI flights: too much to spawn for nobody to see. Asked for 2026-11-23.

- Ideally the real assets: the squadrons based there (item 7's theater data, item 34) parked in
  their own spots, their losses showing as empty spots or wrecks (item 15), and the base's
  defences from the campaign state.
- Static objects where possible (cheap), live units only for what has to act (air defence).
- In DCS while a player is at or near the base (taking off, landing, or diverting there), gone
  when they've left. The parking that flights need stays free.

## Minor improvements

Small additions that make the campaign easier to use; each can be done once the parts it
depends on exist.

### Discord notifications

Post to a Discord channel when a mission comes up that players should know about, mentioning the
role for that kind of mission so the right people see it and can join. For example:
"@recon BDA needed on the bridge at X (struck 14:20); takeoff from Y within the next hour".

- What triggers a post (all configurable): a new player-preferred flight on the ATO (item 2),
  a BDA request (item 28), a CSAR (item 30), a divert or an urgent mission request (items 21,
  24), a ground offensive starting (item 22), a base under attack or captured, campaign news
  (item 35), and session start and end.
- Each kind of post maps to a Discord role to mention (e.g. `recon`, `sead`, `cas`, `helo`,
  `csar`), set in `config/campaign.toml`. Posts can also be limited by side.
- Uses a Discord webhook (no bot needed). The webhook URL is a secret: read it from an
  environment variable or an untracked local file, not from the tracked config. Mentions use
  role ids with `allowed_mentions`, so a post can't ping @everyone by accident.
- Rate limits: merge bursts into one post and don't repeat the same request until it changes.
- Later: a bot that also lets players claim a flight from Discord.

### Time-lapse replay

Play the war back on the overview map (item 3): the front line, bases changing hands, losses and
major strikes over days, at a chosen speed. Built from saved snapshots or the events log
(item 16's campaign history).

### Stats page

Per pilot and per squadron: sorties, hours, kills by type, losses, mission success, and
leaderboards. Built on squadron life (item 34) and the debriefs (item 16); shown in the overview
UI, optionally on a public page.

## Also queued
- SAM emission control: radars on a schedule or in ambush mode ([bubble](bubble.md#not-done-yet)).
- Air combat and fuel for flights outside DCS ([air](air.md#not-done-yet)).
