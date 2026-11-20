"""Build the DCS test missions from a template .miz.

    python3 -m tools.build_test_missions [--template PATH] [--out build]

Produces:
  build/campaign-probe.miz   runs lua/devtools/probe.lua            (API questions; no server needed)
  build/campaign-smoke.miz   runs lua/mission/campaign_bridge.lua   (pair with tools/smoke_server.py)

Each mission has one MISSION START "DO SCRIPT" that loadfile()s the script straight from this repo
through Wine's Z: drive, so editing the Lua only needs a mission restart, not a rebuild.

The template's player aircraft is moved over the probe airbase, and that airbase's warehouse is
made BLUE with limited aircraft (munitions stay unlimited). Requires luajit.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PATCHER = ROOT / "tools" / "miz" / "patch_mission.lua"
PREFIX = Path.home() / "Games" / "dcs-world"
SAVED_GAMES = PREFIX / "drive_c" / "users" / "tim" / "Saved Games" / "DCS"  # tim -> steamuser symlink in this prefix
DEFAULT_TEMPLATE = SAVED_GAMES / "Missions" / "a2a.miz"

# Syria / Khalkhalah: airdrome id 18; its parking area, and the tower's scenery id from
# Mods/terrains/Syria/radio.lua (sceneObjects = {'t:27320578'}).
AIRDROME_ID = 18
AIRBASE_NAME = "Khalkhalah"
# Airbase.getPoint() as DCS reports it (2.9.31); the demo places formations and cartridge waypoints from it.
AIRBASE_XY = (-216085.3, 52697.5)
TOWER_SCENERY_ID = "27320578"
# Dynamic slots for proof P2 (airdrome ids from Mods/terrains/Syria/radio.lua, radioId 'airfield<id>_0').
DYNAMIC_AIRDROMES = {"Khalkhalah": 18, "Tiyas": 39, "Marj Ruhayyil": 23}
# A Client Hornet parked cold at Khalkhalah (proof P19: force_player_slot + the client script's spawn
# on a ramp start). DCS picks the parking spot when the player spawns.
PARKED_CLIENT = {"name": "Ramp", "airdrome_id": AIRDROME_ID, "x": AIRBASE_XY[0], "y": AIRBASE_XY[1]}
# Combined Arms (P19 part 4): blue units a player can take over (RAlt+J), next to where Ramp parked
# in Run 14d. "Walker" is an armed Humvee: driving it to a jet works (Run 14h). The manual says
# infantry can't be occupied (Run 14g: "wrong vehicle type" for Soldier M4), but Combined Arms ships
# infantry sights, so a row of soldier types (Inf-1 ... Inf-N, 10 m apart, east of the Humvee) checks
# whether any of them can be.
INFANTRY_TYPES = ["Soldier M4", "Soldier M249", "Soldier M4 GRG", "JTAC", "Soldier stinger", "Soldier RPG",
                  "Infantry AK", "Infantry AK ver3", "Paratrooper AKS-74", "soldier_wwii_br_01"]
WALKER_XY = (-216030.0, 52800.0)
COMBINED_ARMS = {"slots": 2, "units": [
    {"name": "Walker", "type": "M1043 HMMWV Armament", "x": WALKER_XY[0], "y": WALKER_XY[1]},
] + [
    {"name": f"Inf-{i} {t}", "type": t, "x": WALKER_XY[0] - 15.0, "y": WALKER_XY[1] + 10.0 * i}
    for i, t in enumerate(INFANTRY_TYPES, start=1)
]}


def lua_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def lua_value(v: Any) -> str:
    """A Python value as a Lua literal: dicts become tables with ["key"] = value."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return lua_str(v)
    if isinstance(v, dict):
        return "{ " + ", ".join(f"[{lua_value(k)}] = {lua_value(x)}" for k, x in v.items()) + " }"
    if isinstance(v, (list, tuple)):
        return "{ " + ", ".join(lua_value(x) for x in v) + " }"
    raise TypeError(f"cannot write {type(v).__name__} as Lua")


def wine_roots(repo_path: str | None = None) -> list[str]:
    """Candidate Z:\\ paths for the repo; Fedora Atomic has /home -> /var/home.

    ``repo_path`` is where the repo is on the machine that runs DCS, if that isn't this one."""
    if repo_path:
        if len(repo_path) > 1 and repo_path[1] == ":":  # already a Windows path, e.g. C:\dcs-dynamic-campaign
            return [repo_path.rstrip("\\") + "\\"]
        return ["Z:" + repo_path.rstrip("/").replace("/", "\\") + "\\"]
    paths = {str(ROOT.resolve()), str(ROOT)}
    home, real_home = os.path.expanduser("~"), os.path.realpath(os.path.expanduser("~"))
    for p in list(paths):
        if p.startswith(real_home):
            paths.add(home + p[len(real_home):])
    return sorted("Z:" + p.replace("/", "\\") + "\\" for p in paths)


def loader(rel_path: str, preamble: str = "", repo_path: str | None = None) -> str:
    roots = ", ".join(f"[[{r}]]" for r in wine_roots(repo_path))
    rel = rel_path.replace("/", "\\")
    return f"""{preamble}local rel = [[{rel}]]
local roots = {{ {roots} }}
for _, root in ipairs(roots) do
  local chunk, err = loadfile(root .. rel)
  if chunk then
    env.info("[loader] running " .. root .. rel)
    chunk()
    return
  end
  env.info("[loader] " .. tostring(err))
end
env.error("[loader] could not load " .. rel .. " from the repo")
trigger.action.outText("LOADER FAILED: could not load " .. rel .. " (see dcs.log)", 120)
"""


def build(template: Path, out: Path, name: str, rel_script: str, preamble: str, comment: str,
          repo_path: str | None = None, clients: bool = False, dynamic: list[int] | None = None,
          extra: dict[str, Any] | None = None, files: dict[str, bytes] | None = None) -> Path:
    """``extra``: more patcher params, e.g. ``date``, ``start_time``, ``weather`` (see patch_mission.lua).
    ``files``: more files to put in the .miz, by path inside it (e.g. kneeboard pages)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        with zipfile.ZipFile(template) as z:
            z.extract("mission", tmpd / "in")
            z.extract("warehouses", tmpd / "in")
            entries = [(i, z.read(i)) for i in z.infolist() if i.filename not in ("mission", "warehouses")]
        params = tmpd / "params.lua"
        params.write_text("return {\n" + ",\n".join([
            f"mission_in = {lua_str(str(tmpd / 'in' / 'mission'))}",
            f"mission_out = {lua_str(str(tmpd / 'mission'))}",
            f"warehouses_in = {lua_str(str(tmpd / 'in' / 'warehouses'))}",
            f"warehouses_out = {lua_str(str(tmpd / 'warehouses'))}",
            f"loader = {lua_str(loader(rel_script, preamble, repo_path))}",
            f"comment = {lua_str(comment)}",
            f"airdrome_id = {AIRDROME_ID}",
            f"player_x = {AIRBASE_XY[0] - 8000}",  # 8 km south of the airbase
            f"player_y = {AIRBASE_XY[1]}",
            "player_alt = 3000",
            f"clients = {'true' if clients else 'false'}",
            f"dynamic = {{ {', '.join(str(i) for i in dynamic or [])} }}",
        ] + [f"{k} = {lua_value(v)}" for k, v in (extra or {}).items()]) + "\n}\n")
        r = subprocess.run(["luajit", str(PATCHER), str(params)], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"patch failed:\n{r.stdout}{r.stderr}")
        print(f"{name}: {r.stdout.strip()}")
        for f in ("mission", "warehouses"):  # sanity: output must load as Lua
            chk = subprocess.run(["luajit", "-bl", str(tmpd / f)], capture_output=True, text=True)
            if chk.returncode != 0:
                raise SystemExit(f"{f} output is not valid Lua: {chk.stderr}")

        out.mkdir(parents=True, exist_ok=True)
        dest = out / name
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmpd / "mission", "mission")
            z.write(tmpd / "warehouses", "warehouses")
            for info, data in entries:
                if info.filename not in (files or {}):
                    z.writestr(info, data)
            for name, data in (files or {}).items():
                z.writestr(name, data)
    return dest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    ap.add_argument("--out", type=Path, default=ROOT / "build")
    ap.add_argument("--server", metavar="REPO_PATH",
                    help="instead, build campaign-server.miz and campaign-slots.miz (dynamic slots, proof P2) "
                         "for a dedicated server: the bridge is loaded from "
                         "the repo checkout at REPO_PATH on the server (a Linux path, read through Wine's Z: drive, "
                         "or a Windows path such as C:\\dcs-dynamic-campaign), and player aircraft are Client slots")
    args = ap.parse_args()
    if not shutil.which("luajit"):
        raise SystemExit("luajit is required")
    if args.server:
        server = build(args.template, args.out, "campaign-server.miz", "lua/mission/campaign_bridge.lua", "",
                       "Load campaign bridge", args.server, clients=True)
        slots = build(args.template, args.out, "campaign-slots.miz", "lua/mission/campaign_bridge.lua", "",
                      "Load campaign bridge", args.server, clients=True, dynamic=list(DYNAMIC_AIRDROMES.values()),
                      extra={"parked_client": PARKED_CLIENT, "combined_arms": COMBINED_ARMS})
        print(f"wrote {server}\nwrote {slots}\nrepo roots tried by the loader: {wine_roots(args.server)}")
        return
    probe = build(args.template, args.out, "campaign-probe.miz", "lua/devtools/probe.lua",
                  f'ProbeConfig = {{ airbase = "{AIRBASE_NAME}", known_scenery_id = "{TOWER_SCENERY_ID}" }}\n',
                  "Load campaign API probe")
    smoke = build(args.template, args.out, "campaign-smoke.miz", "lua/mission/campaign_bridge.lua", "",
                  "Load campaign bridge")
    print(f"wrote {probe}\nwrote {smoke}\nrepo roots tried by the loader: {wine_roots()}")


if __name__ == "__main__":
    main()
