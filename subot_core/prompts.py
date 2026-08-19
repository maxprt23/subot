"""Load the tracked LLM prompt policy files."""

from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system.md"
RULES_PATH = PROMPTS_DIR / "rules.md"


def _read_prompt(name, path):
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"cannot read {name} prompt: {path}") from error
    if not content.strip():
        raise ValueError(f"{name} prompt must not be empty")
    return content


def load_llm_prompts():
    """Return the validated system prompt and notification rules."""

    return (
        _read_prompt("system", SYSTEM_PROMPT_PATH),
        _read_prompt("rules", RULES_PATH),
    )
