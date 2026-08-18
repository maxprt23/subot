import unittest
from unittest.mock import patch

from subot_core import config


class NextSleepTests(unittest.TestCase):
    @patch("subot_core.config.random.randint", return_value=7)
    def test_uses_configured_bounds(self, randint):
        cfg = {"poll_interval_min": 5, "poll_interval_max": 10}

        self.assertEqual(config.next_sleep(cfg), 7)
        randint.assert_called_once_with(5, 10)

    def test_rejects_inverted_bounds(self):
        cfg = {"poll_interval_min": 10, "poll_interval_max": 5}
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            config.next_sleep(cfg)

    def test_rejects_non_positive_bounds(self):
        cfg = {"poll_interval_min": 0, "poll_interval_max": 5}
        with self.assertRaisesRegex(ValueError, "must be positive"):
            config.next_sleep(cfg)
