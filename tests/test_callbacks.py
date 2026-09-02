import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import handlers.callbacks as callbacks
import models
from models import Side


class _State:
    def __init__(self, data: dict) -> None:
        self.data = dict(data)
        self.clear_count = 0

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **values) -> None:
        self.data.update(values)

    async def clear(self) -> None:
        self.clear_count += 1
        self.data.clear()


class ConfirmationRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_confirmation_message_cannot_use_new_state(self) -> None:
        user = SimpleNamespace(id=899999)
        callback = SimpleNamespace(
            from_user=user,
            data="buy_ok:899999",
            message=SimpleNamespace(message_id=10, edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        state = _State(
            {
                "user_id": user.id,
                "symbol": "600000",
                "confirmation_message_id": 11,
            }
        )

        with patch.object(
            callbacks, "insert_confirmed_trades", AsyncMock()
        ) as insert_confirmed:
            await callbacks.on_buy_confirm(callback, state)

        insert_confirmed.assert_not_awaited()
        callback.answer.assert_awaited_once_with(
            "该确认已失效，请使用最新的确认按钮", show_alert=True
        )

    async def test_double_click_confirmation_inserts_once(self) -> None:
        user = SimpleNamespace(id=900000)
        callback = SimpleNamespace(
            from_user=user,
            data="buy_ok:900000",
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        state = _State(
            {
                "user_id": user.id,
                "username": "tester",
                "symbol": "600000",
                "market": "A",
                "price": 10.0,
                "qty": 100,
                "currency": "CNY",
                "rate": 1.0,
                "market_price": 10.0,
                "deviation_pct": 0.0,
                "deviation_ack": False,
                "close_all": False,
            }
        )
        inserted = models.Trade(
            id=1,
            user_id=user.id,
            username="tester",
            symbol="600000",
            side=Side.BUY,
            price=10.0,
            qty=100,
            currency="CNY",
            rate=1.0,
            trade_ts="2026-01-01",
            leverage=1.0,
            market="A",
        )

        async def insert_once(*_args, **_kwargs):
            await asyncio.sleep(0)
            return [inserted]

        with (
            patch.object(
                callbacks, "insert_confirmed_trades", AsyncMock(side_effect=insert_once)
            ) as insert_confirmed,
            patch.object(callbacks.quotes, "get_quote", AsyncMock(return_value=None)),
        ):
            await asyncio.gather(
                callbacks.on_buy_confirm(callback, state),
                callbacks.on_buy_confirm(callback, state),
            )

        insert_confirmed.assert_awaited_once()
        self.assertEqual(state.clear_count, 1)

    async def test_double_click_after_deviation_warning_inserts_once(self) -> None:
        user = SimpleNamespace(id=900001)
        message = SimpleNamespace(edit_text=AsyncMock())
        callback = SimpleNamespace(
            from_user=user,
            data="buy_ok:900001",
            message=message,
            answer=AsyncMock(),
        )
        state = _State(
            {
                "user_id": user.id,
                "username": "tester",
                "symbol": "600000",
                "market": "A",
                "price": 12.0,
                "qty": 100,
                "currency": "CNY",
                "rate": 1.0,
                "market_price": 10.0,
                "deviation_pct": 20.0,
                "deviation_ack": False,
                "close_all": False,
            }
        )
        inserted = models.Trade(
            id=1,
            user_id=user.id,
            username="tester",
            symbol="600000",
            side=Side.BUY,
            price=12.0,
            qty=100,
            currency="CNY",
            rate=1.0,
            trade_ts="2026-01-01",
            leverage=1.0,
            market="A",
        )

        async def insert_once(*_args, **_kwargs):
            await asyncio.sleep(0)
            return [inserted]

        with (
            patch.object(
                callbacks, "insert_confirmed_trades", AsyncMock(side_effect=insert_once)
            ) as insert_confirmed,
            patch.object(callbacks.quotes, "get_quote", AsyncMock(return_value=None)),
        ):
            await asyncio.gather(
                callbacks.on_buy_confirm(callback, state),
                callbacks.on_buy_confirm(callback, state),
            )

        insert_confirmed.assert_awaited_once()
        self.assertEqual(state.clear_count, 1)


if __name__ == "__main__":
    unittest.main()
