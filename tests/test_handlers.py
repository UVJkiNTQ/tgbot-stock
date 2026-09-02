import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import handlers
import models
from handlers import (
    _parse_amount_trade_args,
    _parse_close_args,
    _parse_min_unit,
    _parse_trade_args,
    _price_deviation,
)
from quotes import Quote


class BuyHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_buya_forwards_affordable_lot_to_buy_flow(self) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=123, username="tester", full_name="Test User"
            ),
            reply=AsyncMock(),
        )
        command = SimpleNamespace(args="600000 28.9 10000 5x 100s")
        state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
        quote = Quote(
            symbol="600000", name="测试股票", price=28.9, open=28.9,
            prev_close=28.9, high=28.9, low=28.9, market="A",
        )

        with (
            patch.object(handlers.quotes, "get_quote", AsyncMock(return_value=quote)),
            patch.object(handlers.quotes, "get_rate", AsyncMock(return_value=1.0)),
            patch.object(
                handlers.models, "get_position_entries", AsyncMock(return_value=[])
            ),
        ):
            await handlers.cmd_buya(message, command, state)

        saved = state.update_data.await_args.kwargs
        self.assertEqual(saved["qty"], 30000)
        self.assertEqual(saved["requested_leverage"], 5.0)
        self.assertIn("确认买入", message.reply.await_args.args[0])
        self.assertIn("300股", message.reply.await_args.args[0])

    async def test_sella_forwards_to_sell_flow(self) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=123, username="tester", full_name="Test User"
            ),
            reply=AsyncMock(),
        )
        command = SimpleNamespace(args="600000 3 1 01s")
        state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
        quote = Quote(
            symbol="600000", name="测试股票", price=3.0, open=3.0,
            prev_close=3.0, high=3.0, low=3.0, market="A",
        )

        with (
            patch.object(handlers.quotes, "get_quote", AsyncMock(return_value=quote)),
            patch.object(handlers.quotes, "get_rate", AsyncMock(return_value=1.0)),
            patch.object(
                handlers.models, "get_position_entries", AsyncMock(return_value=[])
            ),
        ):
            await handlers.cmd_sella(message, command, state)

        self.assertEqual(state.update_data.await_args.kwargs["qty"], 30)
        self.assertIn("确认卖出", message.reply.await_args.args[0])
        self.assertIn("0.3股", message.reply.await_args.args[0])

    async def test_buya_replies_before_quote_lookup_when_lot_is_unaffordable(
        self,
    ) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=123, username="tester", full_name="Test User"
            ),
            reply=AsyncMock(),
        )
        command = SimpleNamespace(args="600000 10.01 1000 100s")
        state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())

        with patch.object(
            handlers.quotes, "get_quote", AsyncMock()
        ) as get_quote:
            await handlers.cmd_buya(message, command, state)

        get_quote.assert_not_awaited()
        state.set_state.assert_not_awaited()
        state.update_data.assert_not_awaited()
        self.assertIn("无法买入", message.reply.await_args.args[0])

    async def test_hong_kong_buy_uses_hkd_and_canonical_symbol(self) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=123, username="tester", full_name="Test User"
            ),
            reply=AsyncMock(),
        )
        command = SimpleNamespace(args="01810 28.9 500")
        state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
        quote = Quote(
            symbol="01810",
            name="小米集团－Ｗ",
            price=28.9,
            open=29.3,
            prev_close=31.04,
            high=29.5,
            low=27.76,
            market="HK",
        )

        with (
            patch.object(
                handlers.quotes, "get_quote", AsyncMock(return_value=quote)
            ),
            patch.object(
                handlers.quotes, "get_rate", AsyncMock(return_value=0.862692)
            ),
            patch.object(
                handlers.models, "get_position_entries", AsyncMock(return_value=[])
            ),
        ):
            await handlers.cmd_buy(message, command, state)

        saved = state.update_data.await_args.kwargs
        self.assertEqual(saved["symbol"], "01810")
        self.assertEqual(saved["market"], "HK")
        self.assertEqual(saved["currency"], "HKD")
        self.assertEqual(saved["price"], 28.9)
        self.assertEqual(saved["qty"], 50000)
        self.assertEqual(saved["deviation_pct"], 0.0)

        reply = message.reply.await_args.args[0]
        self.assertIn("小米集团－Ｗ (01810)", reply)
        self.assertIn("500股 @ HKD 28.9000", reply)
        self.assertNotIn("委托价偏离", reply)

    async def test_hong_kong_amount_uses_cny_budget_and_hkd_rate(self) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=123, username="tester", full_name="Test User"
            ),
            reply=AsyncMock(),
        )
        command = SimpleNamespace(args="01810 28.9 500")
        state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
        quote = Quote(
            symbol="01810",
            name="小米集团－Ｗ",
            price=28.9,
            open=29.3,
            prev_close=31.04,
            high=29.5,
            low=27.76,
            market="HK",
        )

        with (
            patch.object(
                handlers.quotes, "get_quote", AsyncMock(return_value=quote)
            ),
            patch.object(
                handlers.quotes, "get_rate", AsyncMock(return_value=0.862692)
            ),
            patch.object(
                handlers.models, "get_position_entries", AsyncMock(return_value=[])
            ),
        ):
            await handlers.cmd_buya(message, command, state)

        saved = state.update_data.await_args.kwargs
        self.assertEqual(saved["qty"], 2005)  # 20.05 HKD shares within ¥500
        reply = message.reply.await_args.args[0]
        self.assertIn("20.05股", reply)
        self.assertIn("预算（人民币）：¥500.00", reply)

    async def test_different_leverage_warns_about_new_position_entry(self) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=123, username="tester", full_name="Test User"
            ),
            reply=AsyncMock(),
        )
        command = SimpleNamespace(args="600000 10 100 2x")
        state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
        quote = Quote(
            symbol="600000", name="浦发银行", price=10.0, open=10.0,
            prev_close=10.0, high=10.0, low=10.0, market="A",
        )

        with (
            patch.object(handlers.quotes, "get_quote", AsyncMock(return_value=quote)),
            patch.object(handlers.quotes, "get_rate", AsyncMock(return_value=1.0)),
            patch.object(
                handlers.models, "get_position_entries",
                AsyncMock(
                    return_value=[models.PositionEntry("600000", 5.0, 10000)]
                ),
            ),
        ):
            await handlers.cmd_buy(message, command, state)

        reply = message.reply.await_args.args[0]
        self.assertIn("新的 2x 独立持仓条目", reply)
        self.assertEqual(state.update_data.await_args.kwargs["requested_leverage"], 2.0)

    async def test_wrong_leverage_cannot_reduce_an_existing_position(self) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=123, username="tester", full_name="Test User"
            ),
            reply=AsyncMock(),
        )
        command = SimpleNamespace(args="600000 10 10 2x")
        state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
        quote = Quote(
            symbol="600000", name="浦发银行", price=10.0, open=10.0,
            prev_close=10.0, high=10.0, low=10.0, market="A",
        )

        with (
            patch.object(handlers.quotes, "get_quote", AsyncMock(return_value=quote)),
            patch.object(
                handlers.models, "get_position_entries",
                AsyncMock(
                    return_value=[models.PositionEntry("600000", 5.0, 10000)]
                ),
            ),
        ):
            await handlers.cmd_sell(message, command, state)

        self.assertIn("操作无效", message.reply.await_args.args[0])
        self.assertIn("可平仓杠杆：5x", message.reply.await_args.args[0])
        state.update_data.assert_not_awaited()

    async def test_all_with_leverage_targets_only_that_entry(self) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=123, username="tester", full_name="Test User"
            ),
            reply=AsyncMock(),
        )
        command = SimpleNamespace(args="600000 10 ALL 5x")
        state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
        quote = Quote(
            symbol="600000", name="浦发银行", price=10.0, open=10.0,
            prev_close=10.0, high=10.0, low=10.0, market="A",
        )

        with (
            patch.object(handlers.quotes, "get_quote", AsyncMock(return_value=quote)),
            patch.object(handlers.quotes, "get_rate", AsyncMock(return_value=1.0)),
            patch.object(
                handlers.models, "get_position_entries",
                AsyncMock(
                    return_value=[
                        models.PositionEntry("600000", 2.0, 10000),
                        models.PositionEntry("600000", 5.0, 5000),
                    ]
                ),
            ),
        ):
            await handlers.cmd_sell(message, command, state)

        saved = state.update_data.await_args.kwargs
        self.assertEqual(saved["qty"], 5000)
        self.assertEqual(saved["requested_leverage"], 5.0)
        self.assertTrue(saved["close_all"])
        reply = message.reply.await_args.args[0]
        self.assertIn("5x 卖出50股", reply)
        self.assertNotIn("2x", reply)


class CloseHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_prepares_a_buy_for_a_short_position(self) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=123, username="tester", full_name="Test User"
            ),
            reply=AsyncMock(),
        )
        command = SimpleNamespace(args="600000 8.00")
        state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
        quote = Quote(
            symbol="600000",
            name="浦发银行",
            price=8.0,
            open=8.0,
            prev_close=8.0,
            high=8.0,
            low=8.0,
            market="A",
        )

        with (
            patch.object(handlers.quotes, "get_quote", AsyncMock(return_value=quote)),
            patch.object(handlers.quotes, "get_rate", AsyncMock(return_value=1.0)),
            patch.object(
                handlers.models, "get_position_entries",
                AsyncMock(
                    return_value=[
                        models.PositionEntry("600000", 5.0, -30000)
                    ]
                ),
            ),
        ):
            await handlers.cmd_close(message, command, state)

        saved = state.update_data.await_args.kwargs
        self.assertEqual(saved["qty"], 30000)
        self.assertTrue(saved["close_all"])
        reply = message.reply.await_args.args[0]
        self.assertIn("将关闭 1 个条目", reply)
        self.assertIn("5x 买入300股", reply)


class TradeInputTests(unittest.TestCase):
    def test_trade_lines_converts_utc_to_beijing_time(self) -> None:
        trade = models.Trade(
            id=1,
            user_id=123,
            username="tester",
            symbol="600000",
            side=models.Side.BUY,
            price=10.0,
            qty=100,
            currency="CNY",
            rate=1.0,
            trade_ts="2026-09-02T20:00:00+00:00",
            leverage=1.0,
            market="A",
        )

        rendered = handlers.trade_lines([trade])

        self.assertIn("2026-09-03 04:00:00", rendered)
        self.assertNotIn("2026-09-02", rendered)

    def test_compact_minimum_units_map_to_share_precision(self) -> None:
        self.assertEqual(_parse_min_unit("100s"), 10000)
        self.assertEqual(_parse_min_unit("1s"), 100)
        self.assertEqual(_parse_min_unit("01s"), 10)
        self.assertEqual(_parse_min_unit("001s"), 1)

    def test_beijing_exchange_qualifier_is_preserved(self) -> None:
        parsed = _parse_trade_args("920118.BSE 10 1")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed[0], "920118.BSE")

    def test_amount_trade_floors_to_lot_without_exceeding_amount(self) -> None:
        parsed = _parse_amount_trade_args("600000 28.9 10000 100s 5x")
        self.assertIsNotNone(parsed)
        symbol, price, amount, qty, min_unit, leverage = parsed
        self.assertEqual(symbol, "600000")
        self.assertEqual(price, 28.9)
        self.assertEqual(str(amount), "10000")
        self.assertEqual(qty, 30000)
        self.assertEqual(min_unit, 10000)
        self.assertEqual(leverage, 5.0)
        self.assertLessEqual(price * (qty / models.QTY_SCALE), float(amount))

    def test_amount_trade_converts_cny_budget_to_quote_currency(self) -> None:
        parsed = _parse_amount_trade_args("01810 28.9 500", rate=0.862692)
        self.assertIsNotNone(parsed)
        _symbol, _price, _amount, qty, _min_unit, _leverage = parsed
        self.assertEqual(qty, 2005)

    def test_amount_trade_accepts_options_in_either_order(self) -> None:
        first = _parse_amount_trade_args("600000 3 1 2x 01s")
        second = _parse_amount_trade_args("600000 3 1 01s 2x")
        self.assertEqual(first, second)
        self.assertEqual(first[3], 30)

    def test_amount_trade_defaults_to_one_hundredth_share(self) -> None:
        parsed = _parse_amount_trade_args("600000 3 1")
        self.assertEqual(parsed[3], 33)
        self.assertEqual(parsed[4], 1)

    def test_amount_trade_rejects_duplicate_or_too_small_units(self) -> None:
        self.assertIsNone(_parse_amount_trade_args("600000 3 1 1s 01s"))
        self.assertIsNone(_parse_amount_trade_args("600000 3 1 0001s"))

    def test_hong_kong_symbol_is_canonicalized(self) -> None:
        self.assertEqual(
            _parse_trade_args("700 475.2 100"), ("00700", 475.2, 10000, None)
        )

    def test_non_finite_prices_are_rejected(self) -> None:
        self.assertIsNone(_parse_trade_args("00700 nan 100"))
        self.assertIsNone(_parse_trade_args("00700 inf 100"))

    def test_all_quantity_is_case_insensitive(self) -> None:
        self.assertEqual(
            _parse_trade_args("00700 475.2 all"), ("00700", 475.2, "ALL", None)
        )
        self.assertEqual(
            _parse_trade_args("00700 475.2 ALL 5x"),
            ("00700", 475.2, "ALL", 5.0),
        )

    def test_fractional_quantity_uses_shares_as_input(self) -> None:
        self.assertEqual(
            _parse_trade_args("00700 475.2 1"), ("00700", 475.2, 100, None)
        )
        self.assertEqual(
            _parse_trade_args("00700 475.2 0.01"),
            ("00700", 475.2, 1, None),
        )

    def test_optional_leverage_accepts_x_suffix(self) -> None:
        self.assertEqual(
            _parse_trade_args("00700 475.2 1.25 5x"),
            ("00700", 475.2, 125, 5.0),
        )
        self.assertEqual(
            _parse_trade_args("00700 475.2 1.25 2.5x"),
            ("00700", 475.2, 125, 2.5),
        )

    def test_leverage_requires_x_suffix(self) -> None:
        self.assertIsNone(_parse_trade_args("00700 475.2 1.25 5"))
        self.assertIsNone(_parse_trade_args("00700 475.2 1.25 X5"))

    def test_leverage_below_one_is_rejected(self) -> None:
        self.assertIsNone(_parse_trade_args("00700 475.2 1 0.5x"))

    def test_quantity_smaller_than_one_hundredth_is_rejected(self) -> None:
        self.assertIsNone(_parse_trade_args("00700 475.2 0.001"))

    def test_close_arguments_use_symbol_and_price_only(self) -> None:
        self.assertEqual(_parse_close_args("700 475.2"), ("00700", 475.2))
        self.assertIsNone(_parse_close_args("700 475.2 100"))

    def test_deviation_uses_prices_in_same_currency(self) -> None:
        self.assertAlmostEqual(_price_deviation(475.2, 475.2), 0.0)
        self.assertAlmostEqual(_price_deviation(500.0, 475.2), 5.2188552189)


class ReportCommandTests(unittest.IsolatedAsyncioTestCase):
    def message(self) -> SimpleNamespace:
        return SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            reply=AsyncMock(),
        )

    async def test_delete_accepts_multiple_hash_prefixed_ids(self) -> None:
        message = self.message()
        command = SimpleNamespace(args="#3 #5 #8")
        with patch.object(
            handlers.models,
            "delete_trades",
            AsyncMock(return_value=[3, 8]),
        ) as delete_trades:
            await handlers.cmd_delete(message, command)

        delete_trades.assert_awaited_once_with([3, 5, 8], 123)
        reply = message.reply.await_args.args[0]
        self.assertIn("#3 #8", reply)
        self.assertIn("#5", reply)

    async def test_delete_accepts_symbol_and_leverage(self) -> None:
        message = self.message()
        command = SimpleNamespace(args="600000 5x")
        with patch.object(
            handlers.models,
            "delete_trades_by_symbol_leverage",
            AsyncMock(return_value=4),
        ) as delete_bucket:
            await handlers.cmd_delete(message, command)

        delete_bucket.assert_awaited_once_with(123, "600000", 5.0)
        self.assertIn("全部 4 条", message.reply.await_args.args[0])

    async def test_lb_without_mode_defaults_to_unrealized_included(self) -> None:
        message = self.message()
        command = SimpleNamespace(args=None)
        result = handlers.pnl.UserPnl(
            user_id=123,
            username="tester",
            positions=[],
            total_unrealized_pnl_cny=50.0,
            total_market_value_cny=1050.0,
            total_cost_cny=500.0,
            total_realized_pnl_cny=100.0,
            total_closed_cost_cny=500.0,
        )
        with patch.object(
            handlers.pnl,
            "compute_leaderboard",
            AsyncMock(return_value=[result]),
        ) as compute_leaderboard:
            await handlers.cmd_leaderboard(message, command)

        compute_leaderboard.assert_awaited_once_with(True)
        reply = message.reply.await_args.args[0]
        self.assertIn("含浮盈", reply)
        self.assertIn("+15.00%", reply)
        self.assertIn("+¥150.00", reply)

    async def test_lb_r_uses_realized_only(self) -> None:
        message = self.message()
        command = SimpleNamespace(args="r")
        result = handlers.pnl.UserPnl(
            user_id=123,
            username="tester",
            positions=[],
            total_unrealized_pnl_cny=-999.0,
            total_market_value_cny=0.0,
            total_cost_cny=500.0,
            total_realized_pnl_cny=100.0,
            total_closed_cost_cny=500.0,
        )
        with patch.object(
            handlers.pnl,
            "compute_leaderboard",
            AsyncMock(return_value=[result]),
        ) as compute_leaderboard:
            await handlers.cmd_leaderboard(message, command)

        compute_leaderboard.assert_awaited_once_with(False)
        reply = message.reply.await_args.args[0]
        self.assertIn("不含浮盈", reply)
        self.assertIn("+20.00%", reply)
        self.assertIn("+¥100.00", reply)


if __name__ == "__main__":
    unittest.main()
