import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import handlers
from handlers import _parse_trade_args, _price_deviation
from quotes import Quote


class BuyHandlerTests(unittest.IsolatedAsyncioTestCase):
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
        ):
            await handlers.cmd_buy(message, command, state)

        saved = state.update_data.await_args.kwargs
        self.assertEqual(saved["symbol"], "01810")
        self.assertEqual(saved["currency"], "HKD")
        self.assertEqual(saved["price"], 28.9)
        self.assertEqual(saved["qty"], 500)
        self.assertEqual(saved["deviation_pct"], 0.0)

        reply = message.reply.await_args.args[0]
        self.assertIn("小米集团－Ｗ (01810)", reply)
        self.assertIn("500股 @ HKD 28.9000", reply)
        self.assertNotIn("委托价偏离", reply)


class TradeInputTests(unittest.TestCase):
    def test_hong_kong_symbol_is_canonicalized(self) -> None:
        self.assertEqual(_parse_trade_args("700 475.2 100"), ("00700", 475.2, 100))

    def test_non_finite_prices_are_rejected(self) -> None:
        self.assertIsNone(_parse_trade_args("00700 nan 100"))
        self.assertIsNone(_parse_trade_args("00700 inf 100"))

    def test_deviation_uses_prices_in_same_currency(self) -> None:
        self.assertAlmostEqual(_price_deviation(475.2, 475.2), 0.0)
        self.assertAlmostEqual(_price_deviation(500.0, 475.2), 5.2188552189)


if __name__ == "__main__":
    unittest.main()
