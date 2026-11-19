import datetime as dt
import unittest

from tools.restart_probe import WEATHERS, campaign_now, checks, next_mission


class RestartProbeTests(unittest.TestCase):
    def test_clock_rolls_over_midnight(self):
        now = campaign_now({"date": {"Year": 2020, "Month": 6, "Day": 30}, "abs": 22 * 3600 + 90})
        self.assertEqual(now, dt.datetime(2020, 6, 30, 22, 1, 30))
        n = next_mission(now, 6, 0)
        self.assertEqual(n["extra"]["date"], {"Year": 2020, "Month": 7, "Day": 1})
        self.assertEqual(n["extra"]["start_time"], 4 * 3600 + 60)
        self.assertEqual(n["label"], WEATHERS[0][0])
        self.assertEqual(next_mission(now, 6, len(WEATHERS))["label"], WEATHERS[0][0])

    def test_checks_flag_differences(self):
        n = next_mission(dt.datetime(2020, 6, 1, 8, 0), 6, 2)
        x = n["extra"]
        info = {"date": x["date"], "start_time": x["start_time"], "abs": x["start_time"],
                "weather": {"preset": "Preset1", "base": 2500, "temperature": 30, "qnh": 760, "fog": False,
                            "wind_ground": {"speed": 0, "dir": 0}}}
        failed = [item for item, ok, _ in checks(n, info) if not ok]
        self.assertEqual(failed, ["clouds"])


if __name__ == "__main__":
    unittest.main()
