import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MISSION = ROOT / "lua" / "mission" / "campaign_bridge.lua"
HOOKS = ROOT / "lua" / "hooks" / "campaign_hooks.lua"
TRANSPORT = re.compile(r"-- BEGIN TRANSPORT\n.*?-- END TRANSPORT\n", re.S)
LUAJIT = shutil.which("luajit")


class LuaTests(unittest.TestCase):
    def test_transport_blocks_identical(self):
        mission = TRANSPORT.findall(MISSION.read_text())
        hooks = TRANSPORT.findall(HOOKS.read_text())
        self.assertEqual(len(mission), 1)
        self.assertEqual(len(hooks), 1)
        self.assertEqual(mission[0], hooks[0], "transport copies have diverged")

    @unittest.skipUnless(LUAJIT, "luajit not installed")
    def test_lua_syntax(self):
        for path in (MISSION, HOOKS):
            with self.subTest(path=path.name):
                r = subprocess.run([LUAJIT, "-bl", str(path)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(LUAJIT, "luajit not installed")
    def test_lua_transport(self):
        r = subprocess.run([LUAJIT, "tests/lua/test_transport.lua"], cwd=ROOT,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
