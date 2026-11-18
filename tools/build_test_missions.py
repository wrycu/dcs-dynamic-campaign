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


def lua_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


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
          repo_path: str | None = None, clients: bool = False) -> Path:
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
        ]) + "\n}\n")
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
                z.writestr(info, data)
    return dest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    ap.add_argument("--out", type=Path, default=ROOT / "build")
    ap.add_argument("--server", metavar="REPO_PATH",
                    help="instead, build campaign-server.miz for a dedicated server: the bridge is loaded from "
                         "the repo checkout at REPO_PATH on the server (a Linux path, read through Wine's Z: drive, "
                         "or a Windows path such as C:\\dcs-dynamic-campaign), and player aircraft are Client slots")
    args = ap.parse_args()
    if not shutil.which("luajit"):
        raise SystemExit("luajit is required")
    if args.server:
        server = build(args.template, args.out, "campaign-server.miz", "lua/mission/campaign_bridge.lua", "",
                       "Load campaign bridge", args.server, clients=True)
        print(f"wrote {server}\nrepo roots tried by the loader: {wine_roots(args.server)}")
        return
    probe = build(args.template, args.out, "campaign-probe.miz", "lua/devtools/probe.lua",
                  f'ProbeConfig = {{ airbase = "{AIRBASE_NAME}", known_scenery_id = "{TOWER_SCENERY_ID}" }}\n',
                  "Load campaign API probe")
    smoke = build(args.template, args.out, "campaign-smoke.miz", "lua/mission/campaign_bridge.lua", "",
                  "Load campaign bridge")
    print(f"wrote {probe}\nwrote {smoke}\nrepo roots tried by the loader: {wine_roots()}")


if __name__ == "__main__":
    main()
