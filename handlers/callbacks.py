import asyncio
from functools import wraps

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

import models
import quotes
from .common import (
    format_leverage,
    format_money,
    format_qty,
    needs_deviation_reconfirm,
    verify_confirm_owner,
)
from models import Side


router = Router(name="trade_callbacks")
_confirmation_locks: dict[int, asyncio.Lock] = {}


def _confirmation_lock(user_id: int) -> asyncio.Lock:
    """Serialize state-consuming callbacks for one user.

    FSM ``get_data`` and ``clear`` are separate operations.  Without a lock,
    two fast callback updates can both read the same confirmation snapshot
    before either one clears it.
    """
    return _confirmation_locks.setdefault(user_id, asyncio.Lock())


def serialized_confirmation(handler):
    @wraps(handler)
    async def wrapped(callback: CallbackQuery, state: FSMContext) -> None:
        async with _confirmation_lock(callback.from_user.id):
            await handler(callback, state)

    return wrapped


async def insert_confirmed_trades(
    data: dict, side: Side | None
) -> list[models.Trade]:
    if data.get("close_all"):
        return await models.insert_close_trades(
            data["user_id"],
            data["username"],
            data["symbol"],
            side,
            data["price"],
            data["currency"],
            data["rate"],
            data.get("requested_leverage"),
            market=data["market"],
        )
    if side is None:
        return []
    return [
        await models.insert_trade(
            data["user_id"],
            data["username"],
            data["symbol"],
            side,
            data["price"],
            data["qty"],
            data["currency"],
            data["rate"],
            data.get("requested_leverage"),
            market=data["market"],
        )
    ]


def _confirmed_trade_lines(trades: list[models.Trade]) -> str:
    lines = []
    for trade in trades:
        side_label = "买入" if trade.side == Side.BUY else "卖出"
        lines.append(
            f"{format_leverage(trade.leverage)} {side_label} "
            f"{format_qty(trade.qty)} 股"
            f" @ {format_money(trade.price, trade.currency)} (ID: {trade.id})"
        )
    return "\n".join(lines)


@router.callback_query(F.data.startswith("buy_ok"))
@serialized_confirmation
async def on_buy_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not await verify_confirm_owner(callback, data):
        return
    if await needs_deviation_reconfirm(callback, state, data, "buy", "买入"):
        return
    await state.clear()

    try:
        trades = await insert_confirmed_trades(data, Side.BUY)
    except models.LeverageMismatchError as exc:
        await callback.message.edit_text(f"仓位已变化，操作无效：{exc}")
        await callback.answer()
        return
    if not trades:
        await callback.message.edit_text(
            f"仓位已变化，无法执行 ALL：{data['symbol']} 当前已不是空仓"
        )
        await callback.answer()
        return
    trade = trades[0]
    quote = await quotes.get_quote(trade.symbol, trade.market)
    name = quote.name if quote and quote.price else trade.symbol
    if data.get("close_all"):
        await callback.message.edit_text(
            f"已一次平空：{name} ({trade.symbol})\n"
            f"{_confirmed_trade_lines(trades)}"
        )
    else:
        await callback.message.edit_text(
            f"已记录买入：{name} ({trade.symbol}) × {format_qty(trade.qty)}"
            f" @ {format_money(trade.price, trade.currency)}"
            f"  杠杆 {format_leverage(trade.leverage)} (ID: {trade.id})"
        )
    await callback.answer()


@router.callback_query(F.data.startswith("sell_ok"))
@serialized_confirmation
async def on_sell_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not await verify_confirm_owner(callback, data):
        return
    if await needs_deviation_reconfirm(callback, state, data, "sell", "卖出"):
        return
    await state.clear()

    try:
        trades = await insert_confirmed_trades(data, Side.SELL)
    except models.LeverageMismatchError as exc:
        await callback.message.edit_text(f"仓位已变化，操作无效：{exc}")
        await callback.answer()
        return
    if not trades:
        await callback.message.edit_text(
            f"仓位已变化，无法执行 ALL：{data['symbol']} 当前已不是多仓"
        )
        await callback.answer()
        return
    trade = trades[0]
    quote = await quotes.get_quote(trade.symbol, trade.market)
    name = quote.name if quote and quote.price else trade.symbol
    if data.get("close_all"):
        await callback.message.edit_text(
            f"已一次平多：{name} ({trade.symbol})\n"
            f"{_confirmed_trade_lines(trades)}"
        )
    else:
        await callback.message.edit_text(
            f"已记录卖出：{name} ({trade.symbol}) × {format_qty(trade.qty)}"
            f" @ {format_money(trade.price, trade.currency)}"
            f"  杠杆 {format_leverage(trade.leverage)} (ID: {trade.id})"
        )
    await callback.answer()


@router.callback_query(F.data.startswith("close_ok"))
@serialized_confirmation
async def on_close_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not await verify_confirm_owner(callback, data):
        return
    if await needs_deviation_reconfirm(callback, state, data, "close", "平仓"):
        return
    await state.clear()

    # No fixed side: the database transaction closes whichever direction is
    # current at the exact moment the confirmation is written.
    trades = await insert_confirmed_trades(data, None)
    if not trades:
        await callback.message.edit_text(
            f"{data['symbol']} 当前仓位已经为 0，无需平仓"
        )
        await callback.answer()
        return

    trade = trades[0]
    quote = await quotes.get_quote(trade.symbol, trade.market)
    name = quote.name if quote and quote.price else trade.symbol
    await callback.message.edit_text(
        f"已一次平仓：{name} ({trade.symbol})\n"
        f"{_confirmed_trade_lines(trades)}"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cancel"))
@serialized_confirmation
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await verify_confirm_owner(callback):
        return
    await state.clear()
    await callback.message.edit_text("已取消")
    await callback.answer()
