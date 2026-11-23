import json
import unittest
from unittest.mock import AsyncMock

import httpx

import http_client
from telegram import (
    BaseMiddleware,
    Bot,
    BotCommand,
    BufferedInputFile,
    CallbackPrefix,
    CallbackQuery,
    Command,
    CommandObject,
    Dispatcher,
    FSMContext,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    MemoryStorage,
    Message,
    Router,
    State,
    StatesGroup,
    TelegramAPIError,
    parse_command,
)


class _SampleStates(StatesGroup):
    step_one = State()


class ParseCommandTests(unittest.TestCase):
    def test_parses_command_and_args(self) -> None:
        cmd = parse_command("/buy 600000 10.5 100")
        self.assertEqual(cmd, CommandObject(command="buy", args="600000 10.5 100"))

    def test_handles_bot_mention_and_empty_args(self) -> None:
        self.assertEqual(
            parse_command("/position@StockBot", bot_username="stockbot"),
            CommandObject(command="position", args=None),
        )
        self.assertIsNone(
            parse_command("/position@OtherBot", bot_username="stockbot")
        )
        self.assertIsNone(parse_command("plain text"))


class MemoryStorageTests(unittest.TestCase):
    def test_evicts_oldest_entries_when_over_capacity(self) -> None:
        storage = MemoryStorage(ttl=60.0, max_entries=2)
        storage.update_data((1, 1), {"v": 1})
        storage.update_data((1, 2), {"v": 2})
        storage.update_data((1, 3), {"v": 3})

        self.assertEqual(storage.get_data((1, 1)), {})
        self.assertEqual(storage.get_data((1, 2)), {"v": 2})
        self.assertEqual(storage.get_data((1, 3)), {"v": 3})

    def test_expires_stale_entries(self) -> None:
        storage = MemoryStorage(ttl=0.0, max_entries=10)
        storage.update_data((1, 1), {"v": 1})
        storage.set_state((1, 1), _SampleStates.step_one.state)

        self.assertEqual(storage.get_data((1, 1)), {})
        self.assertIsNone(storage.get_state((1, 1)))


class BoundedCacheTests(unittest.TestCase):
    def test_cache_set_purges_expired_and_enforces_max_size(self) -> None:
        cache: dict[str, tuple[int, float]] = {}
        http_client.cache_set(cache, "a", 1, 100.0, ttl=10.0, max_size=2)
        http_client.cache_set(cache, "b", 2, 105.0, ttl=10.0, max_size=2)
        http_client.cache_set(cache, "c", 3, 111.0, ttl=10.0, max_size=2)

        # "a" expired at t=111 (age 11 >= 10), "b" and "c" remain
        self.assertNotIn("a", cache)
        self.assertEqual(set(cache), {"b", "c"})

        http_client.cache_set(cache, "d", 4, 112.0, ttl=10.0, max_size=2)
        self.assertEqual(set(cache), {"c", "d"})

    def test_bounded_set_caps_size(self) -> None:
        cache: dict[str, str] = {}
        for idx in range(5):
            http_client.bounded_set(cache, f"k{idx}", f"v{idx}", max_size=3)
        self.assertEqual(list(cache.keys()), ["k2", "k3", "k4"])


class DispatcherAndBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_commands_callbacks_fsm_and_middleware(self) -> None:
        router = Router(name="test")
        seen: list[tuple[str, object]] = []

        @router.message(Command("buy"))
        async def on_buy(
            message: Message, command: CommandObject, state: FSMContext
        ) -> None:
            await state.set_state(_SampleStates.step_one)
            await state.update_data(args=command.args)
            seen.append(("buy", (message.message_id, command.args)))

        @router.callback_query(CallbackPrefix("buy_ok"))
        async def on_ok(callback: CallbackQuery, state: FSMContext) -> None:
            data = await state.get_data()
            await state.clear()
            seen.append(("ok", (callback.data, data)))

        class BlockMiddleware(BaseMiddleware):
            def __init__(self) -> None:
                self.blocked = False

            async def __call__(self, handler, event, data):
                if self.blocked:
                    seen.append(("blocked", type(event).__name__))
                    return None
                return await handler(event, data)

        dp = Dispatcher()
        mw = BlockMiddleware()
        dp.message.outer_middleware(mw)
        dp.callback_query.outer_middleware(mw)
        dp.include_router(router)

        bot = Bot(token="TEST")
        bot.username = "stock_bot"
        try:
            await dp.process_update(
                bot,
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 42,
                        "chat": {"id": 100, "type": "group"},
                        "from": {"id": 7, "first_name": "Alice", "username": "alice"},
                        "text": "/buy@stock_bot AAPL 100 1",
                    },
                },
            )
            await dp.process_update(
                bot,
                {
                    "update_id": 2,
                    "callback_query": {
                        "id": "cb1",
                        "from": {"id": 7, "first_name": "Alice"},
                        "data": "buy_ok:7",
                        "message": {
                            "message_id": 43,
                            "chat": {"id": 100, "type": "group"},
                        },
                    },
                },
            )
            mw.blocked = True
            await dp.process_update(
                bot,
                {
                    "update_id": 3,
                    "message": {
                        "message_id": 44,
                        "chat": {"id": 100, "type": "group"},
                        "from": {"id": 7, "first_name": "Alice"},
                        "text": "/buy AAPL 100 1",
                    },
                },
            )
        finally:
            await bot.close()

        self.assertEqual(
            seen,
            [
                ("buy", (42, "AAPL 100 1")),
                ("ok", ("buy_ok:7", {"args": "AAPL 100 1"})),
                ("blocked", "Message"),
            ],
        )

    async def test_bot_api_requests_encode_json_and_multipart(self) -> None:
        captured: list[tuple[str, bytes, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            captured.append(
                (method, request.content, request.headers.get("content-type", ""))
            )
            if method == "setMyCommands":
                return httpx.Response(200, json={"ok": True, "result": True})
            if method == "sendMediaGroup":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": [
                            {"message_id": 10, "chat": {"id": 1, "type": "private"}},
                            {"message_id": 11, "chat": {"id": 1, "type": "private"}},
                        ],
                    },
                )
            if method == "sendMessage":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "message_id": 9,
                            "chat": {"id": 1, "type": "private"},
                            "text": "hi",
                        },
                    },
                )
            return httpx.Response(
                400, json={"ok": False, "error_code": 400, "description": "bad"}
            )

        bot = Bot(token="TEST")
        await bot.close()
        bot._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            ok = await bot.set_my_commands([BotCommand("quote", "查行情")])
            self.assertTrue(ok)

            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton("确认", "buy_ok:1")]]
            )
            msg = await bot.send_message(1, "hi", reply_to_message_id=5, reply_markup=kb)
            self.assertEqual(msg.message_id, 9)

            album = await bot.send_media_group(
                1,
                [
                    InputMediaPhoto(BufferedInputFile(b"PNG1", "a.png"), caption="cap"),
                    InputMediaPhoto(BufferedInputFile(b"PNG2", "b.png")),
                ],
                reply_to_message_id=5,
            )
            self.assertEqual(len(album), 2)

            with self.assertRaises(TelegramAPIError):
                await bot.answer_callback_query(callback_query_id="bad")
        finally:
            await bot.close()

        send_payload = json.loads(captured[1][1])
        self.assertEqual(send_payload["parse_mode"], "HTML")
        self.assertEqual(send_payload["reply_parameters"]["message_id"], 5)
        self.assertEqual(
            send_payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
            "buy_ok:1",
        )
        self.assertIn("multipart/form-data", captured[2][2])
        self.assertIn(b"attach://file0", captured[2][1])
        self.assertIn(b"PNG2", captured[2][1])


if __name__ == "__main__":
    unittest.main()
