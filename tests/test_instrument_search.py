import unittest
from unittest.mock import AsyncMock, patch

import instrument_search


class YahooResultTests(unittest.TestCase):
    def test_japanese_listing_maps_to_jp_suffix(self) -> None:
        result = instrument_search._yahoo_result(
            {
                "quoteType": "EQUITY",
                "symbol": "6727.T",
                "exchange": "JPX",
                "exchDisp": "Tokyo Stock Exchange",
                "longname": "Wacom Co., Ltd.",
            }
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.display_symbol(), "6727.JP")
        self.assertEqual(result.name, "Wacom Co., Ltd.")
        self.assertEqual(result.rank, 0)

    def test_hong_kong_and_a_share_symbols_are_canonicalized(self) -> None:
        hk = instrument_search._yahoo_result(
            {
                "quoteType": "EQUITY",
                "symbol": "0700.HK",
                "exchange": "HKG",
                "longname": "Tencent Holdings Limited",
            }
        )
        sh = instrument_search._yahoo_result(
            {
                "quoteType": "EQUITY",
                "symbol": "600519.SS",
                "exchange": "SHH",
                "longname": "Kweichow Moutai Co., Ltd.",
            }
        )

        assert hk is not None and sh is not None
        self.assertEqual(hk.display_symbol(), "00700.HK")
        self.assertEqual(sh.display_symbol(), "600519.A")

    def test_us_listings_are_kept_and_unsupported_markets_are_skipped(self) -> None:
        us = instrument_search._yahoo_result(
            {
                "quoteType": "EQUITY",
                "symbol": "TM",
                "exchange": "NYQ",
                "longname": "Toyota Motor Corporation",
            }
        )
        otc = instrument_search._yahoo_result(
            {
                "quoteType": "EQUITY",
                "symbol": "WACMY",
                "exchange": "PNK",
                "longname": "Wacom Co., Ltd.",
            }
        )
        german = instrument_search._yahoo_result(
            {
                "quoteType": "EQUITY",
                "symbol": "11W.MU",
                "exchange": "MUN",
                "longname": "Wacom Co Ltd",
            }
        )
        index = instrument_search._yahoo_result(
            {
                "quoteType": "INDEX",
                "symbol": "^N225",
                "exchange": "OSA",
                "longname": "Nikkei 225",
            }
        )

        assert us is not None and otc is not None
        self.assertEqual(us.display_symbol(), "TM.US")
        self.assertEqual(us.rank, 1)
        self.assertEqual(otc.rank, 2)
        self.assertIsNone(german)
        self.assertIsNone(index)


class TencentResultTests(unittest.TestCase):
    def test_hint_entries_are_parsed_and_non_stocks_are_filtered(self) -> None:
        text = (
            'v_hint="'
            "sh~600519~\\u8d35\\u5dde\\u8305\\u53f0~gzmt~GP-A"
            "^hk~00700~\\u817e\\u8baf\\u63a7\\u80a1~txkg~GP"
            "^hk~80700~\\u817e\\u8baf\\u63a7\\u80a1r~txkgr~GP"
            "^us~tcehy.ps~\\u817e\\u8baf\\u63a7\\u80a1(adr)~txkgadr~GP"
            "^hk~13005~\\u817e\\u8baf\\u6cd5\\u5174~txfx~QZ"
            '";'
        )

        results = instrument_search._parse_tencent_hint(text)

        self.assertEqual(
            [result.display_symbol() for result in results],
            ["600519.A", "00700.HK", "TCEHY.US"],
        )
        self.assertEqual(results[0].name, "贵州茅台")
        self.assertEqual(results[1].name, "腾讯控股")

    def test_empty_hint_returns_no_results(self) -> None:
        self.assertEqual(instrument_search._parse_tencent_hint('v_hint="N";'), [])


class FinalizeTests(unittest.TestCase):
    def test_primary_listing_wins_and_duplicate_otc_names_are_dropped(self) -> None:
        results = [
            instrument_search.SearchResult(
                "WACMY", "US", "Wacom Co., Ltd.", "OTC Markets", rank=2
            ),
            instrument_search.SearchResult(
                "6727", "JP", "Wacom Co., Ltd.", "Tokyo", rank=0
            ),
            instrument_search.SearchResult(
                "WACMF", "US", "Wacom Co., Ltd.", "OTC Markets", rank=2
            ),
        ]

        final = instrument_search._finalize(results, 5)

        self.assertEqual(
            [result.display_symbol() for result in final],
            ["6727.JP", "WACMY.US"],
        )


class ProviderSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        instrument_search.clear_cache()

    async def test_ascii_query_uses_yahoo_first(self) -> None:
        yahoo = AsyncMock(
            return_value=[
                instrument_search.SearchResult("6727", "JP", "Wacom")
            ]
        )
        tencent = AsyncMock()
        with (
            patch.object(instrument_search, "_yahoo_search", yahoo),
            patch.object(instrument_search, "_tencent_search", tencent),
        ):
            results = await instrument_search.search_instruments("wacom")

        yahoo.assert_awaited_once_with("wacom")
        tencent.assert_not_awaited()
        self.assertEqual(results[0].display_symbol(), "6727.JP")

    async def test_cjk_query_uses_tencent(self) -> None:
        yahoo = AsyncMock()
        tencent = AsyncMock(
            return_value=[
                instrument_search.SearchResult("00700", "HK", "腾讯控股")
            ]
        )
        with (
            patch.object(instrument_search, "_yahoo_search", yahoo),
            patch.object(instrument_search, "_tencent_search", tencent),
        ):
            results = await instrument_search.search_instruments("腾讯")

        tencent.assert_awaited_once_with("腾讯")
        yahoo.assert_not_awaited()
        self.assertEqual(results[0].display_symbol(), "00700.HK")

    async def test_generic_etf_only_query_expands_to_company_name(self) -> None:
        etf = instrument_search.SearchResult(
            "QYLD", "US", "Covered Call ETF", "NASDAQ", rank=1,
            quote_type="ETF",
        )
        equity = instrument_search.SearchResult(
            "5253", "JP", "COVER Corporation", "Tokyo", rank=0,
        )
        yahoo = AsyncMock(side_effect=[[etf], [equity]])
        with (
            patch.object(instrument_search, "_yahoo_search", yahoo),
            patch.object(instrument_search, "_tencent_search", AsyncMock()),
        ):
            results = await instrument_search.search_instruments("cover", 2)

        self.assertEqual(
            [result.display_symbol() for result in results],
            ["5253.JP"],
        )
        self.assertEqual(
            [call.args[0] for call in yahoo.await_args_list],
            ["cover", "cover corp"],
        )

    async def test_all_provider_failures_raise(self) -> None:
        with (
            patch.object(
                instrument_search, "_yahoo_search",
                AsyncMock(side_effect=RuntimeError("yahoo")),
            ),
            patch.object(
                instrument_search, "_tencent_search",
                AsyncMock(side_effect=RuntimeError("tencent")),
            ),
        ):
            with self.assertRaises(instrument_search.SearchUnavailableError):
                await instrument_search.search_instruments("wacom")


if __name__ == "__main__":
    unittest.main()
