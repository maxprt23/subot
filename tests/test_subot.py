import json
import os
import tempfile
import unittest
from unittest.mock import patch

from curl_cffi.requests.exceptions import RequestException

from subot_core import cli, config, ntfy, runner, state, subito
from subot_core.models import CycleStats, SeenState


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
            subito.extract_items(html),
            [original],
        )

    def test_missing_next_data_is_a_parse_error(self):
        with self.assertRaisesRegex(ValueError, "__NEXT_DATA__"):
            subito.extract_items("<html></html>")


class FetchPageTests(unittest.TestCase):
    @patch("subot_core.subito.requests.get")
    def test_impersonates_chrome(self, get):
        get.return_value.text = "html"

        self.assertEqual(subito.fetch_page("https://example.test/search"), "html")

        get.assert_called_once_with(
            "https://example.test/search",
            headers={"Accept-Language": "it-IT,it;q=0.9,en;q=0.8"},
            timeout=30,
            impersonate="chrome",
        )
        get.return_value.raise_for_status.assert_called_once_with()


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


class LoggingTests(unittest.TestCase):
    def test_malformed_ipv6_url_is_logged_as_invalid(self):
        cfg = {
            "search_urls": ["http://[bad"],
            "ntfy_server": "https://ntfy.example",
        }

        with self.assertLogs("subot", level="INFO") as logs:
            cli.log_startup_config(cfg, dry_run=False, once=False)

        self.assertIn("search_origins=invalid", "\n".join(logs.output))

    def test_startup_config_omits_credentials_topics_and_queries(self):
        cfg = {
            "search_urls": [
                "https://search-secret@example.test/items?q=private"
            ],
            "ntfy_server": "https://server-secret@ntfy.example/private-base",
            "ntfy_topic": "private-topic",
            "ntfy_token": "private-token",
            "poll_interval_min": 60,
            "poll_interval_max": 120,
        }

        with self.assertLogs("subot", level="INFO") as logs:
            cli.log_startup_config(cfg, dry_run=False, once=False)

        output = "\n".join(logs.output)
        self.assertIn("search_origins=https://example.test", output)
        self.assertIn("ntfy_origin=https://ntfy.example", output)
        for secret in ("search-secret", "private", "server-secret", "private-token"):
            self.assertNotIn(secret, output)

    @patch("subot_core.runner.extract_items")
    @patch("subot_core.runner.fetch_page", return_value="html")
    @patch("subot_core.runner.notify")
    def test_notification_error_does_not_log_sensitive_exception(self, notify, fetch, extract):
        extract.return_value = [raw_item("1", 200)]
        notify.side_effect = RequestException(
            "https://ntfy.example/private-topic?token=private-token"
        )
        cfg = {}

        with self.assertLogs("subot", level="ERROR") as logs:
            runner.run_once(
                cfg,
                "https://example.test/search",
                set(),
                dry_run=False,
                stats=CycleStats(),
            )

        output = "\n".join(logs.output)
        self.assertIn("error_type=RequestException", output)
        self.assertNotIn("private-topic", output)
        self.assertNotIn("private-token", output)


class SeenStateTests(unittest.TestCase):
    def test_missing_state_starts_with_no_initialized_searches(self):
        with tempfile.TemporaryDirectory() as directory:
            seen_state = state.load_seen(os.path.join(directory, "seen.json"))

        self.assertEqual(seen_state, SeenState())

    def test_non_object_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "seen.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(["existing"], f)

            with self.assertRaisesRegex(ValueError, "unsupported format"):
                state.load_seen(path)

    def test_structured_state_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "seen.json")
            expected = SeenState({"2", "1"}, {"search-key"})

            state.save_seen(path, expected)

            self.assertEqual(state.load_seen(path), expected)

    def test_failed_write_preserves_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "seen.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(["existing"], f)

            with patch("subot_core.state.json.dump", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    state.save_seen(
                        path, SeenState({"replacement"}, set())
                    )

            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), ["existing"])
            self.assertEqual(os.listdir(directory), ["seen.json"])


class RunOnceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {}
        self.search_url = "https://example.test/search"

    @patch("subot_core.runner.extract_items")
    @patch("subot_core.runner.fetch_page", return_value="html")
    @patch("subot_core.runner.notify")
    def test_initialization_records_existing_items_without_notifying(
        self, notify, fetch, extract
    ):
        extract.return_value = [
            raw_item("1", 200),
            raw_item("2", None),
        ]
        seen = set()
        stats = CycleStats()

        runner.run_once(
            self.cfg,
            self.search_url,
            seen,
            dry_run=False,
            stats=stats,
            initialize=True,
        )

        self.assertEqual(seen, {"1", "2"})
        self.assertEqual(stats.baselined, 2)
        self.assertEqual(stats.matched, 0)
        self.assertEqual(stats.notified, 0)
        notify.assert_not_called()

    @patch("subot_core.runner.extract_items")
    @patch("subot_core.runner.fetch_page", return_value="html")
    @patch("subot_core.runner.notify")
    def test_dry_run_does_not_notify_or_mutate_seen(self, notify, fetch, extract):
        extract.return_value = [raw_item("1", 200), raw_item("2", 50)]
        seen = set()

        runner.run_once(
            self.cfg,
            self.search_url,
            seen,
            dry_run=True,
            stats=CycleStats(),
            initialize=True,
        )

        self.assertEqual(seen, set())
        notify.assert_not_called()

    @patch("subot_core.runner.extract_items")
    @patch("subot_core.runner.fetch_page", return_value="html")
    @patch("subot_core.runner.notify")
    def test_only_successfully_delivered_matches_are_seen(self, notify, fetch, extract):
        extract.return_value = [
            raw_item("1", 200),
            raw_item("2", 250),
            raw_item("3", None),
        ]
        notify.side_effect = [RequestException("unavailable"), None]
        seen = set()

        stats = CycleStats()
        runner.run_once(
            self.cfg, self.search_url, seen, dry_run=False, stats=stats
        )

        self.assertEqual(seen, {"2"})
        self.assertEqual(notify.call_count, 2)
        self.assertEqual(stats.fetched, 3)
        self.assertEqual(stats.matched, 2)
        self.assertEqual(stats.notified, 1)
        self.assertEqual(stats.failures, 1)


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


class MultipleSearchTests(unittest.TestCase):
    def setUp(self):
        self.urls = [
            "https://example.test/first?secret=one",
            "https://example.test/second?secret=two",
        ]
        self.cfg = {
            "search_urls": self.urls,
            "poll_interval_min": 5,
            "poll_interval_max": 10,
        }

    @patch("subot_core.runner.save_seen")
    @patch("subot_core.runner.run_search")
    def test_continuous_mode_validates_intervals_before_first_search(
        self, run_search, save_seen
    ):
        self.cfg["poll_interval_min"] = 0

        with self.assertRaisesRegex(ValueError, "must be positive"):
            runner.run_continuously(
                self.cfg,
                self.urls,
                SeenState(),
                False,
                "/tmp/seen.json",
            )

        run_search.assert_not_called()
        save_seen.assert_not_called()

    @patch("subot_core.runner.save_seen")
    @patch("subot_core.runner.notify")
    @patch("subot_core.runner.extract_items")
    @patch("subot_core.runner.fetch_page", return_value="html")
    def test_once_shares_seen_across_overlapping_searches(
        self, fetch, extract, notify, save_seen
    ):
        saved_states = []
        save_seen.side_effect = lambda path, state: saved_states.append(
            SeenState(
                set(state.listing_ids), set(state.initialized_searches)
            )
        )
        extract.side_effect = [
            [raw_item("1", 200)],
            [raw_item("1", 200), raw_item("2", 250)],
        ]
        seen_state = SeenState(
            initialized_searches={state.search_key(url) for url in self.urls}
        )

        result = runner.run_all_once(
            self.cfg, self.urls, seen_state, False, "/tmp/seen.json"
        )

        self.assertEqual(result, 0)
        self.assertEqual(fetch.call_args_list[0].args, (self.urls[0],))
        self.assertEqual(fetch.call_args_list[1].args, (self.urls[1],))
        self.assertEqual(notify.call_count, 2)
        self.assertEqual(seen_state.listing_ids, {"1", "2"})
        self.assertEqual(
            [saved.listing_ids for saved in saved_states],
            [{"1"}, {"1", "2"}],
        )

    @patch("subot_core.runner.save_seen")
    @patch("subot_core.runner.notify")
    @patch("subot_core.runner.extract_items")
    @patch("subot_core.runner.fetch_page", return_value="html")
    def test_first_run_baselines_every_search_without_notifications(
        self, fetch, extract, notify, save_seen
    ):
        extract.side_effect = [
            [raw_item("1", 200)],
            [raw_item("1", 200), raw_item("2", 250)],
        ]
        seen_state = SeenState()

        result = runner.run_all_once(
            self.cfg, self.urls, seen_state, False, "/tmp/seen.json"
        )

        self.assertEqual(result, 0)
        notify.assert_not_called()
        self.assertEqual(seen_state.listing_ids, {"1", "2"})
        self.assertEqual(
            seen_state.initialized_searches,
            {state.search_key(url) for url in self.urls},
        )
        self.assertEqual(save_seen.call_count, 2)

    @patch("subot_core.runner.save_seen")
    @patch("subot_core.runner.notify")
    @patch("subot_core.runner.extract_items")
    @patch("subot_core.runner.fetch_page", return_value="html")
    def test_listing_after_baseline_is_notified(
        self, fetch, extract, notify, save_seen
    ):
        extract.side_effect = [
            [raw_item("1", 200)],
            [raw_item("1", 200), raw_item("2", 250)],
        ]
        seen_state = SeenState()

        runner.run_all_once(
            self.cfg, [self.urls[0]], seen_state, False, "/tmp/seen.json"
        )
        runner.run_all_once(
            self.cfg, [self.urls[0]], seen_state, False, "/tmp/seen.json"
        )

        self.assertEqual(notify.call_count, 1)
        self.assertEqual(seen_state.listing_ids, {"1", "2"})

    @patch("subot_core.runner.save_seen")
    @patch("subot_core.runner.run_search")
    def test_once_checks_every_search_and_reports_any_failure(
        self, run_search, save_seen
    ):
        run_search.side_effect = [
            CycleStats(failures=1),
            CycleStats(fetched=2),
        ]

        seen_state = SeenState()
        result = runner.run_all_once(
            self.cfg,
            self.urls,
            seen_state,
            False,
            "/tmp/seen.json",
        )

        self.assertEqual(result, 1)
        self.assertEqual(run_search.call_count, 2)
        self.assertEqual(
            [call.args[1] for call in run_search.call_args_list], self.urls
        )
        self.assertEqual(save_seen.call_count, 2)
        self.assertEqual(
            [call.kwargs["initialize"] for call in run_search.call_args_list],
            [True, True],
        )
        self.assertNotIn(
            state.search_key(self.urls[0]), seen_state.initialized_searches
        )
        self.assertIn(
            state.search_key(self.urls[1]), seen_state.initialized_searches
        )

    @patch("subot_core.runner.time.sleep")
    @patch("subot_core.runner.time.monotonic", side_effect=[0, 0, 0, 0, 0, 0])
    @patch("subot_core.runner.next_sleep", side_effect=[100, 200])
    @patch("subot_core.runner.save_seen")
    @patch("subot_core.runner.run_search")
    def test_initial_searches_run_immediately_before_rescheduled_search(
        self, run_search, save_seen, next_sleep, monotonic, sleep
    ):
        run_search.side_effect = [
            CycleStats(failures=1),
            CycleStats(),
            RuntimeError("stop test loop"),
        ]

        with self.assertRaisesRegex(RuntimeError, "stop test loop"):
            runner.run_continuously(
                self.cfg,
                self.urls,
                SeenState(),
                True,
                "/tmp/seen.json",
            )

        self.assertEqual(
            [call.args[1] for call in run_search.call_args_list],
            [self.urls[0], self.urls[1], self.urls[0]],
        )
        sleep.assert_called_once_with(100)

    @patch("subot_core.runner.save_seen", side_effect=[OSError("disk full"), None])
    @patch("subot_core.runner.run_search", return_value=CycleStats())
    def test_once_returns_failure_when_persistence_fails(
        self, run_search, save_seen
    ):
        with self.assertLogs("subot", level="ERROR"):
            result = runner.run_all_once(
                self.cfg,
                self.urls,
                SeenState(),
                False,
                "/tmp/seen.json",
            )

        self.assertEqual(result, 1)
        self.assertEqual(run_search.call_count, 2)
        self.assertEqual(save_seen.call_count, 2)


class MainTests(unittest.TestCase):
    @patch("subot_core.cli.run_all_once", return_value=1)
    @patch(
        "subot_core.cli.load_config",
        return_value={
            "search_urls": ["https://example.test/one", "https://example.test/two"],
            "poll_interval_min": 300,
            "poll_interval_max": 300,
        },
    )
    @patch("subot_core.cli.load_seen", return_value=SeenState())
    def test_once_delegates_all_urls_and_returns_failure(
        self, load_seen, load_config, run_all_once
    ):
        with patch("sys.argv", ["subot.py", "--once"]):
            self.assertEqual(cli.main(), 1)

        self.assertEqual(
            run_all_once.call_args.args[1],
            load_config.return_value["search_urls"],
        )


if __name__ == "__main__":
    unittest.main()
