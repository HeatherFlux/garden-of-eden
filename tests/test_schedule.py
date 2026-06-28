import unittest

from app.sensors.schedule import schedule as sched


class BuildCronLinesTestCase(unittest.TestCase):
    def test_lights_enabled_emits_on_and_off(self):
        s = {
            "lights": {"enabled": True, "onTime": "08:30", "offTime": "22:15", "brightness": 60},
            "pump": {"enabled": False, "runs": []},
        }
        lines = sched.build_cron_lines(s)
        self.assertEqual(len(lines), 2)
        self.assertIn("30 8 * * * /usr/local/bin/light --on --brightness 60", lines[0])
        self.assertIn("15 22 * * * /usr/local/bin/light --off", lines[1])
        self.assertTrue(all(sched.CRON_MARKER in ln for ln in lines))

    def test_pump_runs_convert_minutes_to_seconds(self):
        s = {
            "lights": {"enabled": False},
            "pump": {"enabled": True, "runs": [{"time": "12:00", "duration": 5}]},
        }
        lines = sched.build_cron_lines(s)
        self.assertEqual(len(lines), 1)
        self.assertIn("0 12 * * * /usr/local/bin/water 300", lines[0])

    def test_disabled_emits_nothing(self):
        self.assertEqual(sched.build_cron_lines(sched.DEFAULT_SCHEDULE), [])

    def test_invalid_time_raises(self):
        s = {"lights": {"enabled": True, "onTime": "99:99", "offTime": "22:00"}}
        with self.assertRaises(ValueError):
            sched.build_cron_lines(s)


if __name__ == "__main__":
    unittest.main()
