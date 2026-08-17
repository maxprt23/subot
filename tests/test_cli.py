import unittest
from unittest.mock import patch

from curl_cffi.requests.exceptions import RequestException

from subot_core import cli, runner
from subot_core.models import CycleStats, SeenState
from tests.helpers import raw_item


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
