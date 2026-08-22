import unittest
from unittest.mock import patch

from subot_core import notifications


class NotificationDispatchTests(unittest.TestCase):
    def setUp(self):
        self.item = {
            "subject": "Camera",
            "price": 200,
            "url": "https://example.test/listing",
            "town": "Rome",
            "city": "RM",
        }

    @patch("subot_core.notifications.telegram.notify")
    @patch("subot_core.notifications.ntfy.notify")
    def test_sends_to_every_selected_channel(self, ntfy_notify, telegram_notify):
        cfg = {
            "notification_channels": ["ntfy", "telegram"],
            "ntfy_server": "https://ntfy.example",
            "ntfy_topic": "topic",
            "telegram_bot_token": "bot-token",
            "telegram_chat_id": "123",
        }

        notifications.notify(cfg, self.item)

        ntfy_notify.assert_called_once_with(cfg, self.item)
        telegram_notify.assert_called_once_with(cfg, self.item)
