import unittest
from unittest.mock import AsyncMock, patch

import models
import pnl
from models import Side, Trade
from pnl import calculate_cost_basis, calculate_position_state
from quotes import Quote


def trade(
    side: Side, price: float, qty: int | str,
    rate: float = 1.0, leverage: float = 1.0,
) -> Trade:
    return Trade(
        id=None,
        user_id=1,
        username="tester",
        symbol="00700",
        side=side,
        price=price,
        qty=models.quantity_to_units(qty),
        currency="HKD",
        rate=rate,
        trade_ts="2026-01-01T00:00:00+00:00",
        leverage=leverage,
        market="HK",
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

    def test_partial_short_cover_preserves_short_entry_price(self) -> None:
        trades = [
            trade(Side.SELL, 10, 100, 0.8),
            trade(Side.SELL, 20, 100, 0.9),
            trade(Side.BUY, 8, 50, 1.0),
        ]
        avg, avg_cny = calculate_cost_basis(trades)
        self.assertAlmostEqual(avg, 15.0)
        self.assertAlmostEqual(avg_cny, 13.0)

    def test_short_to_long_reversal_uses_reversal_trade_price(self) -> None:
        trades = [
            trade(Side.SELL, 10, 100),
            trade(Side.BUY, 8, 150),
        ]
        avg, avg_cny = calculate_cost_basis(trades)
        self.assertAlmostEqual(avg, 8.0)
        self.assertAlmostEqual(avg_cny, 8.0)

    def test_long_to_short_reversal_uses_reversal_trade_price(self) -> None:
        trades = [
            trade(Side.BUY, 10, 100),
            trade(Side.SELL, 12, 150),
        ]
        avg, avg_cny = calculate_cost_basis(trades)
        self.assertAlmostEqual(avg, 12.0)
        self.assertAlmostEqual(avg_cny, 12.0)

    def test_direct_reversal_keeps_bucket_leverage(self) -> None:
        trades = [
            trade(Side.BUY, 10, 100, leverage=5),
            trade(Side.SELL, 12, 150, leverage=5),
        ]
        avg, avg_cny, leverage = calculate_position_state(trades)
        self.assertAlmostEqual(avg, 12.0)
        self.assertAlmostEqual(avg_cny, 12.0)
        self.assertEqual(leverage, 5.0)


class ShortPositionPnlTests(unittest.IsolatedAsyncioTestCase):
    async def _compute_at(
        self, current_price: float, leverage: float = 1.0
    ) -> pnl.UserPnl:
        trades = [trade(Side.SELL, 10, 100, leverage=leverage)]
        quote = Quote(
            symbol="00700",
            name="腾讯控股",
            price=current_price,
            open=current_price,
            prev_close=current_price,
            high=current_price,
            low=current_price,
            market="HK",
        )
        with (
            patch.object(
                pnl.models,
                "get_position_entries",
                AsyncMock(
                    return_value=[
                        models.PositionEntry("00700", leverage, -10000, "HK")
                    ]
                ),
            ),
            patch.object(pnl.models, "get_trades", AsyncMock(return_value=trades)),
            patch.object(
                pnl.quotes, "get_quotes",
                AsyncMock(return_value={("00700", "HK"): quote}),
            ),
            patch.object(pnl.quotes, "get_rate", AsyncMock(return_value=1.0)),
        ):
            return await pnl.compute_user_pnl(1)

    async def test_short_profits_when_price_falls(self) -> None:
        result = await self._compute_at(8.0)
        position = result.positions[0]
        self.assertEqual(position.qty, -10000)
        self.assertIs(type(position.qty), int)
        self.assertAlmostEqual(position.avg_cost, 10.0)
        self.assertAlmostEqual(position.unrealized_pnl, 200.0)
        self.assertAlmostEqual(position.unrealized_pnl_pct, 20.0)
        self.assertAlmostEqual(result.total_cost_cny, 1000.0)
        self.assertAlmostEqual(result.total_unrealized_pnl_cny, 200.0)

    async def test_short_loses_when_price_rises(self) -> None:
        result = await self._compute_at(12.0)
        position = result.positions[0]
        self.assertAlmostEqual(position.unrealized_pnl, -200.0)
        self.assertAlmostEqual(position.unrealized_pnl_pct, -20.0)
        self.assertAlmostEqual(result.total_unrealized_pnl_cny, -200.0)

    async def test_leverage_multiplies_short_profit_and_return(self) -> None:
        result = await self._compute_at(8.0, leverage=5.0)
        position = result.positions[0]
        self.assertEqual(position.leverage, 5.0)
        self.assertAlmostEqual(position.unrealized_pnl, 1000.0)
        self.assertAlmostEqual(position.unrealized_pnl_pct, 100.0)
        self.assertAlmostEqual(result.total_unrealized_pnl_cny, 1000.0)

    async def test_different_leverages_are_reported_as_separate_positions(self) -> None:
        trades = [
            trade(Side.BUY, 10, 100, leverage=2),
            trade(Side.BUY, 20, 50, leverage=5),
        ]
        quote = Quote(
            symbol="00700", name="腾讯控股", price=15.0, open=15.0,
            prev_close=15.0, high=15.0, low=15.0, market="HK",
        )
        with (
            patch.object(
                pnl.models,
                "get_position_entries",
                AsyncMock(
                    return_value=[
                        models.PositionEntry("00700", 2.0, 10000, "HK"),
                        models.PositionEntry("00700", 5.0, 5000, "HK"),
                    ]
                ),
            ),
            patch.object(pnl.models, "get_trades", AsyncMock(return_value=trades)),
            patch.object(
                pnl.quotes, "get_quotes",
                AsyncMock(return_value={("00700", "HK"): quote}),
            ),
            patch.object(pnl.quotes, "get_rate", AsyncMock(return_value=1.0)),
        ):
            result = await pnl.compute_user_pnl(1)

        by_leverage = {position.leverage: position for position in result.positions}
        self.assertEqual(set(by_leverage), {2.0, 5.0})
        self.assertAlmostEqual(by_leverage[2.0].unrealized_pnl, 1000.0)
        self.assertAlmostEqual(by_leverage[5.0].unrealized_pnl, -1250.0)
        self.assertAlmostEqual(result.total_unrealized_pnl_cny, -250.0)


class MarketIdentityPnlTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_code_stock_and_fund_keep_their_own_quotes(self) -> None:
        stock_trade = Trade(
            id=1, user_id=1, username="tester", symbol="002714",
            side=Side.BUY, price=40.0, qty=models.quantity_to_units(100),
            currency="CNY", rate=1.0, trade_ts="2026-01-01", market="A",
        )
        fund_trade = Trade(
            id=2, user_id=1, username="tester", symbol="002714",
            side=Side.BUY, price=1.2, qty=models.quantity_to_units(20),
            currency="CNY", rate=1.0, trade_ts="2026-01-02", market="FUND",
        )
        stock_quote = Quote(
            symbol="002714", name="牧原股份", price=42.0, open=42.0,
            prev_close=42.0, high=42.0, low=42.0, market="A",
        )
        fund_quote = Quote(
            symbol="002714", name="鹏华金城混合D", price=1.37, open=1.37,
            prev_close=1.37, high=1.37, low=1.37, market="FUND",
        )

        async def get_trades(
            _user_id: int, symbol: str | None = None, market: str | None = None
        ) -> list[Trade]:
            if symbol is None:
                return [stock_trade, fund_trade]
            return [stock_trade] if market == "A" else [fund_trade]

        with (
            patch.object(
                pnl.models,
                "get_position_entries",
                AsyncMock(
                    return_value=[
                        models.PositionEntry("002714", 1.0, 10000, "A"),
                        models.PositionEntry("002714", 1.0, 2000, "FUND"),
                    ]
                ),
            ),
            patch.object(pnl.models, "get_trades", AsyncMock(side_effect=get_trades)),
            patch.object(
                pnl.quotes,
                "get_quotes",
                AsyncMock(
                    return_value={
                        ("002714", "A"): stock_quote,
                        ("002714", "FUND"): fund_quote,
                    }
                ),
            ),
            patch.object(pnl.quotes, "get_rate", AsyncMock(return_value=1.0)),
        ):
            result = await pnl.compute_user_pnl(1)

        by_market = {position.market: position for position in result.positions}
        self.assertEqual(by_market["A"].name, "牧原股份")
        self.assertEqual(by_market["A"].current_price, 42.0)
        self.assertEqual(by_market["FUND"].name, "鹏华金城混合D")
        self.assertEqual(by_market["FUND"].current_price, 1.37)


if __name__ == "__main__":
    unittest.main()
