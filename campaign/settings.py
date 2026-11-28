"""Operator settings, read from a TOML file such as ``config/campaign.toml``.

Distances are in nautical miles and times in seconds, the units a server operator thinks in;
they are converted to the engine's metres here. Unknown keys are an error, so a typo doesn't
silently fall back to a default.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .bubble import NM, BubbleConfig

# TOML key in [bubble] -> (BubbleConfig field, multiplier to engine units)
BUBBLE_KEYS: dict[str, tuple[str, float]] = {
    "ground_spawn_nm": ("ground_spawn", NM),
    "ground_despawn_nm": ("ground_despawn", NM),
    "garrison_spawn_nm": ("garrison_spawn", NM),
    "garrison_despawn_nm": ("garrison_despawn", NM),
    "sam_spawn_margin_nm": ("threat_spawn_margin", NM),
    "sam_despawn_margin_nm": ("threat_despawn_margin", NM),
    "min_dwell_s": ("min_dwell", 1.0),
    "max_units": ("max_units", 1),
    "max_spawns_per_tick": ("max_spawns_per_tick", 1),
    "lookahead_s": ("lookahead_s", 1.0),
    "air_spawn_nm": ("air_spawn", NM),
    "air_despawn_nm": ("air_despawn", NM),
    "air_min_dwell_s": ("air_min_dwell", 1.0),
    "max_flights": ("max_flights", 1),
}


class SettingsError(ValueError):
    pass


def bubble_config(section: dict[str, Any], base: BubbleConfig | None = None) -> BubbleConfig:
    """Apply a ``[bubble]`` table (nm/seconds) on top of ``base`` (default: BubbleConfig())."""
    unknown = set(section) - set(BUBBLE_KEYS)
    if unknown:
        raise SettingsError(f"unknown [bubble] settings: {', '.join(sorted(unknown))}; "
                            f"known: {', '.join(BUBBLE_KEYS)}")
    changes: dict[str, Any] = {}
    for key, value in section.items():
        field_name, scale = BUBBLE_KEYS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"[bubble] {key} must be a number, got {value!r}")
        changes[field_name] = int(value) if scale == 1 else float(value) * scale
    try:
        return replace(base or BubbleConfig(), **changes)
    except ValueError as e:
        raise SettingsError(f"[bubble]: {e}") from e


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(f"{path}: {e}") from e


def load_bubble_config(path: Path, base: BubbleConfig | None = None) -> BubbleConfig:
    return bubble_config(_read(path).get("bubble", {}), base)


# ---------- [server] ----------

PLAYER_STARTS = ("hot", "cold")


@dataclass
class ServerConfig:
    """How the campaign server runs (campaign/server.py)."""
    host: str = "127.0.0.1"         # the bridge listens here; DCS runs on the same machine
    port: int = 7341
    save: str = "build/saves/campaign.json"
    autosave_s: float = 30.0        # real seconds between saves while something changes
    status_s: float = 60.0          # real seconds between status lines on the console
    slot_sides: list[int] = field(default_factory=lambda: [0, 2])   # spectators and blue (PvE)
    player_start: str = "hot"       # players' flights start on the ramp "hot" (engines running) or "cold"
    map_refresh_s: float = 30.0     # real seconds between war map refreshes (players with the client script)
    # The session mission (campaign/session.py), built when a flight lead commits: from this
    # template .miz (it needs a player aircraft to copy), written to this folder (where DCS loads it
    # from). "": the test missions' a2a.miz, and Saved Games/DCS/Missions/campaign-dev.
    mission_template: str = ""
    missions_dir: str = ""
    # Fly as a ride in a Humvee to the jets (campaign/rides.py), for players whose client script has
    # it (version 24); at this speed.
    ride_to_jet: bool = True
    ride_kmh: float = 20.0

    def __post_init__(self) -> None:
        if not 0 < self.port < 65536:
            raise ValueError("port must be 1-65535")
        if self.autosave_s <= 0 or self.status_s <= 0:
            raise ValueError("autosave_s and status_s must be positive")
        if self.map_refresh_s < 5:
            raise ValueError("map_refresh_s must be at least 5")
        if not self.slot_sides or any(s not in (0, 1, 2) for s in self.slot_sides):
            raise ValueError("slot_sides must list sides 0 (spectators), 1 (red) or 2 (blue)")
        if not 5 <= self.ride_kmh <= 80:
            raise ValueError("ride_kmh must be 5-80")
        if self.player_start not in PLAYER_STARTS:
            raise ValueError(f"player_start must be one of {', '.join(PLAYER_STARTS)}")


SERVER_TYPES: dict[str, type | tuple[type, ...]] = {
    "host": str, "port": int, "save": str, "autosave_s": (int, float), "status_s": (int, float), "slot_sides": list,
    "player_start": str, "map_refresh_s": (int, float), "mission_template": str, "missions_dir": str,
    "ride_to_jet": bool, "ride_kmh": (int, float),
}


def server_config(section: dict[str, Any], base: ServerConfig | None = None) -> ServerConfig:
    """Apply a ``[server]`` table on top of ``base`` (default: ServerConfig())."""
    unknown = set(section) - set(SERVER_TYPES)
    if unknown:
        raise SettingsError(f"unknown [server] settings: {', '.join(sorted(unknown))}; "
                            f"known: {', '.join(SERVER_TYPES)}")
    for key, value in section.items():
        want = SERVER_TYPES[key]
        if (isinstance(value, bool) and want is not bool) or not isinstance(value, want):
            raise SettingsError(f"[server] {key} has the wrong type: {value!r}")
    try:
        return replace(base or ServerConfig(), **section)
    except ValueError as e:
        raise SettingsError(f"[server]: {e}") from e


@dataclass
class Settings:
    server: ServerConfig
    bubble: BubbleConfig


def load_settings(path: Path | None) -> Settings:
    """The whole settings file; without one, the defaults. Unknown sections are an error too."""
    doc = _read(path) if path else {}
    unknown = set(doc) - {"server", "bubble"}
    if unknown:
        raise SettingsError(f"unknown sections: {', '.join(sorted(unknown))}; known: server, bubble")
    return Settings(server=server_config(doc.get("server", {})), bubble=bubble_config(doc.get("bubble", {})))
