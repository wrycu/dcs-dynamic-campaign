"""Stock aircraft loadouts (data/payloads.json, made by tools/extract_payloads.py from the install)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

PAYLOADS_JSON = Path(__file__).resolve().parent.parent / "data" / "payloads.json"

# Countermeasures loaded with every payload; DCS doesn't put them in UnitPayloads files.
COUNTERMEASURES: dict[str, tuple[int, int]] = {  # type -> (chaff, flare)
    "F-16C_50": (60, 60), "FA-18C_hornet": (60, 60), "F-15C": (120, 60),
    "MiG-29A": (30, 30), "MiG-29S": (30, 30), "Su-27": (96, 96), "MiG-21Bis": (18, 32),
}
DEFAULT_COUNTERMEASURES = (30, 30)


class UnknownPayload(KeyError):
    pass


@lru_cache(maxsize=1)
def _catalog(path: Path = PAYLOADS_JSON) -> dict[str, dict[str, dict]]:
    return json.loads(path.read_text())


def loadouts(aircraft_type: str) -> list[str]:
    return sorted(_catalog().get(aircraft_type, {}))


def dcs_payload(aircraft_type: str, name: str | None, fuel: float = 1.0) -> dict:
    """The DCS ``payload`` table for a unit: pylons from the named stock loadout ("" or None = clean).

    ``fuel_fraction`` is resolved to kilograms by the mission bridge, which knows each type's tank size.
    """
    pylons: dict[str, dict] = {}
    if name:
        entry = _catalog().get(aircraft_type, {}).get(name)
        if entry is None:
            raise UnknownPayload(f"no loadout {name!r} for {aircraft_type}; known: {loadouts(aircraft_type)[:8]}...")
        pylons = {num: {"CLSID": clsid} for num, clsid in entry["pylons"].items()}
    chaff, flare = COUNTERMEASURES.get(aircraft_type, DEFAULT_COUNTERMEASURES)
    return {"pylons": pylons, "fuel_fraction": max(0.0, min(1.0, fuel)), "chaff": chaff, "flare": flare, "gun": 100}
