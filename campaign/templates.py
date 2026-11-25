"""Ground formation templates: what a "battalion" of each kind is made of, in DCS unit types.

A scenario names templates for its formations (``data/scenarios/<name>.toml``); a formation's
composition is copied from its template, then tracked on its own (losses, replacements).
Every type name here exists in DCS 2.9 (checked against the install, 2026-11-24).
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import GroundRole


@dataclass(frozen=True)
class Template:
    role: GroundRole
    composition: dict[str, int]
    label: str


TEMPLATES: dict[str, Template] = {
    # Red: Syrian Arab Army (SAA) and Syrian Air Defence Force (SyADF), Soviet-era equipment.
    "red_tank_bn": Template(GroundRole.ARMOR, {"T-72B": 10, "BMP-2": 4, "ZSU-23-4 Shilka": 1, "Ural-375": 3},
                            "tank battalion (T-72)"),
    "red_old_tank_bn": Template(GroundRole.ARMOR, {"T-55": 10, "BMP-1": 4, "Ural-375": 3}, "tank battalion (T-55)"),
    "red_mech_bn": Template(GroundRole.MECH, {"BMP-1": 10, "BTR-80": 4, "Strela-10M3": 1, "Ural-375": 3},
                            "mechanised battalion"),
    "red_art_bn": Template(GroundRole.ARTILLERY, {"SAU Gvozdika": 6, "Grad-URAL": 4, "Ural-375": 3},
                           "artillery battalion"),
    "red_sa2": Template(GroundRole.SAM, {"S_75M_Volhov": 6, "SNR_75V": 1, "p-19 s-125 sr": 1}, "SA-2 battery"),
    "red_sa3": Template(GroundRole.SAM, {"5p73 s-125 ln": 4, "snr s-125 tr": 1, "p-19 s-125 sr": 1}, "SA-3 battery"),
    "red_sa6": Template(GroundRole.SAM, {"Kub 2P25 ln": 4, "Kub 1S91 str": 1}, "SA-6 battery"),
    "red_sa11": Template(GroundRole.SAM, {"SA-11 Buk LN 9A310M1": 4, "SA-11 Buk SR 9S18M1": 1,
                                         "SA-11 Buk CC 9S470M1": 1}, "SA-11 battery"),
    "red_sa8": Template(GroundRole.SAM, {"Osa 9A33 ln": 3}, "SA-8 platoon"),
    "red_aaa": Template(GroundRole.AAA, {"ZSU-23-4 Shilka": 2, "Strela-10M3": 1}, "air defence platoon"),
    "red_ewr": Template(GroundRole.SAM, {"1L13 EWR": 1}, "early warning radar"),
    # Blue: coalition ground forces.
    "blue_tank_bn": Template(GroundRole.ARMOR, {"M-1 Abrams": 10, "M-2 Bradley": 4, "M1097 Avenger": 1},
                             "armoured battalion (M1)"),
    "idf_tank_bn": Template(GroundRole.ARMOR, {"Merkava_Mk4": 10, "M-2 Bradley": 4, "M1097 Avenger": 1},
                            "armoured battalion (Merkava)"),
    "blue_mech_bn": Template(GroundRole.MECH, {"M-2 Bradley": 10, "M1043 HMMWV Armament": 4, "M1097 Avenger": 1},
                             "mechanised battalion (Bradley)"),
    "blue_art_bn": Template(GroundRole.ARTILLERY, {"M-109": 6, "M1043 HMMWV Armament": 2}, "artillery battalion"),
    "blue_hawk": Template(GroundRole.SAM, {"Hawk ln": 3, "Hawk sr": 1, "Hawk tr": 1, "Hawk pcp": 1}, "Hawk battery"),
    "blue_avenger": Template(GroundRole.AAA, {"M1097 Avenger": 2}, "Avenger section"),
}
