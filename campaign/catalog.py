"""Static facts about DCS unit types that the engine needs and DCS can't tell it cheaply.

Ranges are approximate maximum engagement ranges against aircraft, in metres. They only need
to be good enough to decide how early a site must exist in DCS before a player reaches it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .model import Side

if TYPE_CHECKING:
    from .model import CampaignState, Flight

KT = 1852 / 3600   # m/s per knot

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


# Cruise true airspeed by DCS aircraft type, in knots: what the engine flies a flight at when it
# isn't in DCS, and what DCS routes ask for. Typical medium-altitude cruise, not maximum speed.
CRUISE_KT: dict[str, float] = {
    # fighters and attack
    "FA-18C_hornet": 450, "F-16C_50": 480, "F-15C": 480, "F-15ESE": 480, "F-14A-135-GR": 450, "F-14B": 450,
    "F-4E-45MC": 450, "F-5E-3": 420, "M-2000C": 470, "Mirage-F1CE": 450, "Mirage-F1EE": 450, "JF-17": 450,
    "AV8BNA": 420, "A-10C": 300, "A-10C_2": 300, "A-4E-C": 400,
    "MiG-29A": 480, "MiG-29S": 480, "MiG-29G": 480, "MiG-31": 500, "MiG-25PD": 500, "MiG-23MLD": 480,
    "MiG-21Bis": 450, "Su-27": 480, "Su-33": 480, "J-11A": 480, "Su-30": 480, "Su-34": 460, "Su-24M": 450,
    "Su-25": 380, "Su-25T": 380, "Su-17M4": 430, "L-39ZA": 300, "Tornado IDS": 450,
    # bombers
    "B-52H": 450, "B-1B": 480, "Tu-22M3": 470, "Tu-95MS": 400, "Tu-160": 480,
    # support
    "KC-135": 450, "KC135MPRS": 450, "KC130": 290, "IL-78M": 420, "S-3B Tanker": 350,
    "E-3A": 360, "A-50": 400, "E-2C": 290, "KJ-2000": 400,
    "C-130": 290, "C-17A": 450, "IL-76MD": 430, "An-26B": 230, "An-30M": 230, "Yak-40": 300,
    # drones and helicopters
    "MQ-9 Reaper": 170, "MQ-1A Predator": 70,
    "AH-64D_BLK_II": 120, "Ka-50": 140, "Ka-50_3": 140, "Mi-24P": 140, "Mi-8MT": 120, "UH-1H": 100,
    "SA342M": 120, "CH-47Fbl1": 140, "UH-60A": 130,
}
DEFAULT_CRUISE_KT = 430


def cruise_ms(aircraft_type: str) -> float:
    """Cruise true airspeed for a DCS aircraft type, m/s (DEFAULT_CRUISE_KT if not listed)."""
    return CRUISE_KT.get(aircraft_type, DEFAULT_CRUISE_KT) * KT


def flight_cruise_ms(state: CampaignState, flight: Flight) -> float:
    """A flight's cruise speed: its type's, or, in a package, the slowest of the package's flights,
    so the package stays together and meets its times on target."""
    def own(f: Flight) -> float:
        sq = state.squadrons.get(f.squadron_id)
        return cruise_ms(sq.aircraft_type) if sq else DEFAULT_CRUISE_KT * KT

    pkg = state.packages.get(flight.package_id)
    members = [state.flights[i] for i in pkg.flight_ids if i in state.flights] if pkg else []
    return min([own(f) for f in members] + [own(flight)])


def threat_range(composition: dict[str, int]) -> float:
    """Largest air-defence engagement range among the types present (0 if none)."""
    return max((THREAT_RANGE_M.get(t, 0.0) for t, n in composition.items() if n > 0), default=0.0)
