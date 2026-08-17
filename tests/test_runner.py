import unittest
from unittest.mock import patch

from curl_cffi.requests.exceptions import RequestException

from subot_core import runner, state
from subot_core.models import CycleStats, SeenState
from tests.helpers import raw_item


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


if __name__ == "__main__":
    unittest.main()
