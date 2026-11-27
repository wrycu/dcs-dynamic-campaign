# Planner (ATO): design note

**Steps 1–3 built (2026-11-25):** the abstract air war (`campaign/air_war.py`), the planner
(`campaign/planner.py`), a runner for the whole war (`campaign/war.py`), and both in the campaign
server for Southern Syria (step 3; its first DCS run is Run 24). See "Built so far" at the end.
Players can't pick flights yet (step 4). Roadmap
Phase 2 step 6, backlog item 2.
Decided so far: the sortie rates (enough choice), red fighting a defensive war of attrition, and
the AI flying unclaimed flights. Still open: the questions at the end.

## What it has to do

For the campaign to be playable (roadmap, "What playable means"), each side needs a plan of flights
that follows the war, and players need to pick one, get it into the cockpit and fly it. Flights
nobody picks are flown by the AI. Missions have to count whether or not they're in DCS: a CAS
flight flown by the AI 80 nm from any player must still kill tanks, and a SAM must still be able
to shoot it down.

## What exists already

- **Model:** `Package`, `Flight`, `Waypoint`, `ATO` and `Squadron` (`campaign/model.py`).
  `Flight.player_preferred` is there for flights offered to players first.
- **Flights in DCS** ([air](air.md)): ramp, runway and air starts; the waypoint actions `ORBIT`,
  `CAS`, `SEAD`, `BOMB`, `EXIT` and `LAND`; stock loadouts by name; squadron stock out and back;
  watchdogs; taking an airborne flight out of DCS and back.
- **Flights not in DCS:** the air bubble flies them along their waypoints and spawns them near
  players. But nothing happens at their target: an aggregated CAS flight orbits and goes home.
- **The fight model:** `combat.py` has `air_battle`, `sam_losses`, `shorad_losses`, `sead_kills`
  and `cas_kills` ([combat](combat.md)). Only the P7 war sim (`tools/war_sim.py`) uses them.
- **Into the cockpit:** Hornet `.dtc` cartridges (`campaign/dtc.py`). Players import them in the
  briefing or on the ground. Kneeboard pages work from the player's `Saved Games` folder (P5).
- **Slots:** the server can move a blue player into a Client slot built into the mission
  (`force_player_slot`), and the client script can put them straight in the cockpit (P19,
  Run 14d). It can't put a player into a dynamic slot (Run 14). A player can pick a dynamic slot
  themselves, limited by stock, but DCS doesn't let the server refuse it, and the player chooses
  the loadout (Run 9). So players join flights through Client slots, as decided after Run 14.
- **F10 menu:** the bridge already builds menus per coalition or group and reports selections
  (`menu_select`).
- **The ground war** gives targets: battles, formations marching to the front, threatened towns,
  and the SAM sites covering them.

## 1. The planning cycle

- **Rolling plan:** every game hour each side plans what takes off over the next 3 hours, with
  takeoffs spread every 10–20 minutes. Flights already planned stay. New ones fill gaps and react
  to the war (a new battle, a column on the move).
- **Sortie rate sets the size:** each squadron flies about 2.5 sorties per aircraft per day
  (blue; red about 2), from aircraft that are ready. Southern Syria's blue has 64 aircraft, so
  that's about 6–7 aircraft an hour: 3 or 4 flights of 2 (decided: enough choice for players).
  Red's 42 aircraft give about 3–4 aircraft an hour. Players' flights come out of the same stock.
- **Clock:** the war runs at 1× while players fly (D1), so the plan only needs to look a few hours
  ahead. Item 42 (faster war while players are picking) comes later.

## 2. What each side plans

Scored as value × urgency ÷ risk, highest first, until the aircraft for the cycle are used up.

**Blue (attacking, superpower, D2):**

| Mission | Against | Value from the ground war | Flight |
|---|---|---|---|
| CAS | an objective under attack, by either side | battles blue is fighting; more when the odds are close | 2× A-10C, F-16C or F/A-18C, 30–40 min on station |
| Interdiction | red formations marching to the front or massed next to it | their power and how soon they arrive | 2× F-16C, F/A-18C or F-15E |
| SEAD | SAM sites whose envelope covers a package's route or target, or the front | the packages they threaten | 2× F-16C or F/A-18C, with the package |
| Strike | airbases with red aircraft (runways), then depots and power plants | red sorties prevented; later supply (item 9) | 2–4× F-15E or F/A-18C |
| CAP | the front, where red fighters can reach | red fighters within reach | 2× F-15C or F-16C, 45–60 min on station |
| Escort | packages red fighters can reach | the package's value | 2× F-15C or F-16C |

**Red (defensive overall, but not passive):** red doesn't try to win the air war or match blue's
tempo. It fights a war of attrition: wear blue down where blue is weak, and stay out of fights it
would lose.

- **CAP** over its main airbases and Damascus: the core, always up.
- **Intercepts:** fighters launched (or a CAP pushed forward) at blue flights it can catch at a
  disadvantage: strikers and CAS without escort, packages whose escort or CAP has a gap (gone home,
  out of position, too few), and flights low on fuel on their way home. Under its own SAM cover
  where it can be, so blue chasing them runs into the SAMs. It avoids blue's CAP when that's
  stronger.
- **Supporting its own counterattacks:** when the ground war's defender moves to retake ground,
  red flies CAS and escorts for it, and a forward CAP over that fight. This is its main surge.
- **Attacking blue's ground forces:** Su-22s and Su-24s against blue formations on Syrian ground,
  mostly ones that are exposed: columns on the move, units in battle without air cover, ones
  beyond blue's SAM and CAP cover. 1–2 flights at a time, escorted when blue CAP is up.
- **Not:** sweeps into blue's airspace, raids on blue's bases in Jordan or Israel, or anything that
  needs air superiority to work.
- **Sortie rate** about 2 per aircraft per day (blue 2.5), and it holds back when it's losing
  aircraft faster than blue: attrition has to favour it.

How aggressive each side's air force is is a scenario setting (`[sides.red] air = "defensive"`
here; "cautious" or "offensive" elsewhere), so the same planner can play other kinds of war.

**Target knowledge:** both sides plan against what really exists for now. What each side knows
(item 10) comes later and plugs in here.

## 3. Packages, routes and loadouts

- **A package** is one target and one time on target (TOT): the main flight, plus SEAD if its
  route or target is inside a known SAM envelope, plus an escort if enemy fighters can reach it.
  SEAD arrives 2 minutes before the TOT and an escort with the main flight.
- **Flights** are 2-ships (strikes up to 4), from a squadron with the role and ready aircraft, at
  the squadron's base. Each gets its flight number when it's planned (`Flight.number`): blue a
  callsign of its own among the side's flights still planned or flying (Enfield 1 to Pontiac 9),
  red a numeric one. The flight is called by it everywhere (status, news, the war map) and its
  units in DCS have it: Uzi 4 flies as Uzi 4-1 and 4-2 ([air](air.md)).
- **Route:** takeoff, climb point, an ingress point (IP) 15–20 nm from the target, the target
  with its action (CAS zone, BOMB point, SEAD zone, ORBIT for CAP), an egress point, then land at
  the home base. It's meant to go around known SAM envelopes where that adds little, and
  otherwise rely on SEAD; today the legs are straight lines (backlog item 45, after Run 27).
  Altitudes are per mission: CAS 15,000–20,000 ft, strike 20,000–25,000 ft, CAP
  25,000 ft or more. Speeds are per leg (item 5, done).
- **Times:** the TOT is chosen and the rest worked back from cruise speeds. Players get a
  start-up allowance before takeoff: about 5 minutes for a hot start, 20 for a cold one.
- **Loadouts:** a table of mission → stock loadout name per aircraft type, from
  `data/payloads.json`, e.g. CAS F/A-18C "Mk-82*4, AIM-9*2, …".

## 4. Missions that count without DCS

This is the abstract air war: what happens when a flight that isn't in DCS reaches its target.
It's built on `combat.py`, the same functions P7 balanced:

- **On the way:** SAM sites whose envelope the route crosses, `sam_losses`. With SEAD in the
  package that has already arrived, fewer.
- **SEAD at its target:** `sead_kills`. A hit takes the site's tracking radar out of its
  composition, the same loss a DCS kill would cause, so the site is down until it's repaired
  (item 9).
- **CAS, interdiction and strike** at their target: `shorad_losses` against the target's own
  air defence, then `cas_kills` against the formations in the zone. Losses come off their
  compositions, as in a ground fight. Strikes damage the objective's features (`feature_damage`).
- **Air-to-air:** a CAP on station and an enemy flight passing within its radius fight with
  `air_battle`. A flight that loses the fight jettisons and goes home.
- **Mixed:** if one side of an engagement is in DCS, the other is spawned too, as ground targets
  of AI flights already are. DCS decides that engagement.
- **Dice:** these use the campaign's seed with the flight and waypoint, like the ground war, so
  a saved war replays the same.

Without this part the AI's sorties change nothing, and the war still stalls.

## 5. Players: choosing a flight

- **The list:** F10 > Other > Campaign > Flights shows each flight a blue player can take. It
  lists the flights from player-flyable squadrons with takeoff in the next 90 minutes, unclaimed
  or with free seats, e.g. `Springfield 1-1: CAS El Taebah, 2× F/A-18C, Prince Hassan, takeoff
  T+2:10 (in 25 min)`. Choosing one claims a seat in it: lead first, then wingmen.
- **On claiming:**
  - the flight is held for players, so the AI won't fly it;
  - the player gets a message briefing: the route as bearings, distances, altitudes and times
    (nm, ft, magnetic), the target, the TOT, the threats and the loadout. The cartridge and
    kneeboard pages are already in the mission (below).
- **Into the jet: the mission is rebuilt for each session** (decided 2026-11-25; P4 proved the
  rebuild). Every planned blue flight from a player-flyable squadron that takes off during the
  session gets its own group of Client slots in the .miz, one per seat, named for the flight
  (`Springfield 1-1`, `1-2`), parked at its base, with:
  - a hot start (engines running) by default; `player_start = "cold"` in `[server]` parks them
    cold instead (decided 2026-11-25);
  - the planned loadout (no general-purpose pool, no rearming);
  - the flight's route as the group's waypoints;
  - its cartridge in the .miz (`DTC/<flight>.dtc`, set to load itself on spawn), so remote players
    get it with the mission. Hornet first; other types as `dtc.py` learns them;
  - kneeboard pages per flight (route card, target, threats) in `KNEEBOARD/<type>/IMAGES` (P5).
    A mission can only give pages per aircraft type, not per unit, so every Hornet carries the
    pages of all the session's Hornet flights: an index first, then each flight by takeoff time.
  The slots' airbases need their aircraft in the warehouse stock, or DCS greys the slots out (Run 25);
  the probe sets them unlimited, the real build will stock what the campaign's squadrons have.
  Claiming moves the player into their seat (`force_player_slot`; the client script, if they have
  it, puts them in the cockpit). The Hooks refuse a seat to anyone who hasn't claimed it.
- **Seats players don't take are AI** (decided 2026-11-25): players may fly the whole flight; any
  seat no player claimed by the deadline is flown by the AI with them. A slot's Client/AI split is
  fixed in the .miz, so a missing wingman is a separate AI group spawned beside the players and
  told to follow their lead. Run 25: in the air, beside the lead, it keeps formation. From the ramp
  it taxis out and holds on the runway for the lead, blocking it for everyone else; so a wingman
  for an empty seat is parked switched off (uncontrolled) next to the players, and started when
  the lead starts to taxi: it starts up, taxis and takes off behind them (to confirm in DCS). On the ramp,
  spots must fit the aircraft (a Hornet in a small shelter was destroyed); `campaign/parking.py`
  checks them as the Mission Editor does, for every AI ramp start. The Client slots in the .miz
  need the same check when the build gives them parking spots.
- **Deadline:** a seat can be claimed until 10 minutes before takeoff. Then a flight with no
  players is flown by the AI as now (its Client slots stay empty), and a flight with players gets
  AI for its empty seats.
- **Flights planned after the rebuild** have no slots until the next one. So at each rebuild the
  planner fixes blue's player-flyable flights for the whole session; the hourly cycle still adds
  AI-only flights and red's.
- **As built (2026-11-27, `campaign/session.py`; see "The war map" in [server](server.md)):** the
  players choose on the war map, not an F10 list. The rebuild comes when a flight's lead presses
  **Commit** once everyone has joined (asked 2026-11-27), not at a fixed session start: the mission
  then has a Client group for every committed flight still on the ground. Because the seats are
  known then, a seat nobody took is an AI unit in the same group (the native wingman Run 25 showed
  working), not a separate parked group. **Fly** opens 20 min before take-off. Nobody in by 10 min
  after take-off: the AI flies it. A commit is refused while any player is in a jet. Not yet: the
  briefing message, kneeboard pages, warehouse stock from the squadrons (the slots' airbases get
  unlimited aircraft), and confirming in DCS that a Client unit takes the parking stand it's given.
- **Not in this step:** joining a flight already airborne (needs air-start slots in the .miz,
  P2), editing the plan (item 26), building your own package (item 27).

## 6. Build order

Each step is tested offline first, then in DCS:

1. **The abstract air war** (section 4), with the offline runner growing into a whole war:
   ground and air. Check: does air power break the stall at day 3–5, and are losses per sortie
   believable?
2. **The planner** (sections 1–3) for both sides, still offline. Check: sorties per day, the
   mix of missions, and the plan reacting to battles.
3. **AI flights in DCS:** planned flights spawn near players and fly their missions (mostly
   exists). A DCS run.
4. **Players:** build the session's .miz (a Client group per planned player flight, with its
   loadout, route, cartridge and kneeboard pages), the F10 list, claiming, the briefing message and
   moving the player into their seat. First a DCS probe: a cartridge in the .miz loading itself in
   multiplayer, and an AI group following a Client lead. Then a DCS run with the FA-18C.
5. **Empty seats and unclaimed flights to the AI**, and the deadlines.

## Questions to decide

None open.

Decided (2026-11-24): 3–4 blue flights an hour is enough; red fights defensively by attrition
(intercepts where blue is weak, supports its counterattacks, attacks exposed blue units; no
sweeps or raids into blue's airspace); the AI flies unclaimed flights.
Decided (2026-11-25): the mission is rebuilt each session with a Client group per planned player
flight carrying its loadout, cartridge and kneeboard pages (this also gets them to remote players
on a dedicated server); players may fly a whole flight, and the AI fills the seats they don't.
Decided (2026-11-26): player flights start hot (`player_start`, an option); the mission is rebuilt
at or near the start of a session's player flights, not during them, with those flights fixed at
the rebuild (details when the builder is written).

## Built so far (2026-11-25)

```bash
python3 -m campaign.war southern_syria --hours 168 [--seed 2] [--quiet]
```

Runs the whole war with no DCS: ground war, both planners and the air war (a week in about 15
seconds). It prints a line per day per side (sorties by mission, aircraft lost by cause, aircraft
ready, vehicles killed from the air) and writes `build/war/<scenario>-air-<hours>h.png` and `.log`
(every plan, fight, loss and kill).

- **The air war** (section 4) is as described, with what the first runs showed was needed:
  - a flight carrying bombs fights at 3 quality points less until it has attacked or jettisoned
    (`combat.LOADED_PENALTY`);
  - a side fighting by attrition (red) only takes fights it expects to win: its type, with GCI,
    against the enemy's, with AWACS and bombs counted. Its fighters jumped by stronger ones run
    for home 60% of the time;
  - red scrambles intercepts at flights over or near its ground with no blue fighters within
    50 km, only when it expects to win, and less often while it's losing more than it kills
    (`AirWar.attrition_ok`);
  - most parked aircraft are in shelters: a strike destroys few on the ground, but closes the
    runway (fewer sorties from that base) for about a day.
- **The planner** (sections 1–3): each task is scored, then divided by its SAM risk (the summed
  shot-down chances on the way and at the target; SEAD counts), and tasks above 15% risk aren't
  flown. CAP stations are moved back out of known SAM envelopes. A package may borrow 2 aircraft
  from the next hour so that SEAD and escorts fit. Standalone SEAD is limited to the 2 most
  dangerous sites near the front per hour.
- **Reproducible:** each roll uses the seed with the flight, waypoint or hour; the same seed gives
  the same war (a test checks it under different `PYTHONHASHSEED`s).

**Southern Syria, 7 days, offline** (the ground war alone: the coalition held 148–150 objectives
on day 7 and Syria kept about 480 manoeuvre vehicles):

| Seed | Coalition holds, day 7 | Syria's manoeuvre vehicles, day 1 → 7 | Red SAM sites up, day 1 → 7 | Blue sorties, lost | Red sorties, lost | Vehicles killed from the air |
|---|---|---|---|---|---|---|
| 1 | 156 | 587 → 229 | 27 → 12 | 880, 21 (2.4%) | 244, 34 | 260 |
| 2 | 167 | 583 → 308 | 25 → 8 | 847, 21 (2.5%) | 302, 33 | 219 |
| 3 | 153 | 587 → 363 | 26 → 11 | 752, 30 (4.0%) | 394, 29 | 134 |

- Air power breaks the ground war's stall: interdiction and CAS wear Syria's army down by about
  half in a week, and the coalition advances again from day 4–5.
- Red trades best on day 1, catching strikers with their bombs on, then holds its fighters back
  as its losses mount; by day 7 it has 7–13 aircraft left and flies little.
- Blue loses 2.4–4% per sortie, most on day 1; P7 (1991-like) was 0.5–1.2%. With no replacements
  (item 9) its ready aircraft fall from 64 to 32–41. To tune with item 40 and difficulty (item 38).

**In the campaign server (step 3):** Southern Syria runs the ground war, the air war and the planner
as systems. Flights near players spawn in DCS like any other (the air bubble): ramp starts at their
base when due, air starts as they come within 60 nm. Before a flight goes into DCS, the server fills
in DCS's airdrome id for its base on the `LAND` waypoint (`Engine.prepare_flight`); a base DCS doesn't
know becomes a plain waypoint. Air war news (fights, SEAD and strikes) far from players goes to the
log only. Status lists the next six flights to take off, the flights in DCS, and `Air:` and
`Planned flights:` lines.

**Not yet:** players claiming flights (step 4), the AI flying unclaimed ones after a deadline (step
5), routing around SAMs (now only avoided by scoring), and an escort that really escorts in DCS
(it flies the package's route with orders to engage air targets).
