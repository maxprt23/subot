import json
import os
import tempfile
import unittest
from unittest.mock import patch

from subot_core import state
from subot_core.models import SeenState


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
