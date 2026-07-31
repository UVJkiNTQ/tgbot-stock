import unittest

from models import Side, Trade
from pnl import calculate_cost_basis


def trade(side: Side, price: float, qty: int, rate: float = 1.0) -> Trade:
    return Trade(
        id=None,
        user_id=1,
        username="tester",
        symbol="00700",
        side=side,
        price=price,
        qty=qty,
        currency="HKD",
        rate=rate,
        trade_ts="2026-01-01T00:00:00+00:00",
    )


class CostBasisTests(unittest.TestCase):
    def test_partial_sell_preserves_average_cost(self) -> None:
        trades = [
            trade(Side.BUY, 10, 100, 0.8),
            trade(Side.BUY, 20, 100, 0.9),
            trade(Side.SELL, 30, 50, 1.0),
        ]
        avg, avg_cny = calculate_cost_basis(trades)
        self.assertAlmostEqual(avg, 15.0)
        self.assertAlmostEqual(avg_cny, 13.0)

    def test_reentry_drops_cost_of_closed_position(self) -> None:
        trades = [
            trade(Side.BUY, 10, 100, 0.8),
            trade(Side.SELL, 20, 100, 0.9),
            trade(Side.BUY, 30, 100, 1.0),
        ]
        avg, avg_cny = calculate_cost_basis(trades)
        self.assertAlmostEqual(avg, 30.0)
        self.assertAlmostEqual(avg_cny, 30.0)


if __name__ == "__main__":
    unittest.main()
