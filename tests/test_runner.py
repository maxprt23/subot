import fcntl
import os
import signal
import tempfile
import unittest
from unittest.mock import patch

from curl_cffi.requests.exceptions import RequestException

from subot_core import runner, subito, vinted
from subot_core.models import CycleStats, JobStatus, ListingJob
from subot_core.openrouter import OpenRouterDecisionError
from tests.helpers import raw_item


class ChildSignalHandlingTests(unittest.TestCase):
    @patch("subot_core.runner.signal.signal")
    def test_child_ignores_sigint_and_uses_supervisor_stop_event(self, signal_call):
        runner._ignore_sigint_in_child()

        signal_call.assert_called_once_with(signal.SIGINT, signal.SIG_IGN)


class QueuedPollingTests(unittest.TestCase):
    def setUp(self):
        self.search_url = "https://www.subito.it/annunci-italia/vendita/usato/"

    @patch("subot_core.subito.extract_items")
    @patch("subot_core.subito.fetch_page", return_value="html")
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
            self.search_url, store, dry_run=False, stats=stats
        )

        store.enqueue_listing.assert_called_once()
        self.assertEqual(
            store.enqueue_listing.call_args.args[0]["id"], "subito:1"
        )
        self.assertEqual(stats.matched, 1)

    @patch("subot_core.subito.extract_items")
    @patch("subot_core.subito.fetch_page", return_value="html")
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
        store.is_listing_known.side_effect = (
            lambda listing_id: listing_id == "subito:1"
        )
        stats = CycleStats()

        runner.poll_search_once(
            self.search_url, store, dry_run=True, stats=stats
        )

        self.assertEqual(stats.matched, 1)
        store.is_listing_known.assert_has_calls(
            [
                unittest.mock.call("subito:1"),
                unittest.mock.call("subito:2"),
            ]
        )
        store.enqueue_listing.assert_not_called()
        store.initialize_search.assert_not_called()

    @patch("subot_core.subito.extract_items")
    @patch("subot_core.subito.fetch_page", return_value="html")
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
        store.is_listing_known.side_effect = (
            lambda listing_id: listing_id == "subito:1"
        )
        stats = CycleStats()

        runner.poll_search_once(
            self.search_url, store, dry_run=True, stats=stats
        )

        self.assertEqual(stats.matched, 1)
        store.is_listing_known.assert_has_calls(
            [
                unittest.mock.call("subito:1"),
                unittest.mock.call("subito:2"),
            ]
        )
        store.initialize_search.assert_not_called()
        store.enqueue_listing.assert_not_called()

    @patch("subot_core.subito.extract_items")
    @patch("subot_core.subito.fetch_page", return_value="html")
    def test_first_poll_records_a_baseline_without_queueing(
        self, fetch, extract
    ):
        extract.return_value = [raw_item("1", 200), raw_item("2", None)]
        store = unittest.mock.Mock()
        store.is_search_initialized.return_value = False
        store.initialize_search.return_value = 2

        stats = CycleStats()
        runner.poll_search_once(
            self.search_url, store, dry_run=False, stats=stats
        )

        store.initialize_search.assert_called_once()
        store.enqueue_listing.assert_not_called()
        self.assertEqual(stats.baselined, 2)


class SourceRoutingTests(unittest.TestCase):
    def test_routes_supported_subito_urls_to_the_subito_parser(self):
        self.assertIs(
            runner.source_for_url(
                "https://www.subito.it/annunci-italia/vendita/usato/?q=iphone"
            ),
            subito,
        )
        self.assertIs(
            runner.source_for_url("https://subito.it/qualsiasi-percorso"),
            subito,
        )

    def test_routes_vinted_catalog_and_category_urls_to_vinted(self):
        self.assertIs(
            runner.source_for_url(
                "https://www.vinted.it/catalog?search_text=iphone"
            ),
            vinted,
        )
        self.assertIs(
            runner.source_for_url(
                "https://vinted.it/catalog/123-elettronica?search_text=iphone"
            ),
            vinted,
        )

    def test_rejects_non_catalog_vinted_path_before_fetch(self):
        with patch.object(vinted, "fetch_page") as fetch_page:
            with self.assertRaisesRegex(ValueError, "unsupported search URL"):
                runner.poll_search_once(
                    "https://www.vinted.it/member/123",
                    unittest.mock.Mock(),
                    dry_run=True,
                    stats=CycleStats(),
                )

        fetch_page.assert_not_called()

    def test_rejects_unsupported_host_before_fetch(self):
        with patch.object(subito, "fetch_page") as subito_fetch, patch.object(
            vinted, "fetch_page"
        ) as vinted_fetch:
            with self.assertRaisesRegex(ValueError, "unsupported search URL"):
                runner.poll_search_once(
                    "https://example.test/search",
                    unittest.mock.Mock(),
                    dry_run=True,
                    stats=CycleStats(),
                )

        subito_fetch.assert_not_called()
        vinted_fetch.assert_not_called()

    @patch("subot_core.vinted.parse_item")
    @patch("subot_core.vinted.extract_items")
    @patch("subot_core.vinted.fetch_page", return_value="html")
    def test_poll_uses_vinted_parser_for_catalog_url(
        self, fetch_page, extract_items, parse_item
    ):
        raw = object()
        item = {
            "id": "vinted:1",
            "subject": "iPhone",
            "price": 200,
            "url": "https://www.vinted.it/items/1",
            "town": "",
            "city": "",
        }
        extract_items.return_value = [raw]
        parse_item.return_value = item
        store = unittest.mock.Mock()
        store.is_search_initialized.return_value = True
        store.enqueue_listing.return_value = True
        stats = CycleStats()

        runner.poll_search_once(
            "https://www.vinted.it/catalog?search_text=iphone",
            store,
            dry_run=False,
            stats=stats,
        )

        fetch_page.assert_called_once_with(
            "https://www.vinted.it/catalog?search_text=iphone"
        )
        extract_items.assert_called_once_with("html")
        parse_item.assert_called_once_with(raw)
        store.enqueue_listing.assert_called_once_with(item)


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

    @patch("subot_core.runner.notify")
    def test_disabled_llm_notifies_without_decision(self, notify):
        store = unittest.mock.Mock()
        client = unittest.mock.Mock()
        cfg = {"use_llm": False, "llm_max_retries": 3}

        self.assertTrue(runner.process_claimed_job(cfg, store, client, self.job))

        client.decide.assert_not_called()
        notify.assert_called_once_with(cfg, self.job.payload)
        store.mark_notified.assert_called_once_with("1")
        store.mark_rejected.assert_not_called()

    @patch("subot_core.runner.notify")
    def test_disabled_llm_uses_default_retry_limit_without_llm_settings(self, notify):
        store = unittest.mock.Mock()
        notify.side_effect = RequestException("unavailable")

        self.assertFalse(
            runner.process_claimed_job({"use_llm": False}, store, None, self.job)
        )

        store.fail_or_retry.assert_called_once_with(
            "1", "RequestException", max_retries=3
        )

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
                    )

            with open(state_path, "a+") as contender:
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _assert_state_lock_held(state_path):
        with open(state_path, "a+") as contender:
            with unittest.TestCase().assertRaises(BlockingIOError):
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return None


class OpenRouterClientFromConfigTests(unittest.TestCase):
    @patch(
        "subot_core.runner.load_llm_prompts",
        return_value=("System instructions.", "Notification rules."),
    )
    @patch("subot_core.runner.OpenRouterClient")
    def test_loads_system_prompt_and_rules_from_tracked_files(
        self, client_class, load_llm_prompts
    ):
        cfg = {
            "openrouter_api_key": "secret",
            "openrouter_model": "provider/model",
            "llm_max_retries": 3,
            "llm_web_search_max_results": 3,
            "llm_web_search_max_total_results": 6,
            "llm_web_fetch_max_uses": 2,
            "llm_web_fetch_max_content_tokens": 4000,
        }

        runner.openrouter_client_from_config(cfg)

        load_llm_prompts.assert_called_once_with()
        self.assertEqual(
            client_class.call_args.kwargs["system_prompt"],
            "System instructions.",
        )
        self.assertEqual(
            client_class.call_args.kwargs["rules"],
            "Notification rules.",
        )


class ChildProcessLoggingTests(unittest.TestCase):
    @patch("subot_core.runner.run_queued_search", return_value=CycleStats())
    @patch("subot_core.runner.configure_logging")
    @patch("subot_core.runner._ignore_sigint_in_child")
    def test_poller_configures_logging_in_its_child_process(
        self, ignore_sigint, configure_logging, run_queued_search
    ):
        stop_event = unittest.mock.Mock()
        stop_event.is_set.return_value = False
        with tempfile.TemporaryDirectory() as directory:
            runner.run_poller_process(
                stop_event,
                unittest.mock.Mock(),
                True,
                {},
                ["https://example.test/search"],
                os.path.join(directory, "state.sqlite3"),
            )

        ignore_sigint.assert_called_once_with()
        configure_logging.assert_called_once_with()
        run_queued_search.assert_called_once()

    @patch("subot_core.runner.openrouter_client_from_config")
    @patch("subot_core.runner.StateStore")
    @patch("subot_core.runner.configure_logging")
    @patch("subot_core.runner._ignore_sigint_in_child")
    def test_worker_configures_logging_in_its_child_process(
        self, ignore_sigint, configure_logging, state_store, client_from_config
    ):
        stop_event = unittest.mock.Mock()
        stop_event.is_set.return_value = False
        poller_done_event = unittest.mock.Mock()
        poller_done_event.is_set.return_value = True
        store = state_store.return_value.__enter__.return_value
        store.claim_next.return_value = None
        store.all_terminal.return_value = True

        with tempfile.TemporaryDirectory() as directory:
            runner.run_worker_process(
                stop_event,
                poller_done_event,
                True,
                {"llm_max_retries": 3},
                os.path.join(directory, "state.sqlite3"),
            )

        ignore_sigint.assert_called_once_with()
        configure_logging.assert_called_once_with()
        client_from_config.assert_called_once()

    @patch("subot_core.runner.openrouter_client_from_config")
    @patch("subot_core.runner.StateStore")
    def test_worker_does_not_create_openrouter_client_when_llm_is_disabled(
        self, state_store, client_from_config
    ):
        stop_event = unittest.mock.Mock()
        stop_event.is_set.return_value = False
        poller_done_event = unittest.mock.Mock()
        poller_done_event.is_set.return_value = True
        store = state_store.return_value.__enter__.return_value
        store.claim_next.return_value = None
        store.all_terminal.return_value = True

        with tempfile.TemporaryDirectory() as directory:
            runner.run_worker_process(
                stop_event,
                poller_done_event,
                True,
                {"use_llm": False},
                os.path.join(directory, "state.sqlite3"),
            )

        client_from_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
