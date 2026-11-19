# Abstract combat

How fights are resolved in the engine when no player is near (items 8 and 17, proof P7). The model is in `campaign/combat.py`. Two tools test it without DCS:
- `tools/combat_tables.py` runs standard fights many times, next to real-world figures.
- `tools/war_sim.py` runs a whole war on the P6 map layer.

A fixed seed always gives the same result.

## The model

**Ground fights** are fought hour by hour, Lanchester style.
- **Fire:** each side's fire is its combat power, spread over the enemy's vehicles. A vehicle survives the hits it gets according to its toughness.
  - Combat power is the sum of the type values, scaled by supply (×0.5–1) and morale (×0.4–1).
  - Examples (power / toughness): Abrams 15 / 8, T-72B 5 / 2, T-55 2.5 / 1.2, BMP-1 1.8 / 0.9.
- **Terrain:** defenders get more fire and more cover. A town gives ×1.5 on each, an airbase ×1.3, open ground ×1.25 / ×1.2.
- **Artillery** next to the fight adds 70% of its fire and takes none.
- **Breaking:** a formation breaks below 60% strength or 35% morale. It loses morale at 1.5× the share of its vehicles lost each hour.
- **Time limit:** an attack that hasn't won in 12 hours is called off.

**Air fights** are resolved per mission.
- **Air to air:** a BVR pass, then a merge. Each aircraft shoots once per pass, with a chance that depends on the quality difference.
  - Quality examples: F-16C 9, MiG-29A 6, MiG-23 4. AWACS adds +1.
  - Aircraft below quality 6 don't shoot BVR. Strikers below 3 never shoot.
- **SAMs:** each aircraft has a chance to be shot down for each site whose range it enters.
  - The base chance per site: SA-2 2%, SA-3 2.5%, SA-6 and SA-8 3%, SA-11 5%, SA-10 7%.
  - The chance is lower with SEAD in the package (×0.4), with ECM (F-16/F/A-18 ×0.7), and when the site is only passed rather than attacked (×0.5).
  - A formation's own guns and short-range missiles add a small chance (0.2–0.5% per launcher).
- **SEAD:** each aircraft with anti-radiation missiles has a 30–35% chance to knock out the site's radar, which is then down for 36 hours.
- **CAS:** each aircraft that reaches the target kills vehicles at a per-type rate: A-10C 1.0, F-16C and F/A-18C 0.6, Su-22 0.5. The rate is halved against a formation in a town. Tanks are harder to kill than light vehicles.

All of these numbers are first guesses tuned against the references below. They should become scenario settings once the model settles.

## P7 results (2026-11-19, offline)

### Standard fights (`python3 -m tools.combat_tables`, 1,000 runs each)

| Fight | Attacker wins | Losses: attacker / defender | Reference |
|---|---|---|---|
| Abrams bn attacks T-72 bn in a town | 100% | 11% / 46% | 1991: US armour beat T-72s with few losses |
| 2 blue bns (tank + mech) attack T-72 bn in a town | 100% | 6% / 49% | a deliberate attack at good odds |
| Bradley mech bn attacks T-72 bn + artillery in a town | 66% | 34% / 40% | a hard fight for one battalion |
| T-72 bn counterattacks Abrams bn in the open | 0% | 50% / 4% | 1991 Medina Ridge |
| 2 T-72 bns attack 1 T-72 bn in a town | 91% | 21% / 44% | 2:1 against a town: costly; see below |
| T-55 bn attacks a BMP mech bn in the open | 3% | 41% / 21% | attacking at 1:1 fails |

| Air fight | Losses A / B | Reference |
|---|---|---|
| 4 F-16C (AWACS) vs 4 MiG-29A | 0.47 / 3.1 | 1991 Gulf ~33:1, 1982 Bekaa ~80:0 |
| 4 F-16C (AWACS) vs 4 MiG-23MLD | 0.05 / 3.3 | |
| 2 F/A-18C (no AWACS) vs 4 MiG-29A | 0.72 / 1.7 | |
| 4 MiG-29A vs 4 MiG-23MLD | 0.28 / 2.8 | |

| Attack | Loss per sortie | Vehicles killed per 4-ship | Reference |
|---|---|---|---|
| A-10C on a T-72 bn, no SAMs | 0.3% | 3.9 | 1991: 1–2 claimed per A-10 sortie, fewer confirmed; 0.05% losses overall |
| F-16C on a BMP bn under an SA-6, no SEAD | 3.1% | 2.4 | 1973: Israel lost 1–2% per sortie overall, much more in SAM belts |
| Same, with 2 F-16C on SEAD | 0.8% | 2.4 | |
| A-10C under an SA-6 and an SA-8, no SEAD | 8.2% | 3.5 | the worst case |

### A war (`python3 -m tools.war_sim`)

The war runs on the P6 map around Khalkhalah: 67 objectives and 133 links, 40 of which have DCS road lengths. Blue has 6 battalions and 48 aircraft. Red has 12 battalions, 2 more arriving on day 4, 52 aircraft, and 7 SAM sites. Each objective also has a local garrison. 14 days take under a second.

| Red forces | Damascus falls (8 seeds) | Blue ground losses | Red ground losses | Blue aircraft lost |
|---|---|---|---|---|
| ×1 | day 3–6 in all 8 | 15–27% | 100% | 3–10 (0.5–1.2% per sortie) |
| ×2 (35 days) | day 5 in 1 of 6; blue culminates in the rest | 30–47% | 87–100% | 7–15 |
| ×3 (35 days) | never; blue stalls after ~1 objective | 33–56% | 65–93% | 9–25 |

Red's air force is gone by day 4 in every run, losing about 9 aircraft for each one it shoots down.

## Verdict

**The fight model is believable.**
- Quality, numbers, terrain, artillery, SEAD and ECM all push results the right way, by about the right amount.
- The force balance works as a scenario setting, from a quick win through a contested war to a stall.
- It's fast enough to run weeks of war in seconds (item 17).

**What it showed needs work elsewhere:**
1. **The ground commander (item 8).**
   - The simple commanders push spearheads into pockets and leave the rear empty. In one run, red retook Khalkhalah behind a blue column in Damascus.
   - Next to a strong defence, blue won't attack, and it only gathers for one target at a time.
   - A real commander needs to know where the front line is, and to secure the rear and the supply routes.
2. **Red's air commander.** Red keeps sending 4-ship CAPs into blue's fighters until it has none left. A minor country would keep its air force back, as Iraq did in 1991, and fly when the odds are better.
3. **Replacements and repairs (item 9).** The war sim repairs 40% of ground losses (25% after air attack) in 48 hours, but adds no new vehicles or aircraft, so long wars run down.
4. **Even-quality 2:1 against a town wins 91% of the time.** The 3:1 rule of thumb suggests about 50%. A town could give defenders a lower breaking point.
5. **Blue air losses run 0.5–1.2% per sortie.** That's closer to 1973 or Vietnam than to 1991 (0.05%), mostly air to air in the first three days. That may suit a game, where losses should matter, but it is a balance choice.
6. **Map layer: only 40 of 133 links have DCS road lengths.** The others are estimated at 1.4× the straight line. Building the map layer should ask DCS for every link.
