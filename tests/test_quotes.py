import time
import unittest
from unittest.mock import AsyncMock, patch

import quotes


class SymbolTests(unittest.TestCase):
    def test_hong_kong_aliases_are_canonicalized(self) -> None:
        for raw in ("700", "0700", "00700", "0700.HK", "HK00700"):
            with self.subTest(raw=raw):
                self.assertEqual(quotes.normalize_symbol(raw), "00700")
                self.assertEqual(quotes.detect_market(raw), "HK")
                self.assertEqual(quotes.market_currency(raw), "HKD")

    def test_unambiguous_otc_fund_codes_are_detected(self) -> None:
        self.assertEqual(quotes.normalize_symbol("010042"), "010042")
        self.assertEqual(quotes.detect_market("010042"), "FUND")
        self.assertEqual(quotes.market_candidates("010042"), frozenset({"FUND"}))
        self.assertEqual(quotes.market_currency("010042"), "CNY")

    def test_shared_code_segments_are_exposed_as_ambiguous(self) -> None:
        self.assertEqual(
            quotes.market_candidates("002714"), frozenset({"A", "FUND"})
        )
        self.assertEqual(quotes.detect_market("002714"), "A")

    def test_beijing_exchange_code_segments_are_detected(self) -> None:
        for raw in ("430047", "830799", "871970", "920118", "BJ920118", "920118.BJ"):
            with self.subTest(raw=raw):
                self.assertEqual(quotes.detect_market(raw), "BSE")
                self.assertEqual(quotes.market_candidates(raw), frozenset({"BSE"}))

    def test_japanese_stock_requires_an_explicit_jp_qualifier(self) -> None:
        for raw in ("7203.JP", "jp7203", "JP7203"):
            with self.subTest(raw=raw):
                self.assertEqual(quotes.normalize_symbol(raw), "7203")
                self.assertEqual(quotes.detect_market(raw), "JP")
                self.assertEqual(quotes.market_candidates(raw), frozenset({"JP"}))
                self.assertEqual(quotes.market_currency(raw), "JPY")
                self.assertEqual(quotes.yahoo_symbol(raw, "JP"), "7203.T")

        self.assertEqual(quotes.normalize_symbol("7203"), "07203")
        self.assertEqual(quotes.detect_market("7203"), "HK")

    def test_japan_alternative_is_only_offered_for_unqualified_hk_like_codes(
        self
    ) -> None:
        self.assertEqual(quotes.japan_alternative_code("7203"), "7203")
        self.assertIsNone(quotes.japan_alternative_code("00700"))
        self.assertIsNone(quotes.japan_alternative_code("7203.HK"))
        self.assertIsNone(quotes.japan_alternative_code("7203.JP"))

    def test_us_ticker_hkd_remains_us_ticker(self) -> None:
        self.assertEqual(quotes.normalize_symbol("HKD"), "HKD")
        self.assertEqual(quotes.detect_market("HKD"), "US")
        self.assertEqual(quotes.market_currency("HKD"), "USD")


class SinaParserTests(unittest.TestCase):
    def test_hong_kong_layout_uses_current_price_field(self) -> None:
        fields = [
            "TENCENT", "腾讯控股", "470.000", "471.800", "479.800",
            "462.000", "475.200", "3.400",
        ]
        quote = quotes._parse_sina_fields("00700", fields)
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.market, "HK")
        self.assertEqual(quote.name, "腾讯控股")
        self.assertEqual(quote.open, 470.0)
        self.assertEqual(quote.prev_close, 471.8)
        self.assertEqual(quote.price, 475.2)
        self.assertEqual(quotes.quote_currency(quote), "HKD")

    def test_beijing_exchange_uses_sina_bj_code(self) -> None:
        self.assertEqual(quotes._sina_list("920118"), "bj920118")
        self.assertEqual(quotes._sina_list("920118.BSE"), "bj920118")

    def test_a_share_layout_is_unchanged(self) -> None:
        fields = ["浦发银行", "9.590", "9.710", "9.390", "9.590", "9.280"]
        quote = quotes._parse_sina_fields("600000", fields)
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.market, "A")
        self.assertEqual(quote.price, 9.39)
        self.assertEqual(quote.prev_close, 9.71)

    def test_xiaomi_quote_matches_buy_example(self) -> None:
        fields = [
            "XIAOMI-W", "小米集团－Ｗ", "29.300", "31.040", "29.500",
            "27.760", "28.900", "-2.140",
        ]
        quote = quotes._parse_sina_fields("01810", fields)
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.name, "小米集团－Ｗ")
        self.assertEqual(quote.price, 28.9)
        self.assertEqual(quote.open, 29.3)
        self.assertEqual(quote.prev_close, 31.04)

    def test_malformed_quote_is_ignored(self) -> None:
        self.assertIsNone(quotes._parse_sina_fields("00700", ["bad"]))


class YahooChartParserTests(unittest.TestCase):
    def test_parses_japanese_quote_from_v8_chart_payload(self) -> None:
        payload = {
            "meta": {
                "symbol": "7203.T",
                "longName": "Toyota Motor Corporation",
                "shortName": "TOYOTA MOTOR CORP",
                "regularMarketPrice": 3312.0,
                "chartPreviousClose": 3378.0,
                "regularMarketDayHigh": 3384.0,
                "regularMarketDayLow": 3298.0,
            },
            "indicators": {
                "quote": [
                    {
                        "open": [3348.0],
                        "high": [3384.0],
                        "low": [3298.0],
                        "close": [3312.0],
                    }
                ]
            },
        }
        quote = quotes._parse_yahoo_chart_quote("7203", "JP", payload)
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.symbol, "7203")
        self.assertEqual(quote.market, "JP")
        self.assertEqual(quote.name, "Toyota Motor Corporation")
        self.assertEqual(quote.price, 3312.0)
        self.assertEqual(quote.open, 3348.0)
        self.assertEqual(quote.prev_close, 3378.0)
        self.assertEqual(quote.high, 3384.0)
        self.assertEqual(quote.low, 3298.0)

    def test_rejects_missing_price_in_yahoo_chart_payload(self) -> None:
        self.assertIsNone(
            quotes._parse_yahoo_chart_quote("AAPL", "US", {"meta": {}})
        )


class RateTests(unittest.IsolatedAsyncioTestCase):
    async def test_currency_is_case_insensitive(self) -> None:
        quotes.clear_cache()
        quotes._rate_cache["HKD"] = (0.9, time.time())
        self.assertEqual(await quotes.get_rate("hkd"), 0.9)


class MarketIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        quotes.clear_cache()

    async def test_same_code_stock_and_fund_have_independent_cache_keys(self) -> None:
        stock = quotes.Quote(
            symbol="002714", name="牧原股份", price=42.03,
            open=42.3, prev_close=42.4, high=42.61, low=41.95,
            market="A",
        )
        fund = quotes.Quote(
            symbol="002714", name="鹏华金城混合D", price=1.37,
            open=1.3711, prev_close=1.3711, high=1.3711, low=1.37,
            market="FUND",
        )
        with (
            patch.object(
                quotes, "_fetch_sina", AsyncMock(return_value={"002714": stock})
            ),
            patch.object(
                quotes, "_fetch_funds", AsyncMock(return_value={"002714": fund})
            ),
        ):
            fetched = await quotes.get_quotes(
                [("002714", "A"), ("002714", "FUND")]
            )

        self.assertIs(fetched[("002714", "A")], stock)
        self.assertIs(fetched[("002714", "FUND")], fund)
        self.assertEqual(quotes._quote_cache[("002714", "A")][0].market, "A")
        self.assertEqual(
            quotes._quote_cache[("002714", "FUND")][0].market, "FUND"
        )

    async def test_missing_hk_quote_offers_japan_alternative(self) -> None:
        japan = quotes.Quote(
            symbol="7203", name="Toyota Motor Corporation", price=3312.0,
            open=3348.0, prev_close=3378.0, high=3384.0, low=3298.0,
            market="JP",
        )
        with patch.object(
            quotes, "get_quote", AsyncMock(return_value=japan)
        ) as get_quote:
            alternative = await quotes.get_japan_alternative("7203")

        self.assertIs(alternative, japan)
        get_quote.assert_awaited_once_with("7203.JP")

    async def test_fund_fallback_cannot_satisfy_an_a_share_request(self) -> None:
        fund = quotes.Quote(
            symbol="002714", name="鹏华金城混合D", price=1.37,
            open=1.3711, prev_close=1.3711, high=1.3711, low=1.37,
            market="FUND",
        )
        with (
            patch.object(quotes, "_fetch_sina", AsyncMock(return_value={})),
            patch.object(
                quotes, "_fetch_funds", AsyncMock(return_value={"002714": fund})
            ),
        ):
            stock = await quotes.get_quote("002714.A")
            cached_fund = await quotes.get_quote("002714.F")

        self.assertIsNone(stock)
        self.assertIs(cached_fund, fund)


if __name__ == "__main__":
    unittest.main()
