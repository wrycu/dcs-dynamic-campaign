import tempfile
import unittest
from pathlib import Path

from campaign.bubble import NM, BubbleConfig
from campaign.settings import SettingsError, bubble_config, load_bubble_config

ROOT = Path(__file__).resolve().parent.parent


class SettingsTests(unittest.TestCase):
    def test_nm_converted_to_metres(self):
        cfg = bubble_config({"ground_spawn_nm": 8, "ground_despawn_nm": 10, "sam_spawn_margin_nm": 5,
                             "sam_despawn_margin_nm": 9, "max_units": 50})
        self.assertEqual((cfg.ground_spawn, cfg.ground_despawn), (8 * NM, 10 * NM))
        self.assertEqual((cfg.threat_spawn_margin, cfg.threat_despawn_margin), (5 * NM, 9 * NM))
        self.assertEqual(cfg.max_units, 50)

    def test_missing_keys_keep_base(self):
        base = BubbleConfig(min_dwell=30)
        cfg = bubble_config({"ground_despawn_nm": 30}, base)
        self.assertEqual((cfg.min_dwell, cfg.ground_despawn), (30, 30 * NM))

    def test_errors(self):
        with self.assertRaises(SettingsError):
            bubble_config({"ground_spawn": 8})  # typo / wrong unit suffix
        with self.assertRaises(SettingsError):
            bubble_config({"ground_spawn_nm": "8"})
        with self.assertRaises(SettingsError):
            bubble_config({"ground_spawn_nm": 30, "ground_despawn_nm": 25})

    def test_shipped_config_loads(self):
        cfg = load_bubble_config(ROOT / "config" / "campaign.toml")
        self.assertEqual(cfg.ground_spawn, 20 * NM)

    def test_bad_toml(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text("[bubble\n")
            with self.assertRaises(SettingsError):
                load_bubble_config(p)


if __name__ == "__main__":
    unittest.main()
