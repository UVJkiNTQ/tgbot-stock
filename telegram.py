"""Lightweight async Telegram Bot API client and update dispatcher.

Replaces aiogram v3 + pydantic v2 + aiohttp with a small httpx-based
implementation covering only the Bot API surface used by this bot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import inspect
import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"
_FSM_TTL = 3600.0
_FSM_MAX_ENTRIES = 512


class TelegramAPIError(RuntimeError):
    """Raised when the Telegram Bot API returns an error or transport fails."""

    def __init__(self, message: str, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class ParseMode:
    HTML = "HTML"


@dataclass(frozen=True)
class BotCommand:
    command: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {"command": self.command, "description": self.description}


@dataclass(frozen=True)
class InlineKeyboardButton:
    text: str
    callback_data: str

    def to_dict(self) -> dict[str, str]:
        return {"text": self.text, "callback_data": self.callback_data}


@dataclass(frozen=True)
class InlineKeyboardMarkup:
    inline_keyboard: list[list[InlineKeyboardButton]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [button.to_dict() for button in row]
                for row in self.inline_keyboard
            ]
        }


@dataclass(frozen=True)
class BufferedInputFile:
    data: bytes
    filename: str


@dataclass(frozen=True)
class InputMediaPhoto:
    media: BufferedInputFile
    caption: str | None = None
    parse_mode: str | None = None


@dataclass(frozen=True)
class User:
    id: int
    first_name: str = ""
    last_name: str | None = None
    username: str | None = None

    @property
    def full_name(self) -> str:
        if self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.first_name

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> User:
        return cls(
            id=int(raw["id"]),
            first_name=str(raw.get("first_name") or ""),
            last_name=raw.get("last_name"),
            username=raw.get("username"),
        )


@dataclass(frozen=True)
class Chat:
    id: int
    type: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Chat:
        return cls(
            id=int(raw["id"]),
            type=str(raw.get("type") or ""),
        )


@dataclass(frozen=True)
class CommandObject:
    command: str
    args: str | None = None


class TelegramObject:
    """Base class for incoming Telegram update events."""


@dataclass
class Message(TelegramObject):
    message_id: int
    chat: Chat
    from_user: User | None = None
    text: str | None = None
    bot: Bot | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], bot: Bot | None = None) -> Message:
        from_raw = raw.get("from")
        return cls(
            message_id=int(raw["message_id"]),
            chat=Chat.from_dict(raw["chat"]),
            from_user=User.from_dict(from_raw) if isinstance(from_raw, dict) else None,
            text=raw.get("text"),
            bot=bot,
        )

    async def reply(
        self,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> Message:
        assert self.bot is not None
        return await self.bot.send_message(
            chat_id=self.chat.id,
            text=text,
            reply_to_message_id=self.message_id,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

    async def reply_photo(
        self,
        photo: BufferedInputFile,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> Message:
        assert self.bot is not None
        return await self.bot.send_photo(
            chat_id=self.chat.id,
            photo=photo,
            caption=caption,
            reply_to_message_id=self.message_id,
            parse_mode=parse_mode,
        )

    async def reply_media_group(
        self,
        media: list[InputMediaPhoto],
    ) -> list[Message]:
        assert self.bot is not None
        return await self.bot.send_media_group(
            chat_id=self.chat.id,
            media=media,
            reply_to_message_id=self.message_id,
        )

    async def edit_text(
        self,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> Message | bool:
        assert self.bot is not None
        return await self.bot.edit_message_text(
            chat_id=self.chat.id,
            message_id=self.message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )


@dataclass
class CallbackQuery(TelegramObject):
    id: str
    from_user: User
    data: str | None = None
    message: Message | None = None
    bot: Bot | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_dict(
        cls, raw: dict[str, Any], bot: Bot | None = None
    ) -> CallbackQuery:
        msg_raw = raw.get("message")
        return cls(
            id=str(raw["id"]),
            from_user=User.from_dict(raw["from"]),
            data=raw.get("data"),
            message=(
                Message.from_dict(msg_raw, bot=bot)
                if isinstance(msg_raw, dict)
                else None
            ),
            bot=bot,
        )

    async def answer(
        self,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> bool:
        assert self.bot is not None
        return await self.bot.answer_callback_query(
            callback_query_id=self.id,
            text=text,
            show_alert=show_alert,
        )


class State:
    """Minimal FSM state marker compatible with ``StatesGroup`` definitions."""

    def __init__(self, state: str | None = None) -> None:
        self._state = state
        self._group: str | None = None

    def __set_name__(self, owner: type, name: str) -> None:
        self._group = owner.__name__
        if self._state is None:
            self._state = f"{owner.__name__}:{name}"

    @property
    def state(self) -> str:
        return self._state or ""

    def __repr__(self) -> str:
        return f"State({self.state!r})"


class StatesGroup:
    """Base class for grouping ``State`` markers."""


class MemoryStorage:
    """Bounded in-memory FSM storage keyed by ``(chat_id, user_id)``."""

    def __init__(
        self,
        *,
        ttl: float = _FSM_TTL,
        max_entries: int = _FSM_MAX_ENTRIES,
    ) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._states: dict[tuple[int, int], tuple[str | None, float]] = {}
        self._data: dict[tuple[int, int], tuple[dict[str, Any], float]] = {}

    def _prune(self, now: float) -> None:
        for store in (self._states, self._data):
            if len(store) <= self._max_entries:
                continue
            expired = [k for k, (_v, ts) in store.items() if now - ts >= self._ttl]
            for k in expired:
                store.pop(k, None)
            while len(store) > self._max_entries:
                store.pop(next(iter(store)))

    def get_state(self, key: tuple[int, int]) -> str | None:
        entry = self._states.get(key)
        if entry is None:
            return None
        state, ts = entry
        if time.time() - ts >= self._ttl:
            self._states.pop(key, None)
            return None
        return state

    def set_state(self, key: tuple[int, int], state: str | None) -> None:
        now = time.time()
        self._states.pop(key, None)
        if state is not None:
            self._states[key] = (state, now)
            self._prune(now)

    def get_data(self, key: tuple[int, int]) -> dict[str, Any]:
        entry = self._data.get(key)
        if entry is None:
            return {}
        data, ts = entry
        if time.time() - ts >= self._ttl:
            self._data.pop(key, None)
            return {}
        return dict(data)

    def update_data(
        self, key: tuple[int, int], values: dict[str, Any]
    ) -> dict[str, Any]:
        now = time.time()
        current = self.get_data(key)
        current.update(values)
        self._data.pop(key, None)
        self._data[key] = (current, now)
        self._prune(now)
        return dict(current)

    def clear(self, key: tuple[int, int]) -> None:
        self._states.pop(key, None)
        self._data.pop(key, None)


class FSMContext:
    def __init__(self, storage: MemoryStorage, key: tuple[int, int]) -> None:
        self._storage = storage
        self._key = key

    async def get_state(self) -> str | None:
        return self._storage.get_state(self._key)

    async def set_state(self, state: State | str | None = None) -> None:
        value = state.state if isinstance(state, State) else state
        self._storage.set_state(self._key, value)

    async def get_data(self) -> dict[str, Any]:
        return self._storage.get_data(self._key)

    async def update_data(
        self, data: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        if data:
            merged.update(data)
        merged.update(kwargs)
        return self._storage.update_data(self._key, merged)

    async def clear(self) -> None:
        self._storage.clear(self._key)


class Command:
    """Filter matching one or more slash command names."""

    def __init__(self, *commands: str) -> None:
        self.commands = frozenset(cmd.lstrip("/").lower() for cmd in commands)


class CallbackPrefix:
    """Filter matching callback_query data by prefix."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix


class BaseMiddleware:
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        return await handler(event, data)


@dataclass
class _HandlerEntry:
    callback: Callable[..., Awaitable[Any]]
    filter_obj: Any
    accepts_command: bool
    accepts_state: bool


def _build_entry(
    callback: Callable[..., Awaitable[Any]], filter_obj: Any
) -> _HandlerEntry:
    params = inspect.signature(callback).parameters
    return _HandlerEntry(
        callback=callback,
        filter_obj=filter_obj,
        accepts_command="command" in params,
        accepts_state="state" in params,
    )


class Router:
    def __init__(self, name: str | None = None) -> None:
        self.name = name or ""
        self._message_handlers: list[_HandlerEntry] = []
        self._callback_handlers: list[_HandlerEntry] = []
        self._sub_routers: list[Router] = []

    def include_router(self, router: Router) -> Router:
        self._sub_routers.append(router)
        return router

    def message(
        self, filter_obj: Command | None = None
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        def decorator(
            fn: Callable[..., Awaitable[Any]]
        ) -> Callable[..., Awaitable[Any]]:
            self._message_handlers.append(_build_entry(fn, filter_obj))
            return fn

        return decorator

    def callback_query(
        self, filter_obj: CallbackPrefix | None = None
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        def decorator(
            fn: Callable[..., Awaitable[Any]]
        ) -> Callable[..., Awaitable[Any]]:
            self._callback_handlers.append(_build_entry(fn, filter_obj))
            return fn

        return decorator

    def iter_message_handlers(self) -> list[_HandlerEntry]:
        entries = list(self._message_handlers)
        for sub in self._sub_routers:
            entries.extend(sub.iter_message_handlers())
        return entries

    def iter_callback_handlers(self) -> list[_HandlerEntry]:
        entries = list(self._callback_handlers)
        for sub in self._sub_routers:
            entries.extend(sub.iter_callback_handlers())
        return entries


class _MiddlewareRegistry:
    def __init__(self) -> None:
        self.middlewares: list[BaseMiddleware] = []

    def outer_middleware(self, middleware: BaseMiddleware) -> BaseMiddleware:
        self.middlewares.append(middleware)
        return middleware


def parse_command(
    text: str | None, bot_username: str | None = None
) -> CommandObject | None:
    """Parse ``/cmd[@bot] [args]`` from a message text."""
    if not text or not text.startswith("/"):
        return None
    parts = text.split(maxsplit=1)
    head = parts[0][1:]
    if not head:
        return None
    cmd_name, sep, mention = head.partition("@")
    if not cmd_name:
        return None
    if sep and bot_username and mention.lower() != bot_username.lower():
        return None
    args = parts[1].strip() if len(parts) > 1 else None
    if args == "":
        args = None
    return CommandObject(command=cmd_name.lower(), args=args)


class Bot:
    def __init__(
        self,
        token: str,
        *,
        parse_mode: str | None = ParseMode.HTML,
        api_base: str = _API_BASE,
    ) -> None:
        self.token = token
        self.parse_mode = parse_mode
        self._base_url = f"{api_base.rstrip('/')}/bot{token}"
        self.username: str | None = None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def close(self) -> None:
        if not self._client.is_closed:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self._base_url}/{method}"
        try:
            resp = await self._client.post(
                url,
                json=json_body,
                data=data,
                files=files,
                timeout=timeout,
            )
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise TelegramAPIError(f"{method} HTTP error: {exc}") from exc
        except ValueError as exc:
            raise TelegramAPIError(f"{method} returned invalid JSON") from exc

        if not isinstance(payload, dict) or not payload.get("ok"):
            desc = (
                payload.get("description", "unknown Telegram API error")
                if isinstance(payload, dict)
                else "malformed Telegram API response"
            )
            code = payload.get("error_code") if isinstance(payload, dict) else None
            raise TelegramAPIError(f"{method} failed: {desc}", error_code=code)
        return payload.get("result")

    async def get_me(self) -> User:
        result = await self._request("getMe")
        user = User.from_dict(result)
        self.username = user.username
        return user

    async def set_my_commands(self, commands: list[BotCommand]) -> bool:
        result = await self._request(
            "setMyCommands",
            json_body={"commands": [cmd.to_dict() for cmd in commands]},
        )
        return bool(result)

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 30,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            body["offset"] = offset
        if allowed_updates is not None:
            body["allowed_updates"] = allowed_updates
        result = await self._request(
            "getUpdates",
            json_body=body,
            timeout=float(timeout) + 15.0,
        )
        return result if isinstance(result, list) else []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> Message:
        body: dict[str, Any] = {"chat_id": chat_id, "text": text}
        effective_mode = parse_mode if parse_mode is not None else self.parse_mode
        if effective_mode:
            body["parse_mode"] = effective_mode
        if reply_to_message_id is not None:
            body["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup.to_dict()
        result = await self._request("sendMessage", json_body=body)
        return Message.from_dict(result, bot=self)

    async def send_photo(
        self,
        chat_id: int,
        photo: BufferedInputFile,
        *,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> Message:
        form: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption is not None:
            form["caption"] = caption
        effective_mode = parse_mode if parse_mode is not None else self.parse_mode
        if effective_mode:
            form["parse_mode"] = effective_mode
        if reply_to_message_id is not None:
            form["reply_parameters"] = json.dumps(
                {
                    "message_id": reply_to_message_id,
                    "allow_sending_without_reply": True,
                }
            )
        result = await self._request(
            "sendPhoto",
            data=form,
            files={"photo": (photo.filename, photo.data, "image/png")},
            timeout=30.0,
        )
        return Message.from_dict(result, bot=self)

    async def send_media_group(
        self,
        chat_id: int,
        media: list[InputMediaPhoto],
        *,
        reply_to_message_id: int | None = None,
    ) -> list[Message]:
        media_spec: list[dict[str, Any]] = []
        files: dict[str, tuple[str, bytes, str]] = {}
        for index, item in enumerate(media):
            attach_key = f"file{index}"
            entry: dict[str, Any] = {
                "type": "photo",
                "media": f"attach://{attach_key}",
            }
            if item.caption is not None:
                entry["caption"] = item.caption
                effective_mode = (
                    item.parse_mode
                    if item.parse_mode is not None
                    else self.parse_mode
                )
                if effective_mode:
                    entry["parse_mode"] = effective_mode
            media_spec.append(entry)
            files[attach_key] = (
                item.media.filename,
                item.media.data,
                "image/png",
            )

        form: dict[str, Any] = {
            "chat_id": str(chat_id),
            "media": json.dumps(media_spec),
        }
        if reply_to_message_id is not None:
            form["reply_parameters"] = json.dumps(
                {
                    "message_id": reply_to_message_id,
                    "allow_sending_without_reply": True,
                }
            )
        result = await self._request(
            "sendMediaGroup",
            data=form,
            files=files,
            timeout=30.0,
        )
        return [
            Message.from_dict(raw, bot=self)
            for raw in (result if isinstance(result, list) else [])
        ]

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> Message | bool:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        effective_mode = parse_mode if parse_mode is not None else self.parse_mode
        if effective_mode:
            body["parse_mode"] = effective_mode
        if reply_markup is not None:
            body["reply_markup"] = reply_markup.to_dict()
        result = await self._request("editMessageText", json_body=body)
        if isinstance(result, dict):
            return Message.from_dict(result, bot=self)
        return bool(result)

    async def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> bool:
        body: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            body["text"] = text
        if show_alert:
            body["show_alert"] = True
        result = await self._request("answerCallbackQuery", json_body=body)
        return bool(result)


class Dispatcher:
    def __init__(self, storage: MemoryStorage | None = None) -> None:
        self._storage = storage or MemoryStorage()
        self._routers: list[Router] = []
        self.message = _MiddlewareRegistry()
        self.callback_query = _MiddlewareRegistry()

    def include_router(self, router: Router) -> Router:
        self._routers.append(router)
        return router

    def _all_message_handlers(self) -> list[_HandlerEntry]:
        entries: list[_HandlerEntry] = []
        for router in self._routers:
            entries.extend(router.iter_message_handlers())
        return entries

    def _all_callback_handlers(self) -> list[_HandlerEntry]:
        entries: list[_HandlerEntry] = []
        for router in self._routers:
            entries.extend(router.iter_callback_handlers())
        return entries

    async def _run_middlewares(
        self,
        middlewares: list[BaseMiddleware],
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        call_next = handler
        for mw in reversed(middlewares):
            prev = call_next

            async def wrapped(
                ev: TelegramObject,
                d: dict[str, Any],
                _mw: BaseMiddleware = mw,
                _prev: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]] = prev,
            ) -> Any:
                return await _mw(_prev, ev, d)

            call_next = wrapped
        return await call_next(event, data)

    async def process_update(self, bot: Bot, update: dict[str, Any]) -> None:
        if "message" in update and isinstance(update["message"], dict):
            msg = Message.from_dict(update["message"], bot=bot)
            await self._dispatch_message(bot, msg)
        elif "callback_query" in update and isinstance(
            update["callback_query"], dict
        ):
            cb = CallbackQuery.from_dict(update["callback_query"], bot=bot)
            await self._dispatch_callback(cb)

    async def _dispatch_message(self, bot: Bot, message: Message) -> None:
        cmd = parse_command(message.text, bot_username=bot.username)
        matched: _HandlerEntry | None = None
        for entry in self._all_message_handlers():
            if isinstance(entry.filter_obj, Command):
                if cmd is not None and cmd.command in entry.filter_obj.commands:
                    matched = entry
                    break
            elif entry.filter_obj is None:
                matched = entry
                break
        if matched is None:
            return

        user_id = message.from_user.id if message.from_user is not None else 0
        state = FSMContext(self._storage, (message.chat.id, user_id))

        async def invoke(ev: TelegramObject, _data: dict[str, Any]) -> Any:
            kwargs: dict[str, Any] = {}
            if matched.accepts_command:
                kwargs["command"] = cmd
            if matched.accepts_state:
                kwargs["state"] = state
            return await matched.callback(ev, **kwargs)

        await self._run_middlewares(
            self.message.middlewares, invoke, message, {"bot": bot, "state": state}
        )

    async def _dispatch_callback(self, callback: CallbackQuery) -> None:
        data_text = callback.data or ""
        matched: _HandlerEntry | None = None
        for entry in self._all_callback_handlers():
            if isinstance(entry.filter_obj, CallbackPrefix):
                if data_text.startswith(entry.filter_obj.prefix):
                    matched = entry
                    break
            elif entry.filter_obj is None:
                matched = entry
                break
        if matched is None:
            return

        chat_id = (
            callback.message.chat.id if callback.message is not None else 0
        )
        state = FSMContext(self._storage, (chat_id, callback.from_user.id))

        async def invoke(ev: TelegramObject, _data: dict[str, Any]) -> Any:
            kwargs: dict[str, Any] = {}
            if matched.accepts_state:
                kwargs["state"] = state
            return await matched.callback(ev, **kwargs)

        await self._run_middlewares(
            self.callback_query.middlewares,
            invoke,
            callback,
            {"bot": callback.bot, "state": state},
        )

    async def start_polling(self, bot: Bot, *, poll_timeout: int = 30) -> None:
        me = await bot.get_me()
        logger.info("已连接 Telegram Bot: @%s", me.username or me.id)

        offset: int | None = None
        backoff = 1.0
        tasks: set[asyncio.Task[None]] = set()

        async def run_update(update: dict[str, Any]) -> None:
            try:
                await self.process_update(bot, update)
            except Exception:
                logger.exception(
                    "处理 update %s 时发生未捕获异常",
                    update.get("update_id"),
                )

        try:
            while True:
                try:
                    updates = await bot.get_updates(
                        offset=offset,
                        timeout=poll_timeout,
                        allowed_updates=["message", "callback_query"],
                    )
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("getUpdates 失败，%.1f 秒后重试: %s", backoff, exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, 5.0)
                    continue

                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    task = asyncio.create_task(run_update(update))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
        finally:
            for task in list(tasks):
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
