import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import handlers
from quotes import Quote


class BuyHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_hong_kong_buy_uses_hkd_and_canonical_symbol(self) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=123, username="tester", full_name="Test User"),
            reply=AsyncMock(),
        )
        command = SimpleNamespace(args="01810 28.9 500")
        state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
        quote = Quote(
            symbol="01810",
            name="小米集团－Ｗ",
            price=28.9,
            open=31.04,
            prev_close=29.3,
            high=29.5,
            low=27.76,
            market="HK",
        )

        with (
            patch.object(handlers.quotes, "get_quote", AsyncMock(return_value=quote)),
            patch.object(handlers.quotes, "get_rate", AsyncMock(return_value=0.862692)),
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


if __name__ == "__main__":
    unittest.main()
