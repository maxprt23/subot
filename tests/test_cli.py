import os
import tempfile
import unittest
from unittest.mock import patch

from subot_core import cli, runner
from subot_core.models import CycleStats


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

class MainTests(unittest.TestCase):
    def setUp(self):
        initialize_state_patcher = patch(
            "subot_core.cli.initialize_state"
        )
        self.initialize_state = initialize_state_patcher.start()
        self.addCleanup(initialize_state_patcher.stop)
        self.cfg = {
            "search_urls": [
                "https://example.test/one",
                "https://example.test/two",
            ],
            "poll_interval_min": 300,
            "poll_interval_max": 300,
            "ntfy_server": "https://ntfy.example",
            "ntfy_topic": "topic",
            "openrouter_api_key": "secret",
            "openrouter_model": "provider/model",
            "llm_max_retries": 3,
            "llm_web_search_max_results": 3,
            "llm_web_search_max_total_results": 6,
            "llm_web_fetch_max_uses": 2,
            "llm_web_fetch_max_content_tokens": 4000,
        }

    @patch("subot_core.cli.run_supervisor", return_value=0)
    @patch("subot_core.cli.load_config", return_value=None)
    def test_normal_startup_initializes_state_before_spawning_children(
        self, load_config, run_supervisor
    ):
        load_config.return_value = self.cfg
        calls = []
        self.initialize_state.side_effect = lambda path: calls.append(
            ("initialize", path)
        )
        run_supervisor.side_effect = lambda *args, **kwargs: calls.append(
            ("supervisor", None)
        ) or 0

        with patch("sys.argv", ["subot.py"]):
            self.assertEqual(cli.main(), 0)

        self.assertEqual(
            calls,
            [
                ("initialize", cli.STATE_PATH),
                ("supervisor", None),
            ],
        )

    @patch("subot_core.cli.run_supervisor", return_value=1)
    @patch(
        "subot_core.cli.load_config",
        return_value=None,
    )
    def test_once_starts_poller_and_worker_and_returns_supervisor_status(
        self, load_config, run_supervisor
    ):
        load_config.return_value = self.cfg
        with patch("sys.argv", ["subot.py", "--once"]):
            self.assertEqual(cli.main(), 1)

        self.assertEqual(run_supervisor.call_args.args[:2], (
            runner.run_poller_process,
            runner.run_worker_process,
        ))
        self.assertEqual(
            run_supervisor.call_args.kwargs["poller_args"][1],
            self.cfg["search_urls"],
        )
        self.assertTrue(run_supervisor.call_args.kwargs["once"])
        self.assertIsNotNone(run_supervisor.call_args.kwargs["worker_done"])

    @patch("subot_core.cli.get_openrouter_settings")
    @patch("subot_core.cli.run_supervisor", return_value=0)
    @patch("subot_core.cli.load_config", return_value=None)
    def test_disabled_llm_does_not_validate_openrouter_settings(
        self, load_config, run_supervisor, get_openrouter_settings
    ):
        load_config.return_value = {
            "search_urls": ["https://example.test/one"],
            "poll_interval_min": 300,
            "poll_interval_max": 300,
            "ntfy_server": "https://ntfy.example",
            "ntfy_topic": "topic",
            "use_llm": False,
        }

        with patch("sys.argv", ["subot.py", "--once"]):
            self.assertEqual(cli.main(), 0)

        get_openrouter_settings.assert_not_called()
        run_supervisor.assert_called_once()

    @patch("subot_core.cli.run_supervisor")
    @patch("subot_core.cli.load_config", return_value=None)
    def test_disabled_llm_rejects_malformed_optional_retry_limit(
        self, load_config, run_supervisor
    ):
        load_config.return_value = {
            "search_urls": ["https://example.test/one"],
            "poll_interval_min": 300,
            "poll_interval_max": 300,
            "ntfy_server": "https://ntfy.example",
            "ntfy_topic": "topic",
            "use_llm": False,
            "llm_max_retries": "three",
        }

        with patch("sys.argv", ["subot.py", "--once"]):
            with self.assertRaisesRegex(ValueError, "llm_max_retries"):
                cli.main()

        run_supervisor.assert_not_called()

    @patch("subot_core.cli.queue_is_successful", return_value=True)
    @patch("subot_core.cli.queue_failure_boundary", return_value=4)
    @patch("subot_core.cli.run_supervisor", return_value=0)
    @patch("subot_core.cli.load_config", return_value=None)
    def test_once_worker_done_uses_pre_run_failure_boundary(
        self, load_config, run_supervisor, queue_failure_boundary,
        queue_is_successful,
    ):
        load_config.return_value = self.cfg

        with patch("sys.argv", ["subot.py", "--once"]):
            self.assertEqual(cli.main(), 0)

        queue_failure_boundary.assert_called_once_with(cli.STATE_PATH)
        worker_done = run_supervisor.call_args.kwargs["worker_done"]
        self.assertTrue(worker_done())
        queue_is_successful.assert_called_once_with(cli.STATE_PATH, 4)

    @patch("subot_core.cli.run_dry_run_once", return_value=0)
    @patch("subot_core.cli.StateStore")
    @patch("subot_core.cli.load_config", return_value=None)
    def test_dry_run_once_uses_existing_sqlite_state_read_only(
        self, load_config, state_store, run_dry_run_once
    ):
        load_config.return_value = {
            "search_urls": ["https://example.test/one"],
            "poll_interval_min": 300,
            "poll_interval_max": 300,
            "ntfy_server": "https://ntfy.example",
        }
        store = state_store.return_value.__enter__.return_value

        with patch("sys.argv", ["subot.py", "--once", "--dry-run"]), patch(
            "subot_core.cli.os.path.exists", return_value=True
        ):
            self.assertEqual(cli.main(), 0)

        state_store.assert_called_once_with(cli.STATE_PATH, read_only=True)
        run_dry_run_once.assert_called_once_with(
            load_config.return_value["search_urls"],
            store,
        )

    @patch("subot_core.cli.run_dry_run_once", return_value=0)
    @patch("subot_core.cli.StateStore")
    @patch("subot_core.cli.load_config", return_value=None)
    def test_dry_run_once_uses_ephemeral_state_when_sqlite_is_missing(
        self, load_config, state_store, run_dry_run_once
    ):
        load_config.return_value = {
            "search_urls": ["https://example.test/one"],
            "poll_interval_min": 300,
            "poll_interval_max": 300,
            "ntfy_server": "https://ntfy.example",
        }
        store = state_store.return_value.__enter__.return_value

        with patch("sys.argv", ["subot.py", "--once", "--dry-run"]), patch(
            "subot_core.cli.os.path.exists", return_value=False
        ):
            self.assertEqual(cli.main(), 0)

        state_store.assert_called_once_with(":memory:")
        run_dry_run_once.assert_called_once_with(
            load_config.return_value["search_urls"],
            store,
        )

    @patch("subot_core.cli.run_dry_run_continuously", return_value=None)
    @patch("subot_core.cli.StateStore")
    @patch("subot_core.cli.load_config", return_value=None)
    def test_continuous_dry_run_keeps_the_continuous_lifecycle(
        self, load_config, state_store, run_dry_run_continuously
    ):
        load_config.return_value = {
            "search_urls": ["https://example.test/one"],
            "poll_interval_min": 300,
            "poll_interval_max": 300,
            "ntfy_server": "https://ntfy.example",
        }
        store = state_store.return_value.__enter__.return_value

        with patch("sys.argv", ["subot.py", "--dry-run"]), patch(
            "subot_core.cli.os.path.exists", return_value=True
        ):
            self.assertIsNone(cli.main())

        state_store.assert_called_once_with(cli.STATE_PATH, read_only=True)
        run_dry_run_continuously.assert_called_once_with(
            load_config.return_value,
            load_config.return_value["search_urls"],
            store,
        )

    @patch("subot_core.cli.run_dry_run_once", return_value=0)
    @patch("subot_core.cli.load_config", return_value=None)
    def test_dry_run_missing_sqlite_does_not_create_state_file(
        self, load_config, run_dry_run_once
    ):
        load_config.return_value = {
            "search_urls": ["https://example.test/one"],
            "poll_interval_min": 300,
            "poll_interval_max": 300,
            "ntfy_server": "https://ntfy.example",
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state.sqlite3")
            with patch("sys.argv", ["subot.py", "--once", "--dry-run"]), patch(
                "subot_core.cli.STATE_PATH", state_path
            ):
                self.assertEqual(cli.main(), 0)

            self.assertFalse(os.path.exists(state_path))

    @patch("subot_core.cli.StateStore")
    def test_worker_done_requires_successful_terminal_outcomes(self, state_store):
        store = state_store.return_value.__enter__.return_value
        store.once_successful.return_value = False

        self.assertFalse(cli.queue_is_successful(cli.STATE_PATH, 4))

        state_store.assert_called_once_with(cli.STATE_PATH, read_only=True)
        store.once_successful.assert_called_once_with(4)

    @patch("subot_core.cli.StateStore")
    def test_queue_failure_boundary_reads_existing_state_read_only(self, state_store):
        store = state_store.return_value.__enter__.return_value
        store.failure_boundary.return_value = 4

        with patch("subot_core.cli.os.path.exists", return_value=True):
            self.assertEqual(cli.queue_failure_boundary(cli.STATE_PATH), 4)

        state_store.assert_called_once_with(cli.STATE_PATH, read_only=True)
        store.failure_boundary.assert_called_once_with()

    def test_queue_failure_boundary_does_not_create_missing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state.sqlite3")
            self.assertEqual(cli.queue_failure_boundary(state_path), 0)
            self.assertFalse(os.path.exists(state_path))


class DryRunPollingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "poll_interval_min": 300,
            "poll_interval_max": 300,
        }
        self.search_urls = [
            "https://example.test/one",
            "https://example.test/two",
        ]

    @patch("subot_core.cli.log_summary")
    @patch("subot_core.cli.run_queued_search")
    def test_once_uses_queue_aware_polling_for_every_search(
        self, run_queued_search, log_summary
    ):
        run_queued_search.side_effect = [
            CycleStats(matched=1),
            CycleStats(failures=1),
        ]
        store = object()

        self.assertEqual(
            cli.run_dry_run_once(self.search_urls, store),
            1,
        )

        self.assertEqual(
            [call.args for call in run_queued_search.call_args_list],
            [
                (self.search_urls[0], 1, 2, store, True),
                (self.search_urls[1], 2, 2, store, True),
            ],
        )
        self.assertEqual(log_summary.call_count, 2)

    @patch("subot_core.cli.next_sleep", side_effect=RuntimeError("stop"))
    @patch("subot_core.cli.run_queued_search", return_value=CycleStats())
    def test_continuous_uses_queue_aware_polling_before_rescheduling(
        self, run_queued_search, next_sleep
    ):
        with self.assertRaisesRegex(RuntimeError, "stop"):
            cli.run_dry_run_continuously(self.cfg, self.search_urls, object())

        run_queued_search.assert_called_once_with(
            self.search_urls[0],
            1,
            2,
            unittest.mock.ANY,
            True,
        )
        next_sleep.assert_called_once_with(self.cfg)


if __name__ == "__main__":
    unittest.main()
