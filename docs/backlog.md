# Backlog

The big items. Items 1–5 were asked for; 6–13 are needed for a working campaign; 14–19 are
worth having; 20–38 are further features, many from BMS; 39 (kneeboards) is needed. The order
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
  waypoints into their jet.

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
Goes with briefings (item 16) and is needed as soon as players pick real flights (item 2).

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
