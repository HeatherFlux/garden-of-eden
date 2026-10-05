import os
import tempfile
import unittest
from unittest import mock

import config
from app.lib import presets as presets_lib
from app.sensors.schedule import schedule as sched


def _week(light, pump):
    return {
        "lights": {"days": {d: [dict(light)] for d in sched.DAYS}},
        "pump": {"days": {d: [dict(r) for r in pump] for d in sched.DAYS}},
    }


LIGHT = {"onTime": "07:00", "offTime": "21:00", "brightness": 65, "rampMinutes": 10}
PUMP = [{"time": "09:00", "duration": 4}]


class PresetsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        patches = [
            mock.patch.object(config, "PRESETS_FILE", os.path.join(self.tmp, "presets.json")),
            mock.patch.object(config, "SCHEDULE_FILE", os.path.join(self.tmp, "schedule.json")),
            mock.patch.object(sched, "_read_crontab", lambda: []),
            mock.patch.object(sched, "_write_crontab", lambda lines: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # --- built-ins ---
    def test_builtins_expand_to_every_day(self):
        names = [p["name"] for p in presets_lib.builtin_presets()]
        self.assertEqual(
            names,
            [
                "germinate",
                "seedling",
                "vegetative",
                "flower",
                "germinate-night",
                "seedling-night",
                "vegetative-night",
                "flower-night",
            ],
        )
        night = presets_lib.get_preset("Vegetative-Night")
        self.assertTrue(night["builtin"])
        for day in sched.DAYS:
            self.assertEqual(night["lights"]["days"][day][0]["onTime"], "20:00")
            self.assertEqual(len(night["pump"]["days"][day]), 3)

    # --- night mode ---
    @staticmethod
    def _minutes(hh_mm):
        h, m = (int(x) for x in hh_mm.split(":"))
        return h * 60 + m

    def _light_minutes(self, window):
        on, off = self._minutes(window["onTime"]), self._minutes(window["offTime"])
        return (off - on) % (24 * 60)

    def _in_dark(self, window, hh_mm):
        """True when hh_mm falls in [offTime, onTime), wrapping past midnight."""
        t = self._minutes(hh_mm)
        off, on = self._minutes(window["offTime"]), self._minutes(window["onTime"])
        return (t - off) % (24 * 60) < (on - off) % (24 * 60)

    def test_every_stage_has_a_night_variant_with_the_same_light_hours(self):
        for stage in ("germinate", "seedling", "vegetative", "flower"):
            day = presets_lib.get_preset(stage)["lights"]["days"]["mon"][0]
            night = presets_lib.get_preset(stage + "-night")["lights"]["days"]["mon"][0]
            self.assertEqual(self._light_minutes(night), self._light_minutes(day), stage)
            self.assertEqual(night["brightness"], day["brightness"], stage)

    def test_night_variants_light_overnight_and_go_dark_midday(self):
        for stage in ("germinate", "seedling", "vegetative", "flower"):
            win = presets_lib.get_preset(stage + "-night")["lights"]["days"]["mon"][0]
            self.assertGreaterEqual(self._minutes(win["onTime"]), 18 * 60, stage)
            self.assertTrue(9 * 60 <= self._minutes(win["offTime"]) <= 13 * 60, stage)
            self.assertTrue(self._in_dark(win, "15:00"), stage)
            self.assertFalse(self._in_dark(win, "02:00"), stage)

    def test_builtins_follow_gardyn_light_and_pump_limits(self):
        for preset in presets_lib.builtin_presets():
            win = preset["lights"]["days"]["mon"][0]
            hours = self._light_minutes(win) / 60
            self.assertTrue(14 <= hours <= 16, preset["name"])  # Gardyn: 14-16h Boost
            self.assertEqual(win["brightness"], 100, preset["name"])  # dim hours don't count
            runs = preset["pump"]["days"]["mon"]
            self.assertTrue(
                any(self._in_dark(win, r["time"]) for r in runs), preset["name"]
            )  # keep pods wet through the dark period
            for r in runs:
                self.assertLessEqual(r["duration"] * 60, config.MAX_PUMP_RUN_SECONDS)

    def test_builtin_names_cannot_be_saved_or_deleted(self):
        with self.assertRaises(ValueError):
            presets_lib.save_preset(dict(name="flower", **_week(LIGHT, PUMP)))
        with self.assertRaises(ValueError):
            presets_lib.delete_preset("flower")

    # --- custom ---
    def test_save_load_delete_roundtrip(self):
        saved = presets_lib.save_preset(dict(name="  My  Tomatoes ", **_week(LIGHT, PUMP)))
        self.assertEqual(saved["name"], "My Tomatoes")
        self.assertFalse(saved["builtin"])
        listed = presets_lib.load_presets()
        self.assertEqual(listed[-1]["name"], "My Tomatoes")
        self.assertEqual(listed[-1]["pump"]["days"]["sun"], [{"time": "09:00", "duration": 4}])
        # Same name (any case) replaces instead of duplicating.
        presets_lib.save_preset(dict(name="my tomatoes", **_week(dict(LIGHT, brightness=90), PUMP)))
        custom = presets_lib.load_custom()
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0]["lights"]["days"]["mon"][0]["brightness"], 90)
        presets_lib.delete_preset("MY TOMATOES")
        self.assertEqual(presets_lib.load_custom(), [])
        with self.assertRaises(KeyError):
            presets_lib.delete_preset("my tomatoes")

    def test_entries_are_canonicalized(self):
        raw = _week(
            {"onTime": "06:00", "offTime": "20:00", "brightness": "55"},
            [{"time": "10:00", "duration": "3"}],
        )
        raw["lights"]["days"]["mon"][0]["junk"] = True
        saved = presets_lib.save_preset(dict(name="clean", **raw))
        self.assertEqual(
            saved["lights"]["days"]["mon"],
            [{"onTime": "06:00", "offTime": "20:00", "brightness": 55, "rampMinutes": 0}],
        )
        self.assertEqual(saved["pump"]["days"]["mon"], [{"time": "10:00", "duration": 3}])

    def test_invalid_input_rejected(self):
        for bad_name in ("", "   ", "a" * 41, "bad/name", "custom"):
            with self.assertRaises(ValueError, msg=bad_name):
                presets_lib.save_preset(dict(name=bad_name, **_week(LIGHT, PUMP)))
        with self.assertRaises(ValueError):  # bad time caught at save, not at apply
            presets_lib.save_preset(dict(name="oops", **_week(dict(LIGHT, onTime="25:00"), PUMP)))
        with self.assertRaises(ValueError):
            presets_lib.save_preset(
                dict(name="oops", **_week(LIGHT, [{"time": "10:00", "duration": 0}]))
            )

    def test_corrupt_or_shadowing_entries_dropped_on_load(self):
        presets_lib._save_custom(
            [
                dict(name="germinate", **_week(LIGHT, PUMP)),  # shadows a built-in
                {
                    "name": "broken",
                    "lights": {"days": {"mon": [{"onTime": "99:00", "offTime": "1"}]}},
                },
                dict(name="good", **_week(LIGHT, PUMP)),
                "not a dict",
            ]
        )
        self.assertEqual([p["name"] for p in presets_lib.load_custom()], ["good"])

    # --- applying ---
    def test_apply_preset_writes_schedule_and_keeps_vacation(self):
        sched.save_schedule(
            {
                "lights": {"enabled": False, "days": {}},
                "pump": {"enabled": False, "days": {}},
                "vacation": {"enabled": True, "until": "2999-01-01"},
            }
        )
        applied = presets_lib.apply_preset("vegetative")
        self.assertTrue(applied["lights"]["enabled"])
        self.assertTrue(applied["pump"]["enabled"])
        self.assertEqual(applied["vacation"], {"enabled": True, "until": "2999-01-01"})
        self.assertEqual(sched.load_schedule()["lights"]["days"]["wed"][0]["brightness"], 100)
        with self.assertRaises(KeyError):
            presets_lib.apply_preset("nope")

    def test_active_preset_detection(self):
        presets_lib.apply_preset("flower")
        self.assertEqual(presets_lib.active_preset(sched.load_schedule()), "flower")
        presets_lib.save_preset(dict(name="mine", **_week(LIGHT, PUMP)))
        presets_lib.apply_preset("mine")
        self.assertEqual(presets_lib.active_preset(sched.load_schedule()), "mine")
        # Hand-editing one day means no preset matches any more.
        schedule = sched.load_schedule()
        schedule["pump"]["days"]["tue"].append({"time": "15:00", "duration": 2})
        self.assertIsNone(presets_lib.active_preset(schedule))
        self.assertIsNone(presets_lib.active_preset(sched.DEFAULT_SCHEDULE))


if __name__ == "__main__":
    unittest.main()
