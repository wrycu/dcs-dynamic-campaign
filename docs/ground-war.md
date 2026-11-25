# Ground war

Item 8, roadmap Phase 2 step 4. Formations march along links, fight for objectives and take
them; the front line follows from who holds what. The code is `campaign/ground_war.py`, a
`System` the campaign server runs every tick, built from the P7 war sim (`tools/war_sim.py`,
[combat](combat.md)).

```bash
python3 -m campaign.ground_war southern_syria --hours 168 [--seed 2] [--quiet]
```

Runs the scenario's war with no DCS (7 days in about 3 seconds). It prints every attack, capture
and retreat plus a line per day, and writes `build/war/<scenario>-<hours>h.png` (the map at the
end: battles ringed, moving formations with a line to where they're going) and `.log`.

## How it works

- **Movement.** A formation with a route marches along the links' DCS road routes at 25 km/h
  (halts included). Off the map, its position moves along the road. In DCS, the engine sends the
  group its current leg ("On Road", 25 km/h) and reads its progress back from its position.
- **Attacks.** A formation heading for an enemy objective stops 3 km short and attacks
  whoever holds it: the formations there and the local garrison. In DCS it then drives into the
  objective across country.
- **Garrisons.** Every town and airbase starts with local troops, more in bigger towns. Red's
  are infantry, BTR-80s and ZU-23s; blue's are infantry and armed Humvees. A side that takes an
  objective leaves a small garrison of its own. A garrison is a formation (role `GARRISON`,
  order `HOLD`) that never moves. It stands round the edge of its town in 3–8 strongpoints, each
  a mix of infantry and vehicles, facing out. It spawns in DCS like any other, but only within
  8 nm of a player (`garrison_spawn_nm`, [bubble](bubble.md)): there are 121 of them, about 3,500
  vehicles in all.
- **Town edges.** The map layer has no town outlines, so the edge is estimated from the town's
  size (priority, from its buildings): 500, 750 or 1,000 m from the centre; 1,500 m for an
  airbase.
- **Defence.** Formations standing at an objective the enemy threatens deploy on its edge, on
  the road the enemy is coming on, side by side 400 m apart. The threat is
  the neighbour that enemy formations are coming from, or else the one where the enemy is
  strongest. So attackers stopping 3 km out are 2–2.5 km from the defence line, and in DCS
  they meet it before they reach the town. With no threat, formations stay where they are.
- **Fights** are hourly, as in P7 (`combat.ground_hour`): attackers' and defenders' fire,
  terrain, artillery from next door, breaking at 60% strength or 35% morale, called off after
  12 hours.
- **Fights near players.** Formations that are in DCS take only DCS losses, and break by their
  losses. Abstract fire only hits formations and garrisons that aren't in DCS.
- **Captures.** The objective changes hands, with the infrastructure in its area. That happens as
  soon as nothing defends it any more (e.g. the garrison was killed in DCS), not at the next hour. Air
  defences left there are overrun, and squadrons based there move to the nearest base of their
  side that is more than 25 nm from the enemy. The loser's formations retreat to a neighbour
  they hold, away from the enemy. With no neighbour left, they surrender.
- **Recovery.** Formations not fighting regain morale. Their supply comes back faster the
  nearer they are to one of their side's airbases or military sites. A formation below 20%
  strength is destroyed as a unit.
- **Commanders** give orders every 3 hours. A scenario sets each side's `posture`, and the
  attacker's `goal` ([scenarios](scenarios.md)).
  - **Attack:** attack front objectives where it has 2:1, terrain and garrison included. The
    most valuable go first: airbases, big towns, those nearer the goal. With everything free it
    gathers for the best target it can't take yet, and everyone else moves up to the front.
  - **Defend:** retake its own country's ground wherever it can get 2:1, leaving one formation
    on anything the enemy is next to. Reserves go to threatened objectives: a front objective
    whose defence (terrain included) is less than the enemy formations next to it or heading
    for it. The nearest go first, by road, until the defence matches. A reserve is sent only if
    the road is at most 60 km (about 2.5 hours), and at most 1.6 times the straight line plus
    10 km, so it doesn't loop round enemy ground. It never attacks into the enemy's countries.
- **What never moves:** formations with order `HOLD`, air defence (SAM, AAA), and neutral
  ground, which nobody enters.
- **Status** (F10 > Other > Campaign > Status) shows the front, each battle with its hours and
  both sides' strength, who is moving where, and how many garrisons are in DCS. News of attacks,
  captures and retreats goes to everyone as messages.
- **Hourly battle reports**, e.g. "El Taebah, hour 2: Coalition attackers at 100%, Syria
  defenders at 75%". They're messages in DCS when the battle has anything in DCS (players are
  near), and only in the log otherwise.

Each hour's dice come from the seed and the hour, so a loaded save goes on the same way.

## Southern Syria, offline (2026-11-24)

Without an air war (the planner, item 2) or replacements (item 9), seeds 1–3 over 7 days. Rerun
after the Run 22 changes (garrisons as formations, the limits on reserves):

| Seed | Coalition gains: day 1 | day 3 | day 7 | Manoeuvre vehicles left on day 7: coalition (of 212) | Syria (of 605) |
|---|---|---|---|---|---|
| 1 | +11 | +21 | +28 | 189 | 484 |
| 2 | +12 | +25 | +36 | 158 | 376 |
| 3 | +9 | +24 | +28 | 159 | 484 |

Gains are objectives held beyond the coalition's starting 120.

- The coalition takes the border towns on day 1 and pushes towards Daraa and up the Golan.
- Syrian counterattacks retake what it leaves to garrisons.
- Before the limits on reserves, Syria sent almost its whole army to the first town attacked,
  most of it on 10–15-town loops round the coalition's ground. Now it sends the nearest few.
- After about day 3–5 neither side can get 2:1 anywhere, and the war mostly stalls.
- The stall is where air power and players come in: CAS and strikes that wear Syria's
  formations down below what they need to hold.

## Not done yet

- **DCS side of captures:** a captured airbase doesn't change coalition in DCS, nor its
  slots or warehouse (item 11).
- **Defence beyond the boundary:** defenders hold the edge of their objective. They don't
  send a small force out to delay or harass an attack on its way in, or set up further out on
  good ground (backlog item 8).
- **Stuck in DCS:** a group that DCS can't drive (e.g. no road) never finishes its leg. It
  needs a watchdog that falls back to the abstract march.
- **Bridges:** a bridge's owner doesn't change, and a destroyed bridge doesn't cut its link.
- **Commanders** are still the P7 ones, slightly improved. There's no real front line to hold,
  no reserves kept back, and no use of the scenario's victory goal (item 1).
- **How they move in DCS:** whole battalions as single road columns in no set order (artillery
  can lead), all on the same road, and on the road right up to the stop point. See backlog
  item 8 for the plan: march order, smaller mixed groups, several paths, off-road at the end.
- **Replacements and repairs** (item 9).
