"""Extract the stock aircraft loadouts from a DCS install into data/payloads.json.

    python3 -m tools.extract_payloads [--dcs PATH] [--out data/payloads.json]

Reads (never writes) MissionEditor/data/scripts/UnitPayloads/*.lua and
{CoreMods,Mods}/aircraft/*/UnitPayloads/*.lua; module files override the generic ones.
The result maps DCS unit type -> loadout name -> {"pylons": {num: CLSID}, "tasks": [task ids]}.
Requires luajit. Re-run after a DCS update that changes loadouts.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DUMPER = ROOT / "tools" / "miz" / "dump_payloads.lua"
DEFAULT_DCS = Path.home() / "Games" / "dcs-world" / "drive_c" / "Program Files" / "Eagle Dynamics" / "DCS World"


def payload_files(dcs: Path) -> list[Path]:
    files = sorted((dcs / "MissionEditor" / "data" / "scripts" / "UnitPayloads").glob("*.lua"))
    for mods in ("CoreMods", "Mods"):
        files += sorted((dcs / mods / "aircraft").glob("*/UnitPayloads/*.lua"))
    return files


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dcs", type=Path, default=DEFAULT_DCS)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "payloads.json")
    args = ap.parse_args()
    if not shutil.which("luajit"):
        raise SystemExit("luajit is required")
    files = payload_files(args.dcs)
    if not files:
        raise SystemExit(f"no UnitPayloads files under {args.dcs}")
    r = subprocess.run(["luajit", str(DUMPER), *map(str, files)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"dump failed:\n{r.stderr}")
    data = json.loads(r.stdout)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    n = sum(len(v) for v in data.values())
    print(f"wrote {args.out}: {len(data)} aircraft types, {n} loadouts from {len(files)} files")
    skipped = [line for line in r.stderr.splitlines() if line.startswith("skip")]
    if skipped:
        print(f"{len(skipped)} file(s) skipped, e.g. {skipped[0]}")


if __name__ == "__main__":
    main()
