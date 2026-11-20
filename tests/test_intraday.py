import unittest
from datetime import datetime

from intraday import (
    Intraday,
    IntradayPoint,
    _parse_tencent_rows,
    render_chart,
)


class TencentMinuteParseTests(unittest.TestCase):
    def test_cumulative_volume_becomes_per_minute_volume(self) -> None:
        rows = [
            "0930 10.00 100 1000",
            "0931 11.00 300 3200",
            "0932 10.50 400 4200",
        ]
        points = _parse_tencent_rows(rows, "20261120")

        self.assertEqual([p.price for p in points], [10.0, 11.0, 10.5])
        self.assertEqual([p.volume for p in points], [100.0, 200.0, 100.0])
        self.assertEqual(points[0].time, datetime(2026, 11, 20, 9, 30))
        self.assertAlmostEqual(points[0].avg, 10.0)
        self.assertAlmostEqual(
            points[2].avg, (10 * 100 + 11 * 200 + 10.5 * 100) / 400
        )

    def test_rows_without_volume_still_parse(self) -> None:
        rows = ["0930 15.78 1", "0931 15.60 8"]
        points = _parse_tencent_rows(rows, "20261120")

        self.assertEqual([p.price for p in points], [15.78, 15.6])
        self.assertEqual([p.volume for p in points], [1.0, 7.0])

    def test_invalid_rows_are_skipped(self) -> None:
        rows = ["bad", "0930 abc 1", "0931 10.0 5", "0932 -1 6"]
        points = _parse_tencent_rows(rows, "20261120")

        self.assertEqual([p.price for p in points], [10.0])

    def test_invalid_date_returns_no_points(self) -> None:
        self.assertEqual(
            _parse_tencent_rows(["0930 10.0 1"], "not-a-date"), []
        )


class RenderChartTests(unittest.TestCase):
    def test_render_returns_png_bytes(self) -> None:
        points = [
            IntradayPoint(
                time=datetime(2026, 11, 20, 9, 30 + index),
                price=10 + index * 0.05,
                avg=10 + index * 0.03,
                volume=100 + index * 10,
            )
            for index in range(20)
        ]
        png = render_chart(
            Intraday(
                symbol="600000",
                market="A",
                prev_close=10.2,
                points=points,
            )
        )

        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(png), 1000)

    def test_flat_series_does_not_crash(self) -> None:
        points = [
            IntradayPoint(
                time=datetime(2026, 11, 20, 9, 30 + index),
                price=5.0,
                avg=5.0,
            )
            for index in range(5)
        ]
        png = render_chart(
            Intraday(
                symbol="AAPL",
                market="US",
                prev_close=5.0,
                points=points,
            )
        )

        self.assertTrue(png.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
