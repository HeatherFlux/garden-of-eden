import unittest

from app.sensors.schedule import schedule as sched


class BuildCronLinesTestCase(unittest.TestCase):
    def test_lights_per_day_emits_on_and_off_with_dow(self):
        s = {
            "lights": {
                "enabled": True,
                "days": {"mon": [{"onTime": "08:30", "offTime": "22:15", "brightness": 60}]},
            },
            "pump": {"enabled": False},
        }
        lines = sched.build_cron_lines(s)
        self.assertEqual(len(lines), 2)
        # Monday -> cron day-of-week 1
        self.assertIn("30 8 * * 1 /usr/local/bin/light --on --brightness 60", lines[0])
        self.assertIn("15 22 * * 1 /usr/local/bin/light --off", lines[1])
        self.assertTrue(all(sched.CRON_MARKER in ln for ln in lines))

    def test_multiple_entries_per_day(self):
        s = {
            "lights": {
                "enabled": True,
                "days": {
                    "fri": [
                        {"onTime": "06:00", "offTime": "09:00", "brightness": 35},
                        {"onTime": "18:00", "offTime": "22:00", "brightness": 70},
                    ]
                },
            }
        }
        lines = sched.build_cron_lines(s)
        self.assertEqual(len(lines), 4)  # two windows -> two on + two off
        self.assertTrue(all(" * * 5 " in ln for ln in lines))  # all on Friday

    def test_legacy_single_window_migrates_to_every_day(self):
        s = {
            "lights": {"enabled": True, "onTime": "08:00", "offTime": "22:00", "brightness": 70},
            "pump": {"enabled": False, "runs": []},
        }
        lines = sched.build_cron_lines(s)
        self.assertEqual(len(lines), 14)  # 7 days x (on + off)
        dows = {ln.split()[4] for ln in lines}
        self.assertEqual(dows, {"0", "1", "2", "3", "4", "5", "6"})

    def test_pump_runs_convert_minutes_to_seconds_with_dow(self):
        s = {"pump": {"enabled": True, "days": {"tue": [{"time": "06:30", "duration": 3}]}}}
        lines = sched.build_cron_lines(s)
        self.assertEqual(len(lines), 1)
        self.assertIn("30 6 * * 2 /usr/local/bin/water 180", lines[0])

    def test_pump_duration_clamped_to_safety_cap(self):
        # 10 minutes requested, but the hard cap is 5 minutes (300s).
        s = {"pump": {"enabled": True, "days": {"wed": [{"time": "12:00", "duration": 10}]}}}
        lines = sched.build_cron_lines(s)
        self.assertEqual(len(lines), 1)
        self.assertIn(f"/usr/local/bin/water {sched.config.MAX_PUMP_RUN_SECONDS} ", lines[0])
        self.assertNotIn("water 600", lines[0])

    def test_disabled_emits_nothing(self):
        self.assertEqual(sched.build_cron_lines(sched.DEFAULT_SCHEDULE), [])

    def test_invalid_time_raises(self):
        s = {
            "lights": {"enabled": True, "days": {"mon": [{"onTime": "99:99", "offTime": "22:00"}]}}
        }
        with self.assertRaises(ValueError):
            sched.build_cron_lines(s)


class NormalizeScheduleTestCase(unittest.TestCase):
    def test_legacy_pump_runs_apply_to_all_days(self):
        s = {"pump": {"enabled": True, "runs": [{"time": "12:00", "duration": 5}]}}
        norm = sched.normalize_schedule(s)
        self.assertEqual(set(norm["pump"]["days"]), set(sched.DAYS))
        for day in sched.DAYS:
            self.assertEqual(norm["pump"]["days"][day], [{"time": "12:00", "duration": 5}])

    def test_fills_missing_days(self):
        norm = sched.normalize_schedule({"lights": {"enabled": True, "days": {"mon": []}}})
        self.assertEqual(set(norm["lights"]["days"]), set(sched.DAYS))
        self.assertEqual(norm["lights"]["days"]["sun"], [])


if __name__ == "__main__":
    unittest.main()
