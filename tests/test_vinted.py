import json
import unittest
from unittest.mock import patch

from subot_core import vinted


def catalog_html(items):
    payload = {"items": items, "pagination": {"current_page": 1}}
    return (
        '<script type="application/json" data-page="catalog">'
        + json.dumps(payload)
        + "</script>"
    )


class ExtractItemsTests(unittest.TestCase):
    def test_extracts_and_deduplicates_catalog_items(self):
        item = {
            "id": 1234567890,
            "title": "iPhone 13",
            "price": {"amount": "250.00", "currency_code": "EUR"},
            "url": "/items/1234567890-iphone-13",
        }

        self.assertEqual(
            vinted.extract_items(catalog_html([item, dict(item)])),
            [item],
        )

    def test_extracts_items_from_next_rsc_payload(self):
        item = {
            "id": "9876543210",
            "title": "MacBook",
            "price": {"amount": "899,99", "currency_code": "EUR"},
            "url": "/items/9876543210-macbook",
        }
        rsc = {
            "initialCatalogState": {
                "items": {
                    "items": [item],
                    "pagination": {"current_page": 1},
                }
            }
        }
        # Next App Router Flight data stores the JSON in a JSON-encoded string.
        rsc_text = json.dumps("7:" + json.dumps(rsc, ensure_ascii=False))
        html = f'<script>self.__next_f.push([1,{rsc_text}])</script>'

        items = vinted.extract_items(html)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "9876543210")
        self.assertEqual(items[0]["title"], "MacBook")

    def test_ignores_item_collections_outside_initial_catalog_state(self):
        catalog_item = {
            "id": 42,
            "title": "Matching iPhone",
            "price": {"amount": "100", "currency_code": "EUR"},
            "url": "/items/42-matching-iphone",
        }
        recommendation = {
            "id": 99,
            "title": "Unrelated recommendation",
            "price": {"amount": "200", "currency_code": "EUR"},
            "url": "/items/99-unrelated-recommendation",
        }
        rsc = {
            "initialCatalogState": {
                "items": {
                    "items": [catalog_item],
                    "pagination": {"current_page": 1},
                }
            },
            "recommendations": {
                "items": [recommendation],
                "pagination": {"current_page": 1},
            },
        }
        rsc_text = json.dumps("7:" + json.dumps(rsc))
        html = f'<script>self.__next_f.push([1,{rsc_text}])</script>'

        self.assertEqual(vinted.extract_items(html), [catalog_item])

    def test_preserves_html_entities_inside_json_string_values(self):
        item = {
            "id": 123,
            "title": "Phone &quot;special edition&quot;",
            "price": {"amount": "100", "currency_code": "EUR"},
            "url": "/items/123-phone",
        }

        self.assertEqual(vinted.extract_items(catalog_html([item])), [item])

    def test_accepts_category_payload_without_category_specific_assumptions(self):
        item = {
            "id": 42,
            "title": "Game console",
            "price": {"amount": 120, "currency_code": "EUR"},
            "url": "/items/42-game-console",
        }

        self.assertEqual(vinted.extract_items(catalog_html([item]))[0], item)

    def test_root_listing_shaped_json_is_not_accepted_without_catalog_context(self):
        item = {
            "id": 42,
            "title": "Unrelated object",
            "price": {"amount": "120", "currency_code": "EUR"},
            "url": "/items/42-unrelated-object",
        }
        html = '<script type="application/json">' + json.dumps(item) + "</script>"

        with self.assertRaises(ValueError):
            vinted.extract_items(html)

    def test_incomplete_or_mismatched_catalog_record_is_a_parse_error(self):
        records = [
            {
                "id": 42,
                "title": "No URL",
                "price": {"amount": "120", "currency_code": "EUR"},
            },
            {
                "id": 42,
                "title": "Wrong URL",
                "price": {"amount": "120", "currency_code": "EUR"},
                "url": "/items/99-wrong-url",
            },
        ]
        for record in records:
            with self.subTest(record=record):
                with self.assertRaises(ValueError):
                    vinted.extract_items(catalog_html([record]))

    def test_rejects_absolute_item_url_on_untrusted_host(self):
        item = {
            "id": 42,
            "title": "Untrusted URL",
            "price": {"amount": "120", "currency_code": "EUR"},
            "url": "https://evil.example/items/42-untrusted-url",
        }

        with self.assertRaises(ValueError):
            vinted.extract_items(catalog_html([item]))

    def test_rejects_absolute_item_url_with_invalid_port(self):
        item = {
            "id": 42,
            "title": "Invalid port",
            "price": {"amount": "120", "currency_code": "EUR"},
            "url": "https://www.vinted.it:not-a-port/items/42-invalid-port",
        }

        with self.assertRaises(ValueError):
            vinted.extract_items(catalog_html([item]))

    def test_verified_empty_catalog_wins_over_generic_challenge_text(self):
        html = catalog_html([]) + "<p>search_text=just a moment</p>"

        self.assertEqual(vinted.extract_items(html), [])

    def test_deeply_nested_malformed_payload_is_a_value_error(self):
        depth = 1200
        nested = "[" * depth + "]" * depth
        html = (
            '<script type="application/json">{"items": '
            + nested
            + "}</script>"
        )

        with self.assertRaises(ValueError):
            vinted.extract_items(html)

    def test_challenge_or_malformed_page_is_a_parse_error(self):
        for html in (
            "<html><title>Just a moment...</title>Enable JavaScript and cookies</html>",
            "<html><body>not a Vinted catalog</body></html>",
            '<script type="application/json">{"items": []}</script>'
            "<title>Just a moment...</title>",
            catalog_html([{"unexpected": "shape"}]),
        ):
            with self.subTest(html=html):
                with self.assertRaises(ValueError):
                    vinted.extract_items(html)


class ParseItemTests(unittest.TestCase):
    def test_normalizes_vinted_item(self):
        item = {
            "id": 123,
            "title": "iPhone 13",
            "price": {"amount": "1.234,56", "currency_code": "EUR"},
            "url": "/items/123-iphone-13",
        }

        self.assertEqual(
            vinted.parse_item(item),
            {
                "id": "vinted:123",
                "subject": "iPhone 13",
                "price": 1234.56,
                "url": "https://www.vinted.it/items/123-iphone-13",
                "town": "",
                "city": "",
            },
        )

    def test_non_positive_or_unparseable_price_is_none(self):
        base = {
            "id": 123,
            "title": "Item",
            "url": "/items/123-item",
        }
        for price in (0, -1, "not-a-price"):
            with self.subTest(price=price):
                item = dict(base, price={"amount": price})
                self.assertIsNone(vinted.parse_item(item)["price"])

    def test_non_finite_price_is_none(self):
        item = {
            "id": 123,
            "title": "Item",
            "price": {"amount": float("nan")},
            "url": "/items/123-item",
        }
        for amount in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(amount=amount):
                item["price"] = {"amount": amount}
                self.assertIsNone(vinted.parse_item(item)["price"])

    def test_price_parser_handles_grouped_integers_and_decimal_prices(self):
        expected_prices = {
            "1.234": 1234.0,
            "12.345": 12345.0,
            "1,234": 1234.0,
            "1.234.567": 1234567.0,
            "1,234,567": 1234567.0,
            "89.00": 89.0,
            "89,00": 89.0,
            "1.234,56": 1234.56,
        }
        for amount, expected in expected_prices.items():
            with self.subTest(amount=amount):
                item = {
                    "id": 123,
                    "title": "Item",
                    "price": {"amount": amount},
                    "url": "/items/123-item",
                }
                self.assertEqual(vinted.parse_item(item)["price"], expected)

    def test_does_not_fabricate_url_for_item_without_valid_item_url(self):
        self.assertIsNone(
            vinted.parse_item(
                {
                    "id": 123,
                    "title": "Item",
                    "price": {"amount": "10"},
                }
            )
        )

    def test_parse_item_rejects_absolute_item_url_on_untrusted_host(self):
        self.assertIsNone(
            vinted.parse_item(
                {
                    "id": 123,
                    "title": "Item",
                    "price": {"amount": "10"},
                    "url": "https://evil.example/items/123-item",
                }
            )
        )

    def test_parse_item_rejects_absolute_item_url_with_invalid_port(self):
        self.assertIsNone(
            vinted.parse_item(
                {
                    "id": 123,
                    "title": "Item",
                    "price": {"amount": "10"},
                    "url": "https://www.vinted.it:not-a-port/items/123-item",
                }
            )
        )

    def test_missing_id_cannot_be_parsed(self):
        self.assertIsNone(
            vinted.parse_item(
                {
                    "title": "Item",
                    "price": {"amount": "10"},
                    "url": "/items/not-an-id-item",
                }
            )
        )


class FetchPageTests(unittest.TestCase):
    @patch("subot_core.vinted.requests.get")
    def test_impersonates_chrome(self, get):
        get.return_value.text = "html"

        self.assertEqual(vinted.fetch_page("https://www.vinted.it/catalog"), "html")

        get.assert_called_once_with(
            "https://www.vinted.it/catalog",
            headers={"Accept-Language": "it-IT,it;q=0.9,en;q=0.8"},
            timeout=30,
            impersonate="chrome",
        )
        get.return_value.raise_for_status.assert_called_once_with()
