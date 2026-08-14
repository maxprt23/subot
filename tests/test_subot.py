import json
import os
import tempfile
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


class SeenStateTests(unittest.TestCase):
    def test_failed_write_preserves_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "seen.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(["existing"], f)

            with patch("subot.json.dump", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    subot.save_seen(path, {"replacement"})

            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), ["existing"])
            self.assertEqual(os.listdir(directory), ["seen.json"])


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

        _, notification_failures = subot.run_once(self.cfg, seen, dry_run=False)

        self.assertEqual(seen, {"2"})
        self.assertEqual(notify.call_count, 2)
        self.assertEqual(notification_failures, 1)

    @patch("subot.extract_items")
    @patch("subot.fetch_page", return_value="html")
    @patch("subot.notify")
    def test_listing_is_reconsidered_after_entering_price_range(self, notify, fetch, extract):
        extract.side_effect = [[raw_item("1", 50)], [raw_item("1", 200)]]
        seen = set()

        subot.run_once(self.cfg, seen, dry_run=False)
        subot.run_once(self.cfg, seen, dry_run=False)

        notify.assert_called_once()
        self.assertEqual(seen, {"1"})


class NextSleepTests(unittest.TestCase):
    def test_uniform_range_respects_bounds(self):
        cfg = {"poll_interval_min": 5, "poll_interval_max": 10}
        for _ in range(100):
            self.assertIn(subot.next_sleep(cfg), range(5, 11))

    def test_falls_back_to_poll_interval(self):
        cfg = {"poll_interval": 300}
        for _ in range(100):
            self.assertEqual(subot.next_sleep(cfg), 300)

    def test_swaps_inverted_bounds(self):
        cfg = {"poll_interval_min": 10, "poll_interval_max": 5}
        for _ in range(100):
            self.assertIn(subot.next_sleep(cfg), range(5, 11))

    def test_defaults_to_300(self):
        self.assertEqual(subot.next_sleep({}), 300)


class MainTests(unittest.TestCase):
    @patch("subot.load_config", return_value={"poll_interval": 300})
    @patch("subot.load_seen", return_value=set())
    @patch("subot.run_once", side_effect=requests.RequestException("unavailable"))
    def test_once_returns_failure_when_fetch_fails(self, run_once, load_seen, load_config):
        with patch("sys.argv", ["subot.py", "--once"]):
            self.assertEqual(subot.main(), 1)

    @patch("subot.save_seen")
    @patch("subot.load_config", return_value={"poll_interval": 300})
    @patch("subot.load_seen", return_value=set())
    @patch("subot.run_once", return_value=(1, 1))
    def test_once_returns_failure_when_notification_fails(
        self, run_once, load_seen, load_config, save_seen
    ):
        with patch("sys.argv", ["subot.py", "--once"]):
            self.assertEqual(subot.main(), 1)


if __name__ == "__main__":
    unittest.main()
