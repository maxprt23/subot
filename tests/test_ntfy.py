import unittest
from unittest.mock import patch

from subot_core import ntfy


class NotificationTests(unittest.TestCase):
    @patch("subot_core.ntfy.requests.post")
    def test_unicode_title_is_encoded_as_an_ascii_header(self, post):
        post.return_value.raise_for_status.return_value = None
        cfg = {"ntfy_server": "https://ntfy.example", "ntfy_topic": "topic"}
        item = {
            "subject": "🔥iPhone🔥",
            "price": 250,
            "url": "https://example.test/listing",
            "town": "",
            "city": "",
        }

        ntfy.notify(cfg, item)

        title = post.call_args.kwargs["headers"]["Title"]
        self.assertTrue(title.startswith("=?utf-8?"))
        title.encode("ascii")
