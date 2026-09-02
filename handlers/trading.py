from aiogram import Router, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext

import models
import quotes
from .common import (
    PRICE_DEVIATION_PCT,
    TradeConfirm,
    confirm_keyboard,
    format_leverage,
    format_money,
    format_qty,
    parse_amount_trade_args,
    parse_close_args,
    parse_trade_args,
    price_deviation,
)
from models import PositionEntry, Side


router = Router(name="trade_commands")


def _is_closed_by(entry: PositionEntry, side: Side) -> bool:
    return (side == Side.BUY and entry.qty < 0) or (
        side == Side.SELL and entry.qty > 0
    )


def _close_entries_text(entries: list[PositionEntry]) -> str:
    parts = []
    for entry in entries:
        action = "卖出" if entry.qty > 0 else "买入"
        parts.append(
            f"{format_leverage(entry.leverage)} {action}{format_qty(abs(entry.qty))}股"
        )
    return "、".join(parts)


def _trade_usage(side: Side) -> str:
    command = "buy" if side == Side.BUY else "sell"
    example_price = "10.50" if side == Side.BUY else "11.00"
    all_label = "平空仓" if side == Side.BUY else "平多仓"
    return (
        f"用法：/{command} SYMBOL PRICE QTY [Nx]\n"
        f"或：/{command} SYMBOL PRICE ALL [Nx]\n"
        f"示例：/{command} 600000 {example_price} 100 5x\n"
        f"{all_label}：/{command} 600000 {example_price} ALL\n"
        "杠杆必须以 x 结尾，例如 1x、2.5x、5x"
    )


def _amount_trade_usage(side: Side) -> str:
    command = "buya" if side == Side.BUY else "sella"
    action = "买入" if side == Side.BUY else "卖出"
    return (
        f"用法：/{command} SYMBOL PRICE AMOUNT [Nx] [Ns]\n"
        f"示例：/{command} 600000 10.50 10000 5x 100s\n"
        f"按不超过仓位金额计算最大可{action}量；"
        "100s、1s、01s、001s 分别表示最小 100、1、0.1、0.01 股"
    )


@router.message(Command("quote"))
async def cmd_quote(message: types.Message, command: CommandObject) -> None:
    if not command.args:
        await message.reply("用法：/quote SYMBOL\n示例：/quote 600000")
        return
    symbol = command.args.strip().upper()
    quote = await quotes.get_quote(symbol)
    if quote is None or quote.price == 0:
        await message.reply(f"未找到 {symbol} 的行情数据")
        return

    change = quote.price - quote.prev_close
    change_pct = (
        change / quote.prev_close * 100 if quote.prev_close else 0.0
    )
    sign = "+" if change >= 0 else ""
    market_label = {
        "A": "A股",
        "HK": "港股",
        "US": "美股",
        "FUND": "基金",
    }.get(quote.market, quote.market)
    currency = quotes.quote_currency(quote)

    if quote.market == "FUND":
        await message.reply(
            f"{quote.name} ({quote.symbol}) [基金]\n"
            f"估算净值：{quote.price:.4f}  "
            f"单位净值：{quote.prev_close:.4f}  {currency}"
        )
        return

    await message.reply(
        f"{quote.name} ({quote.symbol}) [{market_label}]\n"
        f"现价：{quote.price:.4f}  {sign}{change:.4f} "
        f"({sign}{change_pct:.2f}%)  {currency}\n"
        f"今开：{quote.open:.4f}  昨收：{quote.prev_close:.4f}\n"
        f"最高：{quote.high:.4f}  最低：{quote.low:.4f}"
    )


async def _cmd_trade(
    message: types.Message,
    command: CommandObject,
    state: FSMContext,
    side: Side,
) -> None:
    parsed = parse_trade_args(command.args)
    if parsed is None:
        await message.reply(_trade_usage(side))
        return
    await _cmd_trade_parsed(message, state, side, parsed)


async def _cmd_trade_parsed(
    message: types.Message,
    state: FSMContext,
    side: Side,
    parsed: tuple[str, float, int | str, float | None],
) -> None:
    symbol, price, requested_qty, requested_leverage = parsed
    quote = await quotes.get_quote(symbol)
    action = "买入" if side == Side.BUY else "卖出"
    if quote is None or not quote.price:
        await message.reply(f"未找到 {symbol} 的行情数据，无法记录{action}")
        return
    name = quote.name
    symbol = quote.symbol
    market_price = quote.price
    deviation_pct = price_deviation(price, market_price)

    entries = await models.get_position_entries(
        message.from_user.id, symbol, quote.market
    )
    is_all = requested_qty == "ALL"
    target_entries: list[PositionEntry] = []
    new_entry_notice = ""

    if is_all:
        target_entries = [entry for entry in entries if _is_closed_by(entry, side)]
        if requested_leverage is not None:
            target_entries = [
                entry
                for entry in target_entries
                if entry.leverage == requested_leverage
            ]
        if not target_entries:
            position_label = "空仓" if side == Side.BUY else "多仓"
            leverage_label = (
                f"{format_leverage(requested_leverage)} "
                if requested_leverage is not None
                else ""
            )
            await message.reply(
                f"ALL 仅用于平{leverage_label}{position_label}：{name} ({symbol}) "
                "当前没有对应持仓"
            )
            return
        qty = sum(abs(entry.qty) for entry in target_entries)
        effective_leverage = None
        position_line = (
            f"\n将一次关闭 {len(target_entries)} 个匹配条目："
            f"{_close_entries_text(target_entries)}"
        )
    else:
        assert isinstance(requested_qty, int)
        try:
            trade_plan = models.plan_trade(entries, side, requested_leverage)
        except models.LeverageMismatchError as exc:
            await message.reply(f"操作无效：{exc}")
            return

        qty = requested_qty
        effective_leverage = trade_plan.leverage
        delta = qty if side == Side.BUY else -qty
        position_line = (
            f"\n{format_leverage(effective_leverage)} 条目："
            f"{format_qty(trade_plan.current_qty)} 股"
            f" → {format_qty(trade_plan.current_qty + delta)} 股"
        )
        if trade_plan.new_entry:
            new_entry_notice = (
                f"\n⚠️ 当前已有其他杠杆持仓；"
                f"本次将建立新的 {format_leverage(effective_leverage)} 独立持仓条目"
            )

    currency = quotes.quote_currency(quote)
    try:
        rate = await quotes.get_rate(currency)
    except quotes.RateUnavailableError:
        await message.reply(f"暂时无法获取 {currency}/CNY 汇率，请稍后重试")
        return

    total_orig = price * qty / models.QTY_SCALE
    total_cny = total_orig * rate
    username = message.from_user.username or message.from_user.full_name
    await state.set_state(
        TradeConfirm.buying if side == Side.BUY else TradeConfirm.selling
    )
    await state.update_data(
        user_id=message.from_user.id,
        username=username,
        symbol=symbol,
        market=quote.market,
        price=price,
        qty=qty,
        currency=currency,
        rate=rate,
        market_price=market_price,
        deviation_pct=deviation_pct,
        deviation_ack=False,
        close_all=is_all,
        requested_leverage=requested_leverage,
    )

    leverage_line = (
        ""
        if effective_leverage is None
        else f"\n杠杆 {format_leverage(effective_leverage)}"
    )
    rate_line = ""
    if currency != "CNY":
        rate_line = f"\n汇率 {currency}/CNY: {rate:.4f}  (≈¥{total_cny:,.2f})"
    warn_line = ""
    if deviation_pct > PRICE_DEVIATION_PCT:
        warn_line = (
            f"\n⚠️ 委托价偏离现价({market_price:.4f}) "
            f"{deviation_pct:.1f}%"
        )

    await message.reply(
        f"确认{action}：{name} ({symbol})\n"
        f"{format_qty(qty)}股 @ {format_money(price, currency)}"
        f" = {format_money(total_orig, currency)}"
        f"{leverage_line}{position_line}{new_entry_notice}{rate_line}{warn_line}",
        reply_markup=confirm_keyboard(side.value.lower(), message.from_user.id),
    )


async def _cmd_amount_trade(
    message: types.Message,
    command: CommandObject,
    state: FSMContext,
    side: Side,
) -> None:
    parsed = parse_amount_trade_args(command.args)
    if parsed is None:
        await message.reply(_amount_trade_usage(side))
        return

    symbol, price, amount, qty, min_unit, leverage = parsed
    if qty == 0:
        action = "买入" if side == Side.BUY else "卖出"
        await message.reply(
            f"仓位 {amount:g} 不足：按现价 {price:g}、"
            f"最小单位 {format_qty(min_unit)} 股，无法{action} {symbol}"
        )
        return

    await _cmd_trade_parsed(
        message,
        state,
        side,
        (symbol, price, qty, leverage),
    )


@router.message(Command("buy"))
async def cmd_buy(
    message: types.Message, command: CommandObject, state: FSMContext
) -> None:
    await _cmd_trade(message, command, state, Side.BUY)


@router.message(Command("buya"))
async def cmd_buya(
    message: types.Message, command: CommandObject, state: FSMContext
) -> None:
    await _cmd_amount_trade(message, command, state, Side.BUY)


@router.message(Command("sell"))
async def cmd_sell(
    message: types.Message, command: CommandObject, state: FSMContext
) -> None:
    await _cmd_trade(message, command, state, Side.SELL)


@router.message(Command("sella"))
async def cmd_sella(
    message: types.Message, command: CommandObject, state: FSMContext
) -> None:
    await _cmd_amount_trade(message, command, state, Side.SELL)


@router.message(Command("close"))
async def cmd_close(
    message: types.Message, command: CommandObject, state: FSMContext
) -> None:
    parsed = parse_close_args(command.args)
    if parsed is None:
        await message.reply(
            "用法：/close SYMBOL PRICE\n示例：/close 600000 11.00"
        )
        return
    symbol, price = parsed

    quote = await quotes.get_quote(symbol)
    if quote is None or not quote.price:
        await message.reply(f"未找到 {symbol} 的行情数据，无法平仓")
        return
    name = quote.name
    symbol = quote.symbol

    entries = await models.get_position_entries(
        message.from_user.id, symbol, quote.market
    )
    if not entries:
        await message.reply(f"{name} ({symbol}) 当前没有持仓，无需平仓")
        return

    qty = sum(abs(entry.qty) for entry in entries)
    currency = quotes.quote_currency(quote)
    try:
        rate = await quotes.get_rate(currency)
    except quotes.RateUnavailableError:
        await message.reply(f"暂时无法获取 {currency}/CNY 汇率，请稍后重试")
        return

    market_price = quote.price
    deviation_pct = price_deviation(price, market_price)
    total_orig = price * qty / models.QTY_SCALE
    total_cny = total_orig * rate

    username = message.from_user.username or message.from_user.full_name
    await state.set_state(TradeConfirm.closing)
    await state.update_data(
        user_id=message.from_user.id,
        username=username,
        symbol=symbol,
        market=quote.market,
        price=price,
        qty=qty,
        currency=currency,
        rate=rate,
        market_price=market_price,
        deviation_pct=deviation_pct,
        deviation_ack=False,
        close_all=True,
    )

    rate_line = ""
    if currency != "CNY":
        rate_line = f"\n汇率 {currency}/CNY: {rate:.4f}  (≈¥{total_cny:,.2f})"
    warn_line = ""
    if deviation_pct > PRICE_DEVIATION_PCT:
        warn_line = (
            f"\n⚠️ 委托价偏离现价({market_price:.4f}) "
            f"{deviation_pct:.1f}%"
        )

    await message.reply(
        f"确认一次平仓：{name} ({symbol})\n"
        f"将关闭 {len(entries)} 个条目：{_close_entries_text(entries)}\n"
        f"合计 {format_qty(qty)} 股 @ {format_money(price, currency)}"
        f" = {format_money(total_orig, currency)}{rate_line}{warn_line}",
        reply_markup=confirm_keyboard("close", message.from_user.id),
    )
