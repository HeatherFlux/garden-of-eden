import unittest
from unittest.mock import patch

from app import create_app


class PumpRunTestCase(unittest.TestCase):
    def setUp(self):
        self.client = create_app("default").test_client()

    @patch("app.sensors.pump.routes.threading.Timer")
    @patch("app.sensors.pump.routes.pump_control.on")
    def test_run_starts_pump_and_schedules_off(self, mock_on, mock_timer):
        resp = self.client.post("/pump/run", json={"seconds": 120})
        self.assertEqual(resp.status_code, 200)
        mock_on.assert_called_once()
        mock_timer.assert_called_once()  # off scheduled on a timer
        self.assertEqual(mock_timer.call_args[0][0], 120)

    def test_run_rejects_out_of_range(self):
        self.assertEqual(self.client.post("/pump/run", json={"seconds": 0}).status_code, 400)
        self.assertEqual(self.client.post("/pump/run", json={"seconds": 99999}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
