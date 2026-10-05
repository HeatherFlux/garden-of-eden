"""REST coverage for /schedule/presets (crontab stubbed, files in a temp dir)."""

import os
import tempfile
import unittest
from unittest import mock

import config
from app import create_app
from app.sensors.schedule import schedule as sched

WEEK = {
    "lights": {"days": {"mon": [{"onTime": "06:00", "offTime": "20:00", "brightness": 60}]}},
    "pump": {"days": {"mon": [{"time": "07:00", "duration": 2}]}},
}


class SchedulePresetsApiTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        for p in (
            mock.patch.object(config, "PRESETS_FILE", os.path.join(tmp, "presets.json")),
            mock.patch.object(config, "SCHEDULE_FILE", os.path.join(tmp, "schedule.json")),
            mock.patch.object(sched, "_read_crontab", lambda: []),
            mock.patch.object(sched, "_write_crontab", lambda lines: None),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.client = create_app("default").test_client()

    def test_list_has_builtins_and_no_active_by_default(self):
        resp = self.client.get("/schedule/presets")
        self.assertEqual(resp.status_code, 200)
        names = [p["name"] for p in resp.json["presets"]]
        self.assertEqual(names[:5], ["germinate", "seedling", "vegetative", "flower", "night"])
        self.assertTrue(all(p["builtin"] for p in resp.json["presets"]))
        self.assertIsNone(resp.json["active"])

    def test_save_get_apply_delete(self):
        resp = self.client.post("/schedule/presets", json=dict(name="Herbs", **WEEK))
        self.assertEqual(resp.status_code, 200, resp.json)
        self.assertEqual(resp.json["name"], "Herbs")
        self.assertFalse(resp.json["builtin"])

        resp = self.client.get("/schedule/presets/herbs")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json["pump"]["days"]["mon"], [{"time": "07:00", "duration": 2}])

        resp = self.client.post("/schedule/presets/herbs/apply")
        self.assertEqual(resp.status_code, 200, resp.json)
        self.assertTrue(resp.json["lights"]["enabled"])
        self.assertEqual(resp.json["lights"]["days"]["mon"][0]["brightness"], 60)
        self.assertEqual(
            self.client.get("/schedule").json["lights"]["days"]["mon"][0]["brightness"], 60
        )
        self.assertEqual(self.client.get("/schedule/presets").json["active"], "Herbs")

        resp = self.client.delete("/schedule/presets/herbs")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get("/schedule/presets/herbs").status_code, 404)
        self.assertEqual(self.client.delete("/schedule/presets/herbs").status_code, 404)

    def test_apply_builtin_and_unknown(self):
        self.assertEqual(self.client.post("/schedule/presets/nope/apply").status_code, 404)
        resp = self.client.post("/schedule/presets/flower/apply")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json["lights"]["days"]["sun"][0]["brightness"], 100)

    def test_validation_errors(self):
        self.assertEqual(self.client.post("/schedule/presets", json=[]).status_code, 400)
        resp = self.client.post("/schedule/presets", json=dict(name="flower", **WEEK))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("built-in", resp.json["error"])
        bad = {"name": "x", "lights": {"days": {"mon": [{"onTime": "24:00", "offTime": "1:00"}]}}}
        self.assertEqual(self.client.post("/schedule/presets", json=bad).status_code, 400)
        self.assertEqual(self.client.delete("/schedule/presets/night").status_code, 400)


if __name__ == "__main__":
    unittest.main()
