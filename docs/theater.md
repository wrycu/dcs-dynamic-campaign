# Theater data

The map layer (backlog item 7, roadmap D3): what's on a DCS map that the war is fought over.
Objectives (airbases, towns, military sites, bridges) and the road links between them, generated
from DCS once per map and cleaned up by hand. The scenario layer (who owns what, the front line,
the order of battle) is authored on top of it per scenario.

| File | |
|---|---|
| `data/theaters/<map>/map.json` | The map layer: a `Theater` (`campaign/model.py`). Generated; tracked in git. |
| `data/theaters/<map>/edits.toml` | Hand clean-up, applied by every build. Tracked in git. |
| `data/theaters/<map>/geography.json` | Country borders, rivers and lakes (Natural Earth). Tracked in git. |
| `data/theaters/<map>/water.json` | Where DCS has water, on a 1 km grid. Generated with the map layer; tracked in git. |
| `build/theater/<map>-scenery.json` | The scenery survey: counts of every scenery type, and the infrastructure-looking objects. Local. |
| `build/theater/<map>-cache.json` | Every answer DCS gave the builder, so most rebuilds don't need DCS. Local. |
| `build/theater/<map>-map.png` | A picture of the map layer, to check it by. Local. |

Code: the rules are in `campaign/theater_build.py`; `tools/extract_theater.py` asks DCS and
writes the files; `campaign/theater.py` loads `map.json`.

## Building it

```bash
python3 -m tools.import_geography               # borders, rivers, lakes: no DCS (downloads Natural Earth once)
python3 -m tools.extract_theater                # with DCS (whole map, ~30 min): start campaign-dev/campaign-smoke.miz, take the jet
python3 -m tools.extract_theater --offline      # after changing edits.toml: no DCS needed
```

| Flag | Default | |
|---|---|---|
| `--map` | `Syria` | DCS terrain name. |
| `--center`, `--radius-nm` | whole map, `100` | Only the area within the radius of this airbase. By default the whole map: every airbase plus 30 nm. |
| `--min-buildings` | `800` | Towns with at least this many buildings become objectives. |
| `--max-towns` | none | At most this many towns, the largest. |
| `--link-max-nm` | `25` | The longest link, except where one is needed to connect the network. |

`--offline` stops and lists what's missing if the edits need something DCS hasn't answered yet,
e.g. the road route of an added link or the size of a kept town. A run with DCS fills it in.

## The rules

1. **Airbases:** DCS "airbases", except helipads, which are told apart by name: HS, HL, HI, HJ,
   HC, HT (one prefix per country), HMed (hospitals), HStad (stadiums), HOil (oil fields),
   `H_med_orig_NN`, "... Heliport". Helipads aren't used yet. Not by runways: some real airfields
   (At Tanf, Kharab Ishk) report none. An airbase's features are its runways, weighted equally.
2. **Places** come from the map's `towns.lua`, sorted by name:
   - **landmarks** (mosques, hotels, ministries, towers...) aren't towns. Each joins the nearest
     town within 4 km, or is dropped;
   - **military sites** (`SAA 121th Brigade`, `SyADF 150th Regiment`, training grounds) become
     DEPOT objectives;
   - **towns** within 2.5 km of each other are one town, named after the place nearest its middle.
3. **Size** is the number of buildings within 1.5 km (types with HOUSE, BUILDING or BLOCK in the
   name; not trash cans, walls or market stalls). Towns with `--min-buildings` or more become
   CITY objectives, priority 3 (3,000+ buildings), 2 (1,500+) or 1.
4. **Links:** a link a–b unless some other objective is closer to both (the relative
   neighbourhood graph, which looks like a road network), up to `--link-max-nm`, plus whatever
   is needed to connect everything. Each link gets DCS's road route (`land.findPathOnRoads`,
   simplified to 10 m). A link with no road route is kept as `offroad`.
5. **Bridges:** bridge scenery in the area (searched in 15 km tiles, which also make a survey of every
   scenery type, `build/theater/<map>-scenery.json`, for infrastructure to come) (type names with "bridge" or "most"); twin spans
   within 60 m count once. A bridge within 40 m of a link's route becomes a BRIDGE objective, and
   the link lists it in `via`, in order. Its feature's `dcs_ref` is the scenery object's id, for
   destroying it or checking whether it's still there.

6. **Infrastructure** comes from the scenery survey: objects of the same kind are grouped into
   sites, each named after the nearest town, airbase or military site and in its area
   (`area_of`, so it changes hands with it). Each site's features are up to 20 of its objects
   (`dcs_ref` = scenery object id). Tuned on Syria (2026-11-24):

   | Type | Scenery types | Grouped within | At least | Priority | Syria |
   |---|---|---|---|---|---|
   | REFINERY | `OILGAS_REFINERY_*` | 1.5 km | 3 | 4 | 29 |
   | POWER_PLANT | `POWER_PLANT_*`, `*ELECTROSTATION*` | 1 km | 2 | 4 | 18 |
   | SUBSTATION | `POWER_TRANS_STATION` | 400 m | 1 | 2 | 41 |
   | FUEL_DEPOT | fuel and liquid tanks, not within 3 km of an airbase | 600 m | 10 | 3 | 68 |
   | DAM | `DAM_*`, `*_DAM` | 2 km | 1 | 3 | 4 |
   | FACTORY | cement works; industrial areas (`*INDUSTRIAL*`, not at airbases) | 1 km; 300 m | 1; 200 | 3; 2 | 57 |

   Sites DCS models individually keep their real names: Tishrin, Atatürk, Berdan and Aksifat
   dams, the Lafarge and Konya cement works, the Aleppo power station.
7. **Countries:** each objective's `country`, from `geography.json`'s borders (or the nearest
   country within 3 km, for points just off Natural Earth's coastline). Natural Earth draws the de
   facto lines (the Golan up to the ceasefire line in Israel, Quneitra in Syria; the West Bank
   and Gaza as Palestine) and keeps Northern Cyprus, the UN buffer zone and the British bases
   (Akrotiri, Dhekelia) apart; a scenario decides who actually holds what.
8. **Water:** DCS's surface type (`land.getSurfaceType`) every 1 km, stored as `water.json`. Rivers
   are narrower than that and come from `geography.json`.

## Geography

`python3 -m tools.import_geography` takes Natural Earth's 1:10m countries, rivers and lakes
(public domain, naturalearthdata.com; version pinned in the tool), keeps what touches the map,
converts it with `campaign/geo.py` and writes `geography.json`. The projection is DCS's own
(transverse Mercator per terrain): on Syria it matches `coord.LLtoLO` within 3 cm for all 1,182
places. `campaign/geography.py` loads both files: `country_at(pos)`, `country_near(pos)`,
`is_water(pos)`, and the rivers as polylines.

## Hand edits

`data/theaters/<map>/edits.toml`:

```toml
[objectives]
rename = { town_qura_al_assad = "Qura al-Assad" }
drop = ["mil_army_vehicle_training_ground"]
keep = ["town_shahba"]            # include although it isn't among the largest
add = [ { id = "depot_south", name = "Southern depot", type = "DEPOT", x = -230000.0, y = 60000.0, priority = 2 } ]

[links]
drop = [["town_a", "town_b"]]
add = [["ab_khalkhalah", "town_shahba"]]
```

Ids come from names (`ab_`, `town_`, `mil_`, `br_` + the bridge's object id) and are listed in
`map.json`. The builder warns about edits that name something that isn't there.

## Not done yet

- Rail links, sea links, and ports, power plants, factories and refineries (they're in the model,
  but nothing finds them yet).
- Helipads as FARP sites.
- Rail stations and tunnels (in the survey: `RAILSTATION`, `TUNNEL_RAIL_*`), ports, radio and TV
  masts, and oil fields (the `HOil` helipads mark them).
