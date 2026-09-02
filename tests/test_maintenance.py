import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import CallbackQuery, Message

import maintenance
from handlers.system import cmd_update
from models import DatabaseUpdateResult


class MaintenanceMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await maintenance.end_maintenance()

    async def test_blocks_commands_and_callbacks_while_update_runs(self) -> None:
        self.assertTrue(await maintenance.begin_maintenance())
        middleware = maintenance.MaintenanceMiddleware()
        handler = AsyncMock()

        message = MagicMock(spec=Message)
        message.text = "/position"
        message.reply = AsyncMock()
        await middleware(handler, message, {})

        callback = MagicMock(spec=CallbackQuery)
        callback.answer = AsyncMock()
        await middleware(handler, callback, {})

        handler.assert_not_awaited()
        message.reply.assert_awaited_once()
        callback.answer.assert_awaited_once_with(
            "数据库维护中，请等待完成通知", show_alert=True
        )

    async def test_allows_update_command_through_maintenance_gate(self) -> None:
        self.assertTrue(await maintenance.begin_maintenance())
        middleware = maintenance.MaintenanceMiddleware()
        handler = AsyncMock(return_value="handled")
        message = MagicMock(spec=Message)
        message.text = "/update@stock_bot"

        result = await middleware(handler, message, {"key": "value"})

        self.assertEqual(result, "handled")
        handler.assert_awaited_once_with(message, {"key": "value"})


class UpdateNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await maintenance.end_maintenance()

    async def test_update_announces_maintenance_then_completion(self) -> None:
        message = SimpleNamespace(reply=AsyncMock())
        result = DatabaseUpdateResult(True, 3, 0, 1)

        with patch(
            "handlers.system.models.update_database",
            AsyncMock(return_value=result),
        ):
            await cmd_update(message)

        replies = [call.args[0] for call in message.reply.await_args_list]
        self.assertIn("请暂时不要使用", replies[0])
        self.assertIn("维护结束，现在可以继续使用", replies[1])
        self.assertFalse(maintenance.maintenance_active())

    async def test_update_failure_unlocks_and_notifies_user(self) -> None:
        message = SimpleNamespace(reply=AsyncMock())

        with patch(
            "handlers.system.models.update_database",
            AsyncMock(side_effect=RuntimeError("boom")),
        ), patch("handlers.system.logger.exception"):
            await cmd_update(message)

        replies = [call.args[0] for call in message.reply.await_args_list]
        self.assertIn("事务已回滚", replies[1])
        self.assertIn("可以继续使用", replies[1])
        self.assertFalse(maintenance.maintenance_active())
