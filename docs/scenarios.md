# Scenarios

The scenario layer (roadmap D3): a campaign's starting state, authored on top of a map layer
([theater](theater.md)). A scenario is `data/scenarios/<name>.toml`; `campaign/scenario_file.py`
reads it and builds the starting `CampaignState`.

```bash
python3 -m campaign.scenario_file southern_syria          # check it; picture: build/scenarios/southern_syria.png
python3 -m campaign.server --new --scenario southern_syria   # play it
```

## The file

| Key | |
|---|---|
| `name`, `title`, `description` | |
| `map` | The DCS terrain; its map layer is `data/theaters/<map>/map.json`. |
| `magvar` | Magnetic variation (degrees east), for the bearings below. |
| `[area]` | `center` (an objective id) and `radius_nm`: objectives within are in play; the rest of the map is outside the war. |
| `[sides.blue]`, `[sides.red]` | `name`, and `countries`: each side holds the objectives in its countries (from the map layer). Objectives in any other country are neutral. |
| `[owners]` | Hand overrides: objective id = `"blue"` or `"red"`. |
| `[[squadrons]]` | `id`, `name`, `side`, `type` (DCS aircraft type), `base` (an airbase objective id the side holds), `aircraft`, `pilots` (default: aircraft), `roles`, `player`. |
| `[[formations]]` | Hand-placed ground forces: `side`, `at` (an objective id), optionally `brg_nm = [magnetic bearing, nm]` from it, `templates` (one formation each, `campaign/templates.py`), `name`, `order` (default `DEFEND`). |
| `[rules]` | Routine placements, below. |

Several formations at one place are spread out (1.5 km, 2.1 km, 2.6 km... from it, at even
angles), and never on water. The map layer doesn't know where buildings stand, so in DCS each
vehicle moves before its first spawn to the nearest clear ground (up to 1 km away;
`campaign/spawner.py`, siting): the picture shows where the scenario puts the formations, not
those final spots.

## Rules

- `military_sites = true` (default): every red military site in play gets formations by what its
  name says: air defence regiments (SyADF) an SA-3 and an SA-6; artillery two artillery
  battalions; an armoured division two tank and a mechanised battalion; another division, a
  brigade or a regiment a tank and a mechanised battalion; a training ground an old (T-55) tank
  battalion. (`SITE_RULES` in `campaign/scenario_file.py`.)
- `airbase_defence = { red = [...], blue = [...] }`: templates at every airbase a side holds.
- `city_defence = { red = { min_priority = 3, templates = [...] } }`: templates at every town of at
  least that priority.

## Templates

`campaign/templates.py`: what a battalion of each kind is, in DCS types. Red: `red_tank_bn`
(T-72), `red_old_tank_bn` (T-55), `red_mech_bn`, `red_art_bn`, `red_sa2`, `red_sa3`, `red_sa6`,
`red_sa11`, `red_sa8`, `red_aaa`, `red_ewr`. Blue: `blue_tank_bn`, `blue_mech_bn`, `blue_art_bn`,
`blue_hawk`, `blue_avenger`.

## Southern Syria

The first scenario (D2: superpower vs minor country), decided 2026-11-24: a US-led coalition
flying from Jordan and Israel against Syria, with the front at the borders.

- 90 nm around Khalkhalah: southern Syria up to the Homs gap, northern Jordan, northern Israel,
  and Lebanon (neutral). 308 objectives in play: blue 120, red 140, neutral 48.
- Blue: F-16C (Muwaffaq Salti), F/A-18C (Prince Hassan), A-10C (King Hussein Air College) and
  F-15E (Ramat David) for players, F-15C (Ramat David); 64 aircraft. Two battalion groups staging
  at Ramtha and Irbid, Hawks at the forward bases, Avengers at every airbase.
- Red: MiG-29 (Sayqal), MiG-23 (Al-Dumayr), MiG-21 (Khalkhalah), Su-22 (Tha'lah), Su-24
  (Shayrat); 42 aircraft. Around Damascus three SA-2s, three SA-3s and an SA-11; SA-6s down the
  Daraa highway; early warning on Mount Hermon, at Ad Dumayr and Busra; brigades at every SAA
  site. 103 formations, 856 vehicles.
- The front: 4 links (the Ramtha–Daraa crossings and Kiryat Shmona). The Golan has few towns big
  enough to be objectives; edits to the map layer can add some.

Nothing moves or plans yet: that's the planner (item 2) and the ground war (item 8).
