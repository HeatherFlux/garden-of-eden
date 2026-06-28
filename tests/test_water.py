import unittest

from app.lib.water import is_water_low


class WaterLowTestCase(unittest.TestCase):
    def test_low_when_distance_exceeds_threshold(self):
        # 15cm gap to the surface, alert above 11cm -> low.
        self.assertTrue(is_water_low(15.0, 11.0))

    def test_not_low_when_distance_within_threshold(self):
        self.assertFalse(is_water_low(8.0, 11.0))

    def test_equal_is_not_low(self):
        self.assertFalse(is_water_low(11.0, 11.0))

    def test_disabled_threshold_never_low(self):
        self.assertFalse(is_water_low(99.0, 0))
        self.assertFalse(is_water_low(99.0, None))

    def test_failed_reading_not_low(self):
        self.assertFalse(is_water_low(None, 11.0))


if __name__ == "__main__":
    unittest.main()
