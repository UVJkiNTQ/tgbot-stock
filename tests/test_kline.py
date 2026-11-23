import unittest
from datetime import date

from kline import (
    DailyBar,
    DailySeries,
    _parse_sina_rows,
    _parse_tencent_rows,
    _parse_yahoo_chart,
    render_chart,
)


class TencentDailyParseTests(unittest.TestCase):
    def test_rows_map_date_open_close_high_low_volume(self) -> None:
        rows = [
            ["2026-11-19", "10.00", "10.20", "10.30", "9.90", "12345.00"],
            ["2026-11-20", "10.20", "10.10", "10.40", "10.00", "23456.00"],
        ]
        bars = _parse_tencent_rows(rows)

        self.assertEqual([bar.day for bar in bars], [
            date(2026, 11, 19), date(2026, 11, 20)
        ])
        self.assertEqual(bars[0].open, 10.0)
        self.assertEqual(bars[0].close, 10.2)
        self.assertEqual(bars[0].high, 10.3)
        self.assertEqual(bars[0].low, 9.9)
        self.assertEqual(bars[0].volume, 12345.0)

    def test_bars_are_sorted_ascending(self) -> None:
        rows = [
            ["2026-11-20", "10", "11", "12", "9", "1"],
            ["2026-11-18", "8", "9", "10", "7", "2"],
        ]
        bars = _parse_tencent_rows(rows)

        self.assertEqual(
            [bar.day for bar in bars],
            [date(2026, 11, 18), date(2026, 11, 20)],
        )

    def test_invalid_rows_are_skipped(self) -> None:
        rows = [
            ["bad-date", "10", "11", "12", "9", "1"],
            ["2026-11-20", "0", "11", "12", "9", "1"],
            ["2026-11-21", "10", "11"],
        ]
        self.assertEqual(_parse_tencent_rows(rows), [])

    def test_high_and_low_cover_open_and_close(self) -> None:
        rows = [["2026-11-20", "10", "12", "11", "9", "1"]]
        bars = _parse_tencent_rows(rows)

        self.assertEqual(bars[0].high, 12.0)
        self.assertEqual(bars[0].low, 9.0)


class SinaDailyParseTests(unittest.TestCase):
    def test_dict_rows_are_parsed(self) -> None:
        rows = [
            {
                "day": "2026-11-20",
                "open": "10.0",
                "high": "10.5",
                "low": "9.8",
                "close": "10.2",
                "volume": "1000",
            }
        ]
        bars = _parse_sina_rows(rows)

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].day, date(2026, 11, 20))
        self.assertEqual(bars[0].close, 10.2)

    def test_malformed_rows_are_skipped(self) -> None:
        self.assertEqual(
            _parse_sina_rows([{"day": "2026-11-20"}, "nope"]), []
        )


class YahooDailyParseTests(unittest.TestCase):
    def test_parses_v8_chart_bars_and_skips_nulls(self) -> None:
        result = {
            "meta": {"exchangeTimezoneName": "America/New_York"},
            "timestamp": [1763562600, 1763649000, 1763735400],
            "indicators": {
                "quote": [
                    {
                        "open": [100.0, None, 102.0],
                        "high": [105.0, None, 106.0],
                        "low": [99.0, None, 101.0],
                        "close": [104.0, None, 105.5],
                        "volume": [1000, None, 2000],
                    }
                ]
            },
        }
        bars = _parse_yahoo_chart(result)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].open, 100.0)
        self.assertEqual(bars[0].close, 104.0)
        self.assertEqual(bars[1].volume, 2000.0)


class RenderKlineTests(unittest.TestCase):
    @staticmethod
    def _series() -> DailySeries:
        bars = [
            DailyBar(
                day=date(2026, 10, 1),
                open=10 + index * 0.1,
                high=10.3 + index * 0.1,
                low=9.8 + index * 0.1,
                close=10 + (index % 2) * 0.2,
                volume=1000 + index * 100,
            )
            for index in range(30)
        ]
        return DailySeries(symbol="600000", market="A", bars=bars)

    def test_render_returns_png(self) -> None:
        png = render_chart(self._series())

        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(png), 1000)

    def test_flat_bars_do_not_crash(self) -> None:
        bars = [
            DailyBar(
                day=date(2026, 10, 1 + index),
                open=5.0,
                high=5.0,
                low=5.0,
                close=5.0,
            )
            for index in range(5)
        ]
        png = render_chart(
            DailySeries(symbol="AAPL", market="US", bars=bars)
        )

        self.assertTrue(png.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
