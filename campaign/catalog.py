"""Static facts about DCS unit types that the engine needs and DCS can't tell it cheaply.

Ranges are approximate maximum engagement ranges against aircraft, in metres. They only need
to be good enough to decide how early a site must exist in DCS before a player reaches it.
"""
from __future__ import annotations

from .model import Side

# Air-defence engagement range by DCS unit type (launchers and self-contained systems).
THREAT_RANGE_M: dict[str, float] = {
    # long range
    "S-300PS 5P85C ln": 75_000,
    "S-300PS 5P85D ln": 75_000,
    "Patriot ln": 100_000,
    "S_75M_Volhov": 40_000,          # SA-2
    "Hawk ln": 45_000,
    # medium range
    "SA-11 Buk LN 9A310M1": 35_000,
    "Kub 2P25 ln": 22_000,           # SA-6
    "5p73 s-125 ln": 15_000,         # SA-3
    "NASAMS_LN_C": 25_000,
    "NASAMS_LN_B": 20_000,
    # short range
    "Tor 9A331": 12_000,
    "Osa 9A33 ln": 10_000,
    "2S6 Tunguska": 8_000,
    "Roland ADS": 8_000,
    "Strela-10M3": 5_000,
    "Strela-1 9P31": 4_200,
    "M1097 Avenger": 5_000,
    "M48 Chaparral": 6_000,
    "SA-18 Igla manpad": 5_000,
    "Stinger manpad": 4_500,
    # guns
    "ZSU-23-4 Shilka": 2_500,
    "ZSU_57_2": 4_000,
    "ZU-23 Emplacement": 2_500,
    "Gepard": 4_000,
    "Vulcan": 2_000,
}

# Radars and command posts that belong in the middle of a SAM site rather than on its ring.
SAM_CORE: set[str] = {
    "S-300PS 40B6M tr", "S-300PS 40B6MD sr", "S-300PS 64H6E sr", "S-300PS 54K6 cp",
    "SA-11 Buk SR 9S18M1", "SA-11 Buk CC 9S470M1",
    "Kub 1S91 str",
    "snr s-125 tr", "p-19 s-125 sr",
    "SNR_75V",
    "Patriot str", "Patriot cp", "Patriot ECS", "Patriot AMG", "Patriot EPP",
    "Hawk sr", "Hawk tr", "Hawk pcp", "Hawk cwar",
    "NASAMS_Radar_MPQ64F1", "NASAMS_Command_Post",
    "55G6 EWR", "1L13 EWR",
}

# DCS country ids used for campaign spawns. CJTF Blue/Red exist in every modern mission's
# coalition lists, so they avoid depending on which real countries the .miz assigns.
COUNTRY_FOR_SIDE: dict[Side, int] = {
    Side.BLUE: 80,  # CJTF_BLUE
    Side.RED: 81,   # CJTF_RED
}


def threat_range(composition: dict[str, int]) -> float:
    """Largest air-defence engagement range among the types present (0 if none)."""
    return max((THREAT_RANGE_M.get(t, 0.0) for t, n in composition.items() if n > 0), default=0.0)
