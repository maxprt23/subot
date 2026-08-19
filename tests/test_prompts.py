import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from subot_core import prompts


class PromptLoadingTests(unittest.TestCase):
    def test_loads_system_prompt_and_rules_from_their_tracked_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            system_path = directory_path / "system.md"
            rules_path = directory_path / "rules.md"
            system_path.write_text("System prompt.", encoding="utf-8")
            rules_path.write_text("Rules.", encoding="utf-8")

            with (
                patch.object(prompts, "SYSTEM_PROMPT_PATH", system_path),
                patch.object(prompts, "RULES_PATH", rules_path),
            ):
                self.assertEqual(
                    prompts.load_llm_prompts(),
                    ("System prompt.", "Rules."),
                )

    def test_rejects_an_empty_prompt_file(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            system_path = directory_path / "system.md"
            rules_path = directory_path / "rules.md"
            system_path.write_text("  \n", encoding="utf-8")
            rules_path.write_text("Rules.", encoding="utf-8")

            with (
                patch.object(prompts, "SYSTEM_PROMPT_PATH", system_path),
                patch.object(prompts, "RULES_PATH", rules_path),
            ):
                with self.assertRaisesRegex(
                    ValueError, "system prompt must not be empty"
                ):
                    prompts.load_llm_prompts()
