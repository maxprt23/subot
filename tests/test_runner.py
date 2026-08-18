import fcntl
import os
import tempfile
import unittest
from unittest.mock import patch

from curl_cffi.requests.exceptions import RequestException

from subot_core import runner, state
from subot_core.models import CycleStats, JobStatus, ListingJob, SeenState
from subot_core.openrouter import OpenRouterDecisionError
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


class QueuedPollingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {}
        self.search_url = "https://example.test/search"

    @patch("subot_core.runner.extract_items")
    @patch("subot_core.runner.fetch_page", return_value="html")
    def test_later_poll_queues_new_priced_listing_without_notifying(
        self, fetch, extract
    ):
        extract.return_value = [raw_item("1", 200), raw_item("2", None)]
        store = unittest.mock.Mock()
        store.is_search_initialized.return_value = True
        store.is_listing_known.return_value = False
        store.enqueue_listing.return_value = True
        stats = CycleStats()

        runner.poll_search_once(
            self.cfg, self.search_url, store, dry_run=False, stats=stats
        )

        store.enqueue_listing.assert_called_once()
        self.assertEqual(store.enqueue_listing.call_args.args[0]["id"], "1")
        self.assertEqual(stats.matched, 1)
        self.assertEqual(stats.notified, 0)

    @patch("subot_core.runner.extract_items")
    @patch("subot_core.runner.fetch_page", return_value="html")
    def test_dry_run_counts_only_unseen_priced_listings_without_mutating_store(
        self, fetch, extract
    ):
        extract.return_value = [
            raw_item("1", 200),
            raw_item("2", 250),
            raw_item("3", None),
        ]
        store = unittest.mock.Mock()
        store.is_search_initialized.return_value = True
        store.is_listing_known.side_effect = lambda listing_id: listing_id == "1"
        stats = CycleStats()

        runner.poll_search_once(
            self.cfg, self.search_url, store, dry_run=True, stats=stats
        )

        self.assertEqual(stats.matched, 1)
        store.is_listing_known.assert_has_calls(
            [unittest.mock.call("1"), unittest.mock.call("2")]
        )
        store.enqueue_listing.assert_not_called()
        store.initialize_search.assert_not_called()

    @patch("subot_core.runner.extract_items")
    @patch("subot_core.runner.fetch_page", return_value="html")
    def test_uninitialized_dry_run_excludes_globally_known_priced_listings(
        self, fetch, extract
    ):
        extract.return_value = [
            raw_item("1", 200),
            raw_item("2", 250),
            raw_item("3", None),
        ]
        store = unittest.mock.Mock()
        store.is_search_initialized.return_value = False
        store.is_listing_known.side_effect = lambda listing_id: listing_id == "1"
        stats = CycleStats()

        runner.poll_search_once(
            self.cfg, self.search_url, store, dry_run=True, stats=stats
        )

        self.assertEqual(stats.matched, 1)
        store.is_listing_known.assert_has_calls(
            [unittest.mock.call("1"), unittest.mock.call("2")]
        )
        store.initialize_search.assert_not_called()
        store.enqueue_listing.assert_not_called()

    @patch("subot_core.runner.extract_items")
    @patch("subot_core.runner.fetch_page", return_value="html")
    def test_first_poll_records_a_baseline_without_queueing(
        self, fetch, extract
    ):
        extract.return_value = [raw_item("1", 200), raw_item("2", None)]
        store = unittest.mock.Mock()
        store.is_search_initialized.return_value = False
        store.initialize_search.return_value = 2

        stats = CycleStats()
        runner.poll_search_once(
            self.cfg, self.search_url, store, dry_run=False, stats=stats
        )

        store.initialize_search.assert_called_once()
        store.enqueue_listing.assert_not_called()
        self.assertEqual(stats.baselined, 2)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"llm_max_retries": 3}
        self.job = ListingJob(
            listing_id="1",
            payload={
                "id": "1",
                "subject": "Camera",
                "price": 200,
                "url": "https://example.test/1",
                "town": "",
                "city": "",
            },
            status=JobStatus.CLAIMED,
            attempts=1,
        )

    @patch("subot_core.runner.notify")
    def test_true_decision_notifies_then_marks_job_notified(self, notify):
        store = unittest.mock.Mock()
        client = unittest.mock.Mock()
        client.decide.return_value = True

        self.assertTrue(runner.process_claimed_job(self.cfg, store, client, self.job))

        notify.assert_called_once_with(self.cfg, self.job.payload)
        store.mark_notified.assert_called_once_with("1")
        store.mark_rejected.assert_not_called()

    @patch("subot_core.runner.notify")
    def test_false_decision_marks_job_rejected_without_notifying(self, notify):
        store = unittest.mock.Mock()
        client = unittest.mock.Mock()
        client.decide.return_value = False

        self.assertTrue(runner.process_claimed_job(self.cfg, store, client, self.job))

        notify.assert_not_called()
        store.mark_rejected.assert_called_once_with("1")

    def test_invalid_decision_is_requeued_with_the_configured_retry_limit(self):
        store = unittest.mock.Mock()
        client = unittest.mock.Mock()
        client.decide.side_effect = OpenRouterDecisionError("invalid")

        self.assertFalse(runner.process_claimed_job(self.cfg, store, client, self.job))

        store.fail_or_retry.assert_called_once_with(
            "1", "OpenRouterDecisionError", max_retries=3
        )

    def test_worker_fails_before_recovering_when_another_worker_holds_state_lock(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state.sqlite3")
            with open(state_path, "a+") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch("subot_core.runner.StateStore") as state_store:
                    with self.assertRaises(SystemExit) as raised:
                        runner.run_worker_process(
                            unittest.mock.Mock(),
                            unittest.mock.Mock(),
                            False,
                            self.cfg,
                            state_path,
                            False,
                        )

            self.assertEqual(raised.exception.code, 1)
            state_store.assert_not_called()

    def test_worker_holds_state_lock_until_normal_exit_then_releases_it(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state.sqlite3")
            stop_event = unittest.mock.Mock()
            stop_event.is_set.return_value = False
            poller_done_event = unittest.mock.Mock()
            poller_done_event.is_set.return_value = True
            store = unittest.mock.Mock()
            store.claim_next.side_effect = lambda: self._assert_state_lock_held(
                state_path
            )
            store.all_terminal.return_value = True

            with patch("subot_core.runner.StateStore") as state_store:
                state_store.return_value.__enter__.return_value = store
                with patch("subot_core.runner.openrouter_client_from_config"):
                    runner.run_worker_process(
                        stop_event,
                        poller_done_event,
                        True,
                        self.cfg,
                        state_path,
                        False,
                    )

            with open(state_path, "a+") as contender:
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _assert_state_lock_held(state_path):
        with open(state_path, "a+") as contender:
            with unittest.TestCase().assertRaises(BlockingIOError):
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return None


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
