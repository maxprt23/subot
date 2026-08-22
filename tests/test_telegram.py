import unittest
from unittest.mock import patch

from subot_core import telegram


class TelegramNotificationTests(unittest.TestCase):
    @patch("subot_core.telegram.requests.post")
    def test_posts_listing_to_configured_chat(self, post):
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {"ok": True}
        cfg = {"telegram_bot_token": "bot-token", "telegram_chat_id": "123"}
        item = {
            "subject": "Camera",
            "price": 200,
            "url": "https://example.test/listing",
            "town": "Rome",
            "city": "RM",
        }

        telegram.notify(cfg, item)

        self.assertEqual(
            post.call_args.args[0],
            "https://api.telegram.org/botbot-token/sendMessage",
        )
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "chat_id": "123",
                "text": "Camera\n200 €\nRome (RM)\nhttps://example.test/listing",
            },
        )
