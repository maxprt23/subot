"""Small OpenRouter client used to decide whether a listing is noteworthy.

The client deliberately has no retry loop.  A worker owns retry policy and can
retry both transport errors and :class:`OpenRouterDecisionError` instances.
"""

import json
from collections.abc import Mapping

from curl_cffi import requests


ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
REASONING_EFFORTS = frozenset(
    ("none", "minimal", "low", "medium", "high", "xhigh", "max")
)


class OpenRouterDecisionError(RuntimeError):
    """The model response was not a valid decision response."""


class OpenRouterClient:
    """Call OpenRouter and return the model's exact ``true``/``false`` decision.

    ``system_prompt`` and ``rules`` are sent as separate system messages. The
    listing is sent as JSON data in a user message; prompt policy belongs only
    in the system messages.
    """

    def __init__(
        self,
        *,
        api_key,
        model=None,
        system_prompt,
        rules,
        web_search_max_results=5,
        web_search_max_total_results=None,
        web_fetch_max_uses=None,
        web_fetch_max_content_tokens=None,
        reasoning_effort=None,
        timeout=60.0,
        endpoint=ENDPOINT,
        model_id=None,
    ):
        # ``model_id`` is an explicit alias for configuration integrations;
        # ``model`` remains convenient for callers mirroring the API payload.
        if model is not None and model_id is not None and model != model_id:
            raise ValueError("model and model_id must match when both are set")
        selected_model = model if model is not None else model_id

        self._require_non_empty_string("api_key", api_key)
        self._require_non_empty_string("model", selected_model)
        self._require_string("system_prompt", system_prompt)
        self._require_string("rules", rules)
        self._require_non_empty_string("endpoint", endpoint)
        self._require_positive_number("timeout", timeout)

        self.api_key = api_key
        self.model = selected_model
        self.system_prompt = system_prompt
        self.rules = rules
        self.web_search_max_results = self._limit(
            "web_search_max_results", web_search_max_results
        )
        self.web_search_max_total_results = self._limit(
            "web_search_max_total_results", web_search_max_total_results
        )
        self.web_fetch_max_uses = self._limit("web_fetch_max_uses", web_fetch_max_uses)
        self.web_fetch_max_content_tokens = self._limit(
            "web_fetch_max_content_tokens", web_fetch_max_content_tokens
        )
        self.reasoning_effort = self._reasoning_effort(reasoning_effort)
        self.timeout = timeout
        self.endpoint = endpoint

    @staticmethod
    def _require_string(name, value):
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")

    @classmethod
    def _require_non_empty_string(cls, name, value):
        cls._require_string(name, value)
        if not value.strip():
            raise ValueError(f"{name} must not be empty")

    @staticmethod
    def _require_positive_number(name, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a positive number")
        if value <= 0:
            raise ValueError(f"{name} must be a positive number")

    @staticmethod
    def _limit(name, value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer or null")
        return value

    @staticmethod
    def _reasoning_effort(value):
        if value is None:
            return None
        if not isinstance(value, str) or value not in REASONING_EFFORTS:
            allowed = ", ".join(sorted(REASONING_EFFORTS))
            raise ValueError(f"reasoning_effort must be null or one of: {allowed}")
        return value

    def _tools(self):
        search_parameters = {
            name: value
            for name, value in (
                ("max_results", self.web_search_max_results),
                ("max_total_results", self.web_search_max_total_results),
            )
            if value is not None
        }
        fetch_parameters = {
            name: value
            for name, value in (
                ("max_uses", self.web_fetch_max_uses),
                ("max_content_tokens", self.web_fetch_max_content_tokens),
            )
            if value is not None
        }
        return [
            {
                "type": "openrouter:web_search",
                "parameters": search_parameters,
            },
            {
                "type": "openrouter:web_fetch",
                "parameters": fetch_parameters,
            },
        ]

    @staticmethod
    def _listing_content(listing):
        if not isinstance(listing, Mapping):
            raise OpenRouterDecisionError("listing must be a mapping")
        try:
            encoded = json.dumps(
                dict(listing), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as error:
            raise OpenRouterDecisionError("listing cannot be encoded") from error

        return encoded

    def payload_for(self, listing):
        """Build the request payload for *listing* without making a request."""

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "system", "content": self.rules},
                {"role": "user", "content": self._listing_content(listing)},
            ],
            "tools": self._tools(),
        }
        if self.reasoning_effort is not None:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        return payload

    @staticmethod
    def _content_from_response(response):
        try:
            data = response.json()
        except (TypeError, ValueError) as error:
            raise OpenRouterDecisionError("OpenRouter returned invalid JSON") from error

        if not isinstance(data, dict):
            raise OpenRouterDecisionError("OpenRouter response has no choices")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OpenRouterDecisionError("OpenRouter response has no choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise OpenRouterDecisionError("OpenRouter response has no message")
        message = first.get("message")
        if not isinstance(message, dict):
            raise OpenRouterDecisionError("OpenRouter response has no message")
        content = message.get("content")
        if not isinstance(content, str):
            raise OpenRouterDecisionError("OpenRouter response has no text content")
        return content

    def decide(self, listing):
        """Return ``True`` or ``False`` for *listing*.

        HTTP/client exceptions are intentionally allowed to propagate.  The
        worker can then apply its retry policy without this client hiding the
        distinction between transport failure and an invalid model response.
        """

        response = requests.post(
            self.endpoint,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=self.payload_for(listing),
            timeout=self.timeout,
        )
        response.raise_for_status()
        content = self._content_from_response(response)
        if content == "true":
            return True
        if content == "false":
            return False
        raise OpenRouterDecisionError("OpenRouter response was not true or false")


__all__ = ["ENDPOINT", "OpenRouterClient", "OpenRouterDecisionError"]
