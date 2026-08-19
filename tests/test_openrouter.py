import unittest
from unittest.mock import Mock, patch

from curl_cffi.requests.exceptions import RequestException

from subot_core.openrouter import OpenRouterClient, OpenRouterDecisionError


class OpenRouterClientTests(unittest.TestCase):
    def client(self, **overrides):
        settings = {
            "api_key": "secret-key",
            "model": "provider/model",
            "system_prompt": "You are a careful listing filter.",
            "rules": "Notify only when the listing meets my criteria.",
            "web_search_max_results": 3,
            "web_search_max_total_results": 7,
            "web_fetch_max_uses": 2,
            "web_fetch_max_content_tokens": 12000,
        }
        settings.update(overrides)
        return OpenRouterClient(**settings)

    @staticmethod
    def response(content):
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": content}}]
        }
        response.raise_for_status.return_value = None
        return response

    @patch("subot_core.openrouter.requests.post")
    def test_decide_sends_listing_url_and_configured_tools(self, post):
        post.return_value = self.response("true")
        listing = {
            "id": "123",
            "subject": "Camera",
            "price": 250,
            "url": "https://www.subito.it/annunci/123",
        }

        result = self.client().decide(listing)

        self.assertTrue(result)
        post.assert_called_once()
        call = post.call_args
        self.assertEqual(
            call.args[0], "https://openrouter.ai/api/v1/chat/completions"
        )
        self.assertEqual(call.kwargs["headers"], {
            "Authorization": "Bearer secret-key",
            "Content-Type": "application/json",
        })
        self.assertEqual(call.kwargs["timeout"], 60.0)
        payload = call.kwargs["json"]
        self.assertEqual(payload["model"], "provider/model")
        self.assertEqual(
            [message["content"] for message in payload["messages"][:2]],
            [
                "You are a careful listing filter.",
                "Notify only when the listing meets my criteria.",
            ],
        )
        self.assertEqual(payload["messages"][2], {
            "role": "user",
            "content": (
                '{"id":"123","price":250,"subject":"Camera",'
                '"url":"https://www.subito.it/annunci/123"}'
            ),
        })
        self.assertEqual(payload["tools"], [
            {
                "type": "openrouter:web_search",
                "parameters": {
                    "max_results": 3,
                    "max_total_results": 7,
                },
            },
            {
                "type": "openrouter:web_fetch",
                "parameters": {
                    "max_uses": 2,
                    "max_content_tokens": 12000,
                },
            },
        ])

    @patch("subot_core.openrouter.requests.post")
    def test_decide_returns_false(self, post):
        post.return_value = self.response("false")

        self.assertFalse(self.client().decide({"url": "https://example.test"}))

    @patch("subot_core.openrouter.requests.post")
    def test_decide_rejects_anything_other_than_exact_lowercase_boolean(self, post):
        for content in (" true", "false\n", "TRUE", "0", "maybe", "```true```"):
            with self.subTest(content=content):
                post.return_value = self.response(content)
                with self.assertRaises(OpenRouterDecisionError):
                    self.client().decide({"url": "https://example.test"})

    @patch("subot_core.openrouter.requests.post")
    def test_decide_rejects_malformed_api_response(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"choices": []}
        post.return_value = response

        with self.assertRaises(OpenRouterDecisionError):
            self.client().decide({"url": "https://example.test"})

    @patch("subot_core.openrouter.requests.post")
    def test_http_errors_are_propagated_for_worker_retry(self, post):
        response = Mock()
        response.raise_for_status.side_effect = RequestException("503")
        post.return_value = response

        with self.assertRaises(RequestException):
            self.client().decide({"url": "https://example.test"})

    @patch("subot_core.openrouter.requests.post")
    def test_json_decode_errors_are_decision_errors(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.side_effect = ValueError("not json")
        post.return_value = response

        with self.assertRaises(OpenRouterDecisionError):
            self.client().decide({"url": "https://example.test"})

    def test_limits_must_be_positive_integers(self):
        for name in (
            "web_search_max_results",
            "web_search_max_total_results",
            "web_fetch_max_uses",
            "web_fetch_max_content_tokens",
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.client(**{name: 0})

    def test_optional_limits_are_omitted_from_tool_parameters(self):
        client = self.client(
            web_search_max_results=None,
            web_search_max_total_results=None,
            web_fetch_max_uses=None,
            web_fetch_max_content_tokens=None,
        )

        with patch("subot_core.openrouter.requests.post") as post:
            post.return_value = self.response("false")
            client.decide({"url": "https://example.test"})

        self.assertEqual(post.call_args.kwargs["json"]["tools"], [
            {"type": "openrouter:web_search", "parameters": {}},
            {"type": "openrouter:web_fetch", "parameters": {}},
        ])

    def test_reasoning_effort_is_sent_when_configured(self):
        client = self.client(reasoning_effort="high")

        payload = client.payload_for({"url": "https://example.test"})

        self.assertEqual(payload["reasoning"], {"effort": "high"})


if __name__ == "__main__":
    unittest.main()
