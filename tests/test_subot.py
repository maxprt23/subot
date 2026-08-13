import json
import unittest
from unittest.mock import patch

import requests

import subot


def raw_item(aid, price, subject="Listing"):
    return {
        "urn": f"urn:subito:item:list:{aid}",
        "subject": subject,
        "urls": {"default": f"https://example.test/listing-{aid}.htm"},
        "features": {"/price": {"values": [{"key": str(price)}]}},
    }


class ExtractItemsTests(unittest.TestCase):
    def test_uses_only_unique_primary_results(self):
        original = {
            "urn": "urn:listing:1",
            "urls": {"default": "https://example.test/1"},
        }
        data = {
            "props": {
                "pageProps": {
                    "initialState": {
                        "items": {
                            "originalList": [original, original],
                            "galleryList": [{"id": "gallery"}],
                            "boostedItems": [{"id": "sponsored"}],
                        }
                    }
                }
            }
        }
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            f"{json.dumps(data)}"
            "</script>"
        )

        self.assertEqual(
            subot.extract_items(html),
            [original],
        )

    def test_missing_next_data_is_a_parse_error(self):
        with self.assertRaisesRegex(ValueError, "__NEXT_DATA__"):
            subot.extract_items("<html></html>")


class NotificationTests(unittest.TestCase):
    @patch("subot.requests.post")
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

        subot.notify(cfg, item)

        title = post.call_args.kwargs["headers"]["Title"]
        self.assertTrue(title.startswith("=?utf-8?"))
        title.encode("ascii")


class RunOnceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "search_url": "https://example.test/search",
            "min_price": 100,
            "max_price": 500,
        }

    @patch("subot.extract_items")
    @patch("subot.fetch_page", return_value="html")
    @patch("subot.notify")
    def test_dry_run_does_not_notify_or_mutate_seen(self, notify, fetch, extract):
        extract.return_value = [raw_item("1", 200), raw_item("2", 50)]
        seen = set()

        subot.run_once(self.cfg, seen, dry_run=True)

        self.assertEqual(seen, set())
        notify.assert_not_called()

    @patch("subot.extract_items")
    @patch("subot.fetch_page", return_value="html")
    @patch("subot.notify")
    def test_only_successfully_delivered_matches_are_seen(self, notify, fetch, extract):
        extract.return_value = [
            raw_item("1", 200),
            raw_item("2", 250),
            raw_item("3", 50),
        ]
        notify.side_effect = [requests.RequestException("unavailable"), None]
        seen = set()

        subot.run_once(self.cfg, seen, dry_run=False)

        self.assertEqual(seen, {"2", "3"})
        self.assertEqual(notify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
