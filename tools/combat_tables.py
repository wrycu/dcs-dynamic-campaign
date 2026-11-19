"""Proof P7, part 1: standard fights from campaign/combat.py, run many times, next to real-world
figures to judge them against.

    python3 -m tools.combat_tables [--runs 1000] [--seed 1]
"""
from __future__ import annotations

import argparse
import random
import statistics
from dataclasses import dataclass
from typing import Callable

from campaign import combat
from campaign.combat import AirSide, Fighter

TANK_BLUE = {"M-1 Abrams": 30, "M1097 Avenger": 2}
MECH_BLUE = {"M-2 Bradley": 30, "M-1 Abrams": 14, "M1097 Avenger": 2}
TANK_RED = {"T-72B": 31, "ZSU-23-4 Shilka": 2}
OLD_TANK_RED = {"T-55": 31, "ZSU-23-4 Shilka": 2}
MECH_RED = {"BMP-1": 30, "T-55": 10, "ZSU-23-4 Shilka": 2, "Strela-1 9P31": 2}
ARTY_RED = {"SAU Gvozdika": 18}


def fighter(fid: str, comp: dict[str, int], **kw) -> Fighter:
    return Fighter(fid, dict(comp), combat.vehicles(comp), **kw)


@dataclass
class Row:
    name: str
    reference: str
    run: Callable[[random.Random], dict[str, float]]


def ground(attackers: list[tuple[str, dict]], defenders: list[tuple[str, dict]], terrain: str,
           support_def: list[tuple[str, dict]] = ()) -> Callable[[random.Random], dict[str, float]]:
    def run(rng: random.Random) -> dict[str, float]:
        a = [fighter(f"a{i}", c) for i, (_, c) in enumerate(attackers)]
        d = [fighter(f"d{i}", c) for i, (_, c) in enumerate(defenders)]
        d += [fighter(f"s{i}", c, support=True) for i, (_, c) in enumerate(support_def)]
        a_full = sum(f.full for f in a)
        d_full = sum(f.full for f in d if not f.support)
        r = combat.ground_battle(a, d, terrain, rng)
        a_lost = sum(r.losses.get(f.id, 0) for f in a)
        d_lost = sum(r.losses.get(f.id, 0) for f in d if not f.support)
        return {"attacker wins": float(r.winner == "attacker"), "hours": r.hours,
                "attacker lost %": 100 * a_lost / a_full, "defender lost %": 100 * d_lost / d_full}
    return run


def air(a: list[str], b: list[str], awacs_a: bool, awacs_b: bool) -> Callable[[random.Random], dict[str, float]]:
    def run(rng: random.Random) -> dict[str, float]:
        la, lb = combat.air_battle(AirSide(a, awacs_a), AirSide(b, awacs_b), rng)
        return {"A lost": len(la), "B lost": len(lb)}
    return run


def strike(aircraft: list[str], sites: list[tuple[str, bool]], sead: list[str], target: dict[str, int]
           ) -> Callable[[random.Random], dict[str, float]]:
    def run(rng: random.Random) -> dict[str, float]:
        suppressed = bool(sead) and combat.sead_kills(sead, rng)
        exposures = [s for s in sites] if not suppressed else sites[1:]
        lost = set(combat.sam_losses(aircraft, exposures, bool(sead), rng))
        alive = [t for i, t in enumerate(aircraft) if i not in lost]
        lost |= {i for i in combat.shorad_losses(alive, target, rng)}
        reached = [t for i, t in enumerate(aircraft) if i not in lost]
        kills = combat.cas_kills(reached, target, rng)
        return {"aircraft lost": len(lost), "loss % per sortie": 100 * len(lost) / len(aircraft),
                "vehicles killed": sum(kills.values()), "site radar down": float(suppressed)}
    return run


ROWS = [
    Row("Abrams bn attacks T-72 bn in a town", "1991: US armour beat dug-in T-72s with few losses "
        "(73 Easting: 1 Bradley lost, ~85 Iraqi vehicles); 3:1 rule of thumb for a prepared defence",
        ground([("blue", TANK_BLUE)], [("red", TANK_RED)], "town")),
    Row("Bradley mech bn attacks BMP-1 mech bn in a town", "1991-2003: US mech vs Iraqi mech, lopsided",
        ground([("blue", MECH_BLUE)], [("red", MECH_RED)], "town")),
    Row("Bradley mech bn attacks T-72 bn + 2S1 artillery in a town", "a hard fight; should often fail alone",
        ground([("blue", MECH_BLUE)], [("red", TANK_RED)], "town", [("arty", ARTY_RED)])),
    Row("2 blue bns (tank + mech) attack T-72 bn in a town", "a deliberate attack at good odds: should win",
        ground([("b1", TANK_BLUE), ("b2", MECH_BLUE)], [("red", TANK_RED)], "town")),
    Row("T-72 bn counterattacks Abrams bn in the open", "1991 (Medina Ridge): attacker shredded",
        ground([("red", TANK_RED)], [("blue", TANK_BLUE)], "open")),
    Row("2 T-72 bns attack 1 T-72 bn in a town (even quality)", "2:1 vs a town: costly, uncertain; "
        "Dupuy: attacker ~2-6%/day division-wide, a battalion in a hard fight 10-30%",
        ground([("r1", TANK_RED), ("r2", TANK_RED)], [("r3", TANK_RED)], "town")),
    Row("T-55 bn attacks BMP-1 mech bn in the open", "older red vs red: slow, bloody",
        ground([("r1", OLD_TANK_RED)], [("r2", MECH_RED)], "open")),
    Row("4 F-16C (AWACS) vs 4 MiG-29A", "1982 Bekaa ~80:0, 1991 Gulf ~33:1 (with AWACS)",
        air(["F-16C_50"] * 4, ["MiG-29A"] * 4, True, False)),
    Row("4 F-16C (AWACS) vs 4 MiG-23MLD", "more lopsided than vs MiG-29",
        air(["F-16C_50"] * 4, ["MiG-23MLD"] * 4, True, False)),
    Row("2 FA-18C (no AWACS) vs 4 MiG-29A", "outnumbered, no AWACS: should still usually win",
        air(["FA-18C_hornet"] * 2, ["MiG-29A"] * 4, False, False)),
    Row("4 MiG-29A vs 4 MiG-23MLD", "even-ish, MiG-29 better",
        air(["MiG-29A"] * 4, ["MiG-23MLD"] * 4, False, False)),
    Row("4 A-10C CAS on T-72 bn, no SAMs", "1991: A-10 ~1-2 vehicle kills per sortie claimed, "
        "fewer confirmed after the war; 0.05% per sortie losses coalition-wide",
        strike(["A-10C_2"] * 4, [], [], TANK_RED)),
    Row("4 F-16C CAS on BMP mech bn under an SA-6, no SEAD", "1973: Israel lost ~1-2%/sortie early in "
        "SAM belts; Vietnam SA-2 ~0.5-1%",
        strike(["F-16C_50"] * 4, [("SA-6", True)], [], MECH_RED)),
    Row("4 F-16C CAS on BMP mech bn under an SA-6, 2 F-16 SEAD", "SEAD should cut losses by half or more",
        strike(["F-16C_50"] * 4, [("SA-6", True)], ["F-16C_50"] * 2, MECH_RED)),
    Row("4 A-10C CAS on BMP mech bn under an SA-6 + SA-8, no SEAD", "the worst case: several % per sortie",
        strike(["A-10C_2"] * 4, [("SA-6", True), ("SA-8", True)], [], MECH_RED)),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    for row in ROWS:
        rng = random.Random(f"{args.seed}:{row.name}")
        results = [row.run(rng) for _ in range(args.runs)]
        print(f"\n{row.name}  ({args.runs} runs)")
        print(f"  reference: {row.reference}")
        for key in results[0]:
            vals = sorted(r[key] for r in results)
            mean = statistics.fmean(vals)
            lo, hi = vals[len(vals) // 10], vals[(len(vals) * 9) // 10]
            if key in ("attacker wins", "site radar down"):
                print(f"  {key:<18} {100 * mean:5.0f}%")
            else:
                print(f"  {key:<18} mean {mean:6.2f}   10-90%: {lo:.1f} - {hi:.1f}")


if __name__ == "__main__":
    main()
