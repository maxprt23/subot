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


class OpenRouterSettingsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "openrouter_api_key": "secret",
            "openrouter_model": "provider/model",
            "llm_max_retries": 3,
            "llm_web_search_max_results": 3,
            "llm_web_search_max_total_results": 6,
            "llm_web_fetch_max_uses": 2,
            "llm_web_fetch_max_content_tokens": 4000,
        }

    def test_returns_validated_openrouter_settings(self):
        self.assertEqual(
            config.get_openrouter_settings(self.cfg),
            {**self.cfg, "llm_reasoning_effort": None},
        )

    def test_accepts_configured_reasoning_effort(self):
        self.cfg["llm_reasoning_effort"] = "high"

        self.assertEqual(
            config.get_openrouter_settings(self.cfg)["llm_reasoning_effort"],
            "high",
        )

    def test_rejects_unknown_reasoning_effort(self):
        self.cfg["llm_reasoning_effort"] = "very-high"

        with self.assertRaisesRegex(ValueError, "llm_reasoning_effort"):
            config.get_openrouter_settings(self.cfg)

    def test_requires_non_empty_model_id(self):
        self.cfg["openrouter_model"] = ""

        with self.assertRaisesRegex(ValueError, "openrouter_model"):
            config.get_openrouter_settings(self.cfg)

    def test_rejects_negative_retry_limit(self):
        self.cfg["llm_max_retries"] = -1

        with self.assertRaisesRegex(ValueError, "llm_max_retries"):
            config.get_openrouter_settings(self.cfg)

    def test_rejects_more_than_three_retries(self):
        self.cfg["llm_max_retries"] = 4

        with self.assertRaisesRegex(ValueError, "llm_max_retries"):
            config.get_openrouter_settings(self.cfg)

    def test_rejects_non_positive_tool_limits(self):
        self.cfg["llm_web_fetch_max_uses"] = 0

        with self.assertRaisesRegex(ValueError, "llm_web_fetch_max_uses"):
            config.get_openrouter_settings(self.cfg)


class LlmEnabledTests(unittest.TestCase):
    def test_defaults_to_enabled(self):
        self.assertTrue(config.llm_enabled({}))

    def test_accepts_disabled_mode(self):
        self.assertFalse(config.llm_enabled({"use_llm": False}))

    def test_rejects_non_boolean_setting(self):
        with self.assertRaisesRegex(ValueError, "use_llm must be a boolean"):
            config.llm_enabled({"use_llm": "false"})


class RetryLimitTests(unittest.TestCase):
    def test_defaults_to_three_when_llm_is_disabled(self):
        self.assertEqual(config.get_retry_limit({}, default=3), 3)

    def test_rejects_malformed_optional_retry_limit(self):
        with self.assertRaisesRegex(ValueError, "llm_max_retries must be an integer"):
            config.get_retry_limit({"llm_max_retries": "three"}, default=3)


class NotificationChannelTests(unittest.TestCase):
    def test_defaults_existing_configurations_to_ntfy(self):
        self.assertEqual(
            config.get_notification_channels({"ntfy_server": "x", "ntfy_topic": "y"}),
            ("ntfy",),
        )

    def test_accepts_ntfy_and_telegram(self):
        cfg = {
            "notification_channels": ["telegram", "ntfy"],
            "ntfy_server": "https://ntfy.example",
            "ntfy_topic": "topic",
            "telegram_bot_token": "bot-token",
            "telegram_chat_id": "123",
        }

        self.assertEqual(
            config.get_notification_channels(cfg), ("telegram", "ntfy")
        )

    def test_rejects_no_selected_notification_channels(self):
        with self.assertRaisesRegex(
            ValueError, "notification_channels must select at least one channel"
        ):
            config.get_notification_channels({"notification_channels": []})

    def test_requires_new_configurations_to_select_a_channel(self):
        with self.assertRaisesRegex(
            ValueError, "notification_channels must select at least one channel"
        ):
            config.get_notification_channels({})

    def test_requires_settings_for_selected_telegram_channel(self):
        with self.assertRaisesRegex(ValueError, "telegram_chat_id"):
            config.validate_notification_settings(
                {
                    "notification_channels": ["telegram"],
                    "telegram_bot_token": "bot-token",
                }
            )

    def test_accepts_numeric_telegram_chat_id(self):
        cfg = {
            "notification_channels": ["telegram"],
            "telegram_bot_token": "bot-token",
            "telegram_chat_id": -1001234567890,
        }

        self.assertEqual(config.validate_notification_settings(cfg), ("telegram",))
