import json
import unittest

from utils.steam_checker import detect_status


class SteamCheckerParserTests(unittest.TestCase):
    def test_classic_not_craftable_from_g_rgassets_descriptions(self) -> None:
        html = """
        <script>
        var g_rgAssets = {
          "440": {
            "2": {
              "123": {
                "appid": 440,
                "name": "A Distinctive Lack of Hue",
                "market_name": "A Distinctive Lack of Hue",
                "market_hash_name": "A Distinctive Lack of Hue",
                "type": "Level 5 Paint Can",
                "descriptions": [
                  {"value": "Paint Color: A Distinctive Lack of Hue"},
                  {"value": "( Not Usable in Crafting )"}
                ]
              }
            }
          }
        };
        var g_rgListingInfo = {
          "listing1": {
            "asset": {"appid": 440, "contextid": "2", "id": "123", "amount": "1"}
          }
        };
        </script>
        """

        result = detect_status(html, "A Distinctive Lack of Hue")

        self.assertEqual(result.status, "NOT_CRAFTABLE")
        self.assertEqual(result.parse_method, "classic:g_rgAssets")
        self.assertTrue(result.found_not_craftable)

    def test_beta_craftable_from_render_context_descriptions(self) -> None:
        query_data = {
            "queries": [
                {
                    "state": {
                        "data": {
                            "pages": [
                                {
                                    "listings": [
                                        {
                                            "description": {
                                                "appid": 440,
                                                "name": "Team Spirit",
                                                "market_name": "Team Spirit",
                                                "market_hash_name": "Team Spirit",
                                                "type": "Level 5 Paint Can",
                                                "descriptions": [
                                                    {"value": "Paint Color: Team Spirit"}
                                                ],
                                            }
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                }
            ]
        }
        render_context = {"queryData": json.dumps(query_data)}
        encoded = json.dumps(json.dumps(render_context))
        html = f"<script>window.SSR.renderContext = JSON.parse({encoded});</script>"

        result = detect_status(html, "Team Spirit")

        self.assertEqual(result.status, "CRAFTABLE")
        self.assertEqual(result.parse_method, "beta:renderContext")
        self.assertFalse(result.found_not_craftable)

    def test_parse_fail_is_unknown(self) -> None:
        result = detect_status("<html><body>No market data here.</body></html>", "Pink as Hell")

        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(result.parse_method, "parser_failed")


if __name__ == "__main__":
    unittest.main()
