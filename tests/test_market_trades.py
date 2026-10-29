import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import handlers
import models
from quotes import Quote


class MarketTradeTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self, args):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=123, username="tester", full_name="Tester"),
            reply=AsyncMock(return_value=SimpleNamespace(message_id=42)),
        )
        state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
        return message, SimpleNamespace(args=args), state

    def quote(self, price=28.9):
        return Quote("01810", "小米", price, 29, 29, 30, 28, "HK")

    async def test_all_four_commands_use_quote_price_and_require_confirmation(self):
        cases = [
            (handlers.cmd_buyn, "01810.HK 0.25 5x", 25, "buy", False),
            (handlers.cmd_selln, "01810.HK 100 5x", 10000, "sell", False),
            (handlers.cmd_buyna, "01810.HK 500 5x 1s", 2000, "buy", True),
            (handlers.cmd_sellna, "01810.HK 500 01s 5x", 2000, "sell", True),
            (handlers.cmd_buyna, "01810.HK 500", 2005, "buy", True),
        ]
        for handler, args, qty, action, budget in cases:
            with self.subTest(command=handler.__name__, args=args):
                message, command, state = self.make_context(args)
                with (
                    patch.object(handlers.quotes, "get_quote", AsyncMock(return_value=self.quote())) as get_quote,
                    patch.object(handlers.quotes, "get_rate", AsyncMock(return_value=0.862692)),
                    patch.object(models, "get_position_entries", AsyncMock(return_value=[])),
                    patch.object(models, "insert_trade", AsyncMock()) as insert,
                ):
                    await handler(message, command, state)
                get_quote.assert_awaited_once_with("01810.HK")
                insert.assert_not_awaited()
                saved = next(
                    call.kwargs for call in state.update_data.await_args_list
                    if "price" in call.kwargs
                )
                self.assertEqual(saved["price"], 28.9)
                self.assertEqual(saved["market_price"], 28.9)
                self.assertEqual(saved["qty"], qty)
                self.assertEqual(saved["deviation_pct"], 0)
                self.assertEqual(saved["currency"], "HKD")
                self.assertEqual(saved["symbol"], "01810")
                state.set_state.assert_awaited_once_with(
                    handlers.TradeConfirm.buying if action == "buy" else handlers.TradeConfirm.selling
                )
                markup = message.reply.await_args.kwargs["reply_markup"]
                self.assertEqual(markup.inline_keyboard[0][0].callback_data, f"{action}_ok:123")
                self.assertEqual(markup.inline_keyboard[0][1].callback_data, "cancel:123")
                self.assertEqual("预算（人民币）" in message.reply.await_args.args[0], budget)
                self.assertEqual(state.update_data.await_args.kwargs["confirmation_message_id"], 42)

    async def test_bad_arguments_are_rejected_before_fetching_quote(self):
        for handler in (handlers.cmd_buyn, handlers.cmd_selln, handlers.cmd_buyna, handlers.cmd_sellna):
            for args in (None, " ", "AAPL", "AAPL -1", "AAPL NaN", "AAPL 100 5", "AAPL 100 2x 3x"):
                with self.subTest(command=handler.__name__, args=args):
                    message, command, state = self.make_context(args)
                    with patch.object(handlers.quotes, "get_quote", AsyncMock()) as get_quote:
                        await handler(message, command, state)
                    get_quote.assert_not_awaited()
                    state.set_state.assert_not_awaited()
                    self.assertIn(f"/{handler.__name__[4:]}", message.reply.await_args.args[0])

    async def test_missing_or_invalid_quote_does_not_create_confirmation(self):
        for handler in (handlers.cmd_buyn, handlers.cmd_selln, handlers.cmd_buyna, handlers.cmd_sellna):
            for quote in (None, self.quote(0), self.quote(-1), self.quote(float("nan"))):
                with self.subTest(command=handler.__name__, quote=quote):
                    message, command, state = self.make_context("01810 100")
                    with patch.object(handlers.quotes, "get_quote", AsyncMock(return_value=quote)):
                        await handler(message, command, state)
                    state.set_state.assert_not_awaited()
                    self.assertIn("未找到", message.reply.await_args.args[0])

    async def test_budget_uses_actual_price_to_reject_unaffordable_lot(self):
        for handler in (handlers.cmd_buyna, handlers.cmd_sellna):
            message, command, state = self.make_context("01810 100 100s")
            with (
                patch.object(handlers.quotes, "get_quote", AsyncMock(return_value=self.quote())),
                patch.object(handlers.quotes, "get_rate", AsyncMock(return_value=0.86)),
            ):
                await handler(message, command, state)
            state.set_state.assert_not_awaited()
            self.assertIn("预算", message.reply.await_args.args[0])
            self.assertIn("不足", message.reply.await_args.args[0])

    async def test_exchange_rate_failure_does_not_create_confirmation(self):
        message, command, state = self.make_context("01810 500")
        with (
            patch.object(handlers.quotes, "get_quote", AsyncMock(return_value=self.quote())),
            patch.object(handlers.quotes, "get_rate", AsyncMock(side_effect=handlers.quotes.RateUnavailableError)),
        ):
            await handlers.cmd_buyna(message, command, state)
        state.set_state.assert_not_awaited()
        self.assertIn("汇率", message.reply.await_args.args[0])
