import json
import unittest
from unittest.mock import patch

from subot_core import subito


class ExtractItemsTests(unittest.TestCase):
    def test_uses_only_unique_primary_results(self):
        original = {
            "urn": "urn:listing:1",
            "urls": {"default": "https://example.test/1"},
        }
        data = {
            "props": {
                "pageProps": {
                    "initialState": {
                        "items": {
                            "originalList": [original, original],
                            "galleryList": [{"id": "gallery"}],
                            "boostedItems": [{"id": "sponsored"}],
                        }
                    }
                }
            }
        }
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            f"{json.dumps(data)}"
            "</script>"
        )

        self.assertEqual(
            subito.extract_items(html),
            [original],
        )

    def test_missing_next_data_is_a_parse_error(self):
        with self.assertRaisesRegex(ValueError, "__NEXT_DATA__"):
            subito.extract_items("<html></html>")


class FetchPageTests(unittest.TestCase):
    @patch("subot_core.subito.requests.get")
    def test_impersonates_chrome(self, get):
        get.return_value.text = "html"

        self.assertEqual(subito.fetch_page("https://example.test/search"), "html")

        get.assert_called_once_with(
            "https://example.test/search",
            headers={"Accept-Language": "it-IT,it;q=0.9,en;q=0.8"},
            timeout=30,
            impersonate="chrome",
        )
        get.return_value.raise_for_status.assert_called_once_with()


class ParseItemTests(unittest.TestCase):
    def test_normalizes_subito_item_with_namespaced_id(self):
        item = {
            "urn": "urn:subito:item:list:123",
            "subject": "iPhone 13",
            "urls": {"default": "https://www.subito.it/listing-123.htm"},
            "features": {"/price": {"values": [{"key": "200"}]}},
        }

        self.assertEqual(
            subito.parse_item(item),
            {
                "id": "subito:123",
                "subject": "iPhone 13",
                "price": 200.0,
                "url": "https://www.subito.it/listing-123.htm",
                "town": "",
                "city": "",
            },
        )
