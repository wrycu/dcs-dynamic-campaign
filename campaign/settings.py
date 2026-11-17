"""Operator settings, read from a TOML file such as ``config/campaign.toml``.

Distances are in nautical miles and times in seconds, the units a server operator thinks in;
they are converted to the engine's metres here. Unknown keys are an error, so a typo doesn't
silently fall back to a default.
"""
from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

from .bubble import NM, BubbleConfig

# TOML key in [bubble] -> (BubbleConfig field, multiplier to engine units)
BUBBLE_KEYS: dict[str, tuple[str, float]] = {
    "ground_spawn_nm": ("ground_spawn", NM),
    "ground_despawn_nm": ("ground_despawn", NM),
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


def load_bubble_config(path: Path, base: BubbleConfig | None = None) -> BubbleConfig:
    try:
        with path.open("rb") as f:
            doc = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(f"{path}: {e}") from e
    return bubble_config(doc.get("bubble", {}), base)
