import json
import os
import tempfile
import unittest
from unittest.mock import patch

from curl_cffi.requests.exceptions import RequestException

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


class FetchPageTests(unittest.TestCase):
    @patch("subot.requests.get")
    def test_impersonates_chrome(self, get):
        get.return_value.text = "html"

        self.assertEqual(subot.fetch_page("https://example.test/search"), "html")

        get.assert_called_once_with(
            "https://example.test/search",
            headers={"Accept-Language": "it-IT,it;q=0.9,en;q=0.8"},
            timeout=30,
            impersonate="chrome",
        )
        get.return_value.raise_for_status.assert_called_once_with()


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


class LoggingTests(unittest.TestCase):
    def test_malformed_ipv6_url_is_logged_as_invalid(self):
        cfg = {
            "search_url": "http://[bad",
            "ntfy_server": "https://ntfy.example",
        }

        with self.assertLogs("subot", level="INFO") as logs:
            subot.log_startup_config(cfg, dry_run=False, once=False)

        self.assertIn("search_origin=invalid", "\n".join(logs.output))

    def test_startup_config_omits_credentials_topics_and_queries(self):
        cfg = {
            "search_url": "https://search-secret@example.test/items?q=private",
            "ntfy_server": "https://server-secret@ntfy.example/private-base",
            "ntfy_topic": "private-topic",
            "ntfy_token": "private-token",
            "min_price": 100,
            "max_price": 500,
            "poll_interval_min": 60,
            "poll_interval_max": 120,
        }

        with self.assertLogs("subot", level="INFO") as logs:
            subot.log_startup_config(cfg, dry_run=False, once=False)

        output = "\n".join(logs.output)
        self.assertIn("search_origin=https://example.test", output)
        self.assertIn("ntfy_origin=https://ntfy.example", output)
        for secret in ("search-secret", "private", "server-secret", "private-token"):
            self.assertNotIn(secret, output)

    @patch("subot.extract_items")
    @patch("subot.fetch_page", return_value="html")
    @patch("subot.notify")
    def test_notification_error_does_not_log_sensitive_exception(self, notify, fetch, extract):
        extract.return_value = [raw_item("1", 200)]
        notify.side_effect = RequestException(
            "https://ntfy.example/private-topic?token=private-token"
        )
        cfg = {
            "search_url": "https://example.test/search",
            "min_price": 100,
            "max_price": 500,
        }

        with self.assertLogs("subot", level="ERROR") as logs:
            subot.run_once(cfg, set(), dry_run=False)

        output = "\n".join(logs.output)
        self.assertIn("error_type=RequestException", output)
        self.assertNotIn("private-topic", output)
        self.assertNotIn("private-token", output)


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
        notify.side_effect = [RequestException("unavailable"), None]
        seen = set()

        stats = subot.run_once(self.cfg, seen, dry_run=False)

        self.assertEqual(seen, {"2"})
        self.assertEqual(notify.call_count, 2)
        self.assertEqual(stats.fetched, 3)
        self.assertEqual(stats.matched, 2)
        self.assertEqual(stats.notified, 1)
        self.assertEqual(stats.failures, 1)

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
    @patch("subot.run_once", side_effect=RequestException("unavailable"))
    def test_once_returns_failure_when_fetch_fails(self, run_once, load_seen, load_config):
        with patch("sys.argv", ["subot.py", "--once"]):
            self.assertEqual(subot.main(), 1)

    @patch("subot.load_config", return_value={"poll_interval": 300})
    @patch("subot.load_seen", return_value=set())
    @patch("subot.save_seen")
    def test_summary_preserves_counts_when_cycle_fails(
        self, save_seen, load_seen, load_config
    ):
        def fail_after_fetch(cfg, seen, dry_run, stats):
            stats.fetched = 4
            stats.matched = 2
            raise ValueError("bad response")

        with patch("subot.run_once", side_effect=fail_after_fetch):
            with self.assertLogs("subot", level="INFO") as logs:
                with patch("sys.argv", ["subot.py", "--once"]):
                    self.assertEqual(subot.main(), 1)

        summaries = [line for line in logs.output if "polling cycle summary" in line]
        self.assertEqual(len(summaries), 1)
        self.assertIn("fetched=4 matched=2 notified=0 failures=1", summaries[0])

    @patch("subot.save_seen")
    @patch("subot.load_config", return_value={"poll_interval": 300})
    @patch("subot.load_seen", return_value=set())
    @patch("subot.run_once", return_value=subot.CycleStats(fetched=1, failures=1))
    def test_once_returns_failure_when_notification_fails(
        self, run_once, load_seen, load_config, save_seen
    ):
        with self.assertLogs("subot", level="INFO") as logs:
            with patch("sys.argv", ["subot.py", "--once"]):
                self.assertEqual(subot.main(), 1)

        summaries = [line for line in logs.output if "polling cycle summary" in line]
        self.assertEqual(len(summaries), 1)
        self.assertIn(
            "fetched=1 matched=0 notified=0 failures=1 next_poll_seconds=none",
            summaries[0],
        )


if __name__ == "__main__":
    unittest.main()
