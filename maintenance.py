import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


_state_lock = asyncio.Lock()
_active = False


def maintenance_active() -> bool:
    return _active


async def begin_maintenance() -> bool:
    """Enter maintenance mode once; return False if another update owns it."""
    global _active
    async with _state_lock:
        if _active:
            return False
        _active = True
        return True


async def end_maintenance() -> None:
    global _active
    async with _state_lock:
        _active = False


def _is_update_command(event: TelegramObject) -> bool:
    if not isinstance(event, Message) or not event.text:
        return False
    command = event.text.split(maxsplit=1)[0].split("@", maxsplit=1)[0]
    return command.lower() == "/update"


class MaintenanceMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not maintenance_active() or _is_update_command(event):
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer("数据库维护中，请等待完成通知", show_alert=True)
        elif isinstance(event, Message):
            await event.reply("数据库维护中，请暂时不要操作，完成后会通知")
        return None
