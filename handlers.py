import math

from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import models
import pnl
import quotes
from models import Side

router = Router()

PRICE_DEVIATION_PCT = 5.0


def _price_deviation(price: float, market_price: float) -> float:
    if market_price <= 0:
        return 0.0
    return abs(price - market_price) / market_price * 100


class TradeConfirm(StatesGroup):
    buying = State()
    selling = State()


_ACTION_LABELS = {"buy": "买入", "sell": "卖出"}


def _confirm_kb(action: str, user_id: int) -> InlineKeyboardMarkup:
    label = _ACTION_LABELS.get(action, action)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"确认{label}", callback_data=f"{action}_ok:{user_id}"
                ),
                InlineKeyboardButton(text="取消", callback_data=f"cancel:{user_id}"),
            ]
        ]
    )


def _callback_owner(callback: CallbackQuery) -> int | None:
    _, _, owner = callback.data.partition(":")
    try:
        return int(owner)
    except ValueError:
        return None


def _fmt_money(amount: float, currency: str) -> str:
    if currency == "CNY":
        return f"¥{amount:,.4f}"
    return f"{currency} {amount:,.4f}"


HELP_TEXT = """股票持仓 Bot 命令：

/quote SYMBOL — 查实时行情
/buy SYMBOL PRICE QTY — 记录买入（二次确认，自动汇率）
/sell SYMBOL PRICE QTY — 记录卖出（二次确认，自动汇率）
/position — 查看我的持仓和浮盈（统一CNY）
/pnl — 查看我的损益汇总
/lb — 收益率排行榜（CNY口径）
/trades — 查看我的交易记录（含ID）
/del ID — 删除一笔交易记录（不带ID可查看列表）

SYMBOL 示例：600000(A股) 00700(港股) AAPL(美股)
ETF 也支持：510050 159919 02800

代码冲突时强制指定类型（加后缀）：
· 010042.F — 场外基金
· 600000.A — A股  ·  00700.HK — 港股  ·  AAPL.US — 美股"""


@router.message(Command("start", "help"))
async def cmd_help(message: types.Message) -> None:
    await message.reply(HELP_TEXT)


@router.message(Command("quote"))
async def cmd_quote(message: types.Message, command: CommandObject) -> None:
    if not command.args:
        await message.reply("用法：/quote SYMBOL\n示例：/quote 600000")
        return
    symbol = command.args.strip().upper()
    q = await quotes.get_quote(symbol)
    if q is None or q.price == 0:
        await message.reply(f"未找到 {symbol} 的行情数据")
        return

    change = q.price - q.prev_close
    change_pct = (change / q.prev_close * 100) if q.prev_close else 0.0
    sign = "+" if change >= 0 else ""
    market_label = {"A": "A股", "HK": "港股", "US": "美股", "FUND": "基金"}.get(q.market, q.market)
    currency = quotes.quote_currency(q)

    if q.market == "FUND":
        await message.reply(
            f"{q.name} ({q.symbol}) [基金]\n"
            f"估算净值：{q.price:.4f}  单位净值：{q.prev_close:.4f}  {currency}"
        )
        return

    await message.reply(
        f"{q.name} ({q.symbol}) [{market_label}]\n"
        f"现价：{q.price:.4f}  {sign}{change:.4f} ({sign}{change_pct:.2f}%)  {currency}\n"
        f"今开：{q.open:.4f}  昨收：{q.prev_close:.4f}\n"
        f"最高：{q.high:.4f}  最低：{q.low:.4f}"
    )


def _parse_trade_args(args: str) -> tuple[str, float, int] | None:
    if not args:
        return None
    parts = args.strip().split()
    if len(parts) != 3:
        return None
    try:
        price = float(parts[1])
        qty = int(parts[2])
        symbol = parts[0].upper()
    except ValueError:
        return None
    if not math.isfinite(price) or price <= 0 or qty <= 0:
        return None
    return symbol, price, qty


@router.message(Command("buy"))
async def cmd_buy(
    message: types.Message, command: CommandObject, state: FSMContext
) -> None:
    parsed = _parse_trade_args(command.args)
    if parsed is None:
        await message.reply(
            "用法：/buy SYMBOL PRICE QTY\n"
            "示例：/buy 600000 10.50 100\n"
            "港股：/buy 01810 28.9 500"
        )
        return
    symbol, price, qty = parsed

    q = await quotes.get_quote(symbol)
    if q is None or not q.price:
        await message.reply(f"未找到 {symbol} 的行情数据，无法记录买入")
        return
    name = q.name
    symbol = q.symbol
    market_price = q.price
    deviation_pct = _price_deviation(price, market_price)

    currency = quotes.quote_currency(q)
    try:
        rate = await quotes.get_rate(currency)
    except quotes.RateUnavailableError:
        await message.reply(f"暂时无法获取 {currency}/CNY 汇率，请稍后重试")
        return

    total_orig = price * qty
    total_cny = total_orig * rate

    username = message.from_user.username or message.from_user.full_name
    await state.set_state(TradeConfirm.buying)
    await state.update_data(
        user_id=message.from_user.id,
        username=username,
        symbol=symbol,
        price=price,
        qty=qty,
        currency=currency,
        rate=rate,
        market_price=market_price,
        deviation_pct=deviation_pct,
        deviation_ack=False,
    )

    rate_line = ""
    if currency != "CNY":
        rate_line = f"\n汇率 {currency}/CNY: {rate:.4f}  (≈¥{total_cny:,.2f})"

    warn_line = ""
    if deviation_pct > PRICE_DEVIATION_PCT:
        warn_line = f"\n⚠️ 委托价偏离现价({market_price:.4f}) {deviation_pct:.1f}%"

    await message.reply(
        f"确认买入：{name} ({symbol})\n"
        f"{qty}股 @ {_fmt_money(price, currency)} = {_fmt_money(total_orig, currency)}{rate_line}{warn_line}",
        reply_markup=_confirm_kb("buy", message.from_user.id),
    )


@router.message(Command("sell"))
async def cmd_sell(
    message: types.Message, command: CommandObject, state: FSMContext
) -> None:
    parsed = _parse_trade_args(command.args)
    if parsed is None:
        await message.reply("用法：/sell SYMBOL PRICE QTY\n示例：/sell 600000 11.00 50")
        return
    symbol, price, qty = parsed

    q = await quotes.get_quote(symbol)
    if q is None or not q.price:
        await message.reply(f"未找到 {symbol} 的行情数据，无法记录卖出")
        return
    name = q.name
    symbol = q.symbol

    hold_map = await models.get_user_summary(message.from_user.id)
    holding = hold_map.get(symbol, 0)

    if holding <= 0:
        await message.reply(f"你没有 {name} ({symbol}) 的持仓，无法卖出")
        return
    if qty > holding:
        await message.reply(
            f"持仓不足：{name} ({symbol}) 当前持有 {holding} 股，卖出 {qty} 股超出了持仓"
        )
        return

    currency = quotes.quote_currency(q)
    try:
        rate = await quotes.get_rate(currency)
    except quotes.RateUnavailableError:
        await message.reply(f"暂时无法获取 {currency}/CNY 汇率，请稍后重试")
        return
    market_price = q.price
    deviation_pct = _price_deviation(price, market_price)

    trades = await models.get_trades(message.from_user.id, symbol)
    avg_cost, _avg_cost_cny = pnl.calculate_cost_basis(trades)

    total_orig = price * qty
    total_cny = total_orig * rate

    username = message.from_user.username or message.from_user.full_name
    await state.set_state(TradeConfirm.selling)
    await state.update_data(
        user_id=message.from_user.id,
        username=username,
        symbol=symbol,
        price=price,
        qty=qty,
        currency=currency,
        rate=rate,
        market_price=market_price,
        deviation_pct=deviation_pct,
        deviation_ack=False,
    )

    rate_line = ""
    if currency != "CNY":
        rate_line = f"\n汇率 {currency}/CNY: {rate:.4f}  (≈¥{total_cny:,.2f})"

    warn_line = ""
    if deviation_pct > PRICE_DEVIATION_PCT:
        warn_line = f"\n⚠️ 委托价偏离现价({market_price:.4f}) {deviation_pct:.1f}%"

    await message.reply(
        f"确认卖出：{name} ({symbol})\n"
        f"持仓 {holding} 股  成本 {_fmt_money(avg_cost, currency)}\n"
        f"卖出 {qty} 股 @ {_fmt_money(price, currency)} = {_fmt_money(total_orig, currency)}{rate_line}{warn_line}",
        reply_markup=_confirm_kb("sell", message.from_user.id),
    )


async def _verify_confirm_owner(callback: CallbackQuery, data: dict | None = None) -> bool:
    if callback.from_user.id != _callback_owner(callback):
        await callback.answer("只有发起人可以操作此按钮", show_alert=True)
        return False
    if data is not None and (not data or callback.from_user.id != data.get("user_id")):
        await callback.answer("会话已过期，请重新发起", show_alert=True)
        return False
    return True


async def _needs_deviation_reconfirm(
    callback: CallbackQuery, state: FSMContext, data: dict, action: str, label: str
) -> bool:
    if data.get("deviation_pct", 0.0) <= PRICE_DEVIATION_PCT or data.get("deviation_ack"):
        return False
    await state.update_data(deviation_ack=True)
    await callback.message.edit_text(
        f"⚠️ 价格偏离提示\n\n"
        f"{data['symbol']} 委托价 {data['price']:.4f} 与现价 {data['market_price']:.4f}"
        f" 偏离 {data['deviation_pct']:.1f}%（超过 {PRICE_DEVIATION_PCT:.0f}%）\n\n"
        f"确定要按此价格{label}吗？",
        reply_markup=_confirm_kb(action, data["user_id"]),
    )
    await callback.answer()
    return True


@router.callback_query(F.data.startswith("buy_ok"))
async def on_buy_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not await _verify_confirm_owner(callback, data):
        return
    if await _needs_deviation_reconfirm(callback, state, data, "buy", "买入"):
        return
    await state.clear()

    trade = await models.insert_trade(
        data["user_id"], data["username"], data["symbol"],
        Side.BUY, data["price"], data["qty"],
        data["currency"], data["rate"],
    )
    q = await quotes.get_quote(trade.symbol)
    name = q.name if q and q.price else trade.symbol
    await callback.message.edit_text(
        f"已记录买入：{name} ({trade.symbol}) × {trade.qty}"
        f" @ {_fmt_money(trade.price, trade.currency)} (ID: {trade.id})"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sell_ok"))
async def on_sell_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not await _verify_confirm_owner(callback, data):
        return
    if await _needs_deviation_reconfirm(callback, state, data, "sell", "卖出"):
        return
    await state.clear()

    trade = await models.insert_trade(
        data["user_id"], data["username"], data["symbol"],
        Side.SELL, data["price"], data["qty"],
        data["currency"], data["rate"],
    )
    q = await quotes.get_quote(trade.symbol)
    name = q.name if q and q.price else trade.symbol
    await callback.message.edit_text(
        f"已记录卖出：{name} ({trade.symbol}) × {trade.qty}"
        f" @ {_fmt_money(trade.price, trade.currency)} (ID: {trade.id})"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cancel"))
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _verify_confirm_owner(callback):
        return
    await state.clear()
    await callback.message.edit_text("已取消")
    await callback.answer()


def _position_lines(result: pnl.UserPnl) -> list[str]:
    lines = [f"{result.username} 持仓汇总：\n"]
    for p in result.positions:
        sign = "+" if p.unrealized_pnl >= 0 else ""
        sign_cny = "+" if p.unrealized_pnl_cny >= 0 else ""

        if p.is_foreign:
            lines.append(
                f"{p.name} ({p.symbol}) × {p.qty}  [{p.currency}]\n"
                f"  成本 {p.avg_cost:.4f}  现价 {p.current_price:.4f}\n"
                f"  汇率 {p.rate:.4f}  ¥成本 {p.avg_cost_cny:.4f}  ¥现价 {p.current_price_cny:.4f}\n"
                f"  浮盈 {sign}{p.unrealized_pnl:.2f} {p.currency}"
                f" (≈¥{sign_cny}{p.unrealized_pnl_cny:.2f})"
            )
        else:
            lines.append(
                f"{p.name} ({p.symbol}) × {p.qty}\n"
                f"  成本 {p.avg_cost:.4f}  现价 {p.current_price:.4f}\n"
                f"  浮盈 {sign}{p.unrealized_pnl:.2f} ({sign}{p.unrealized_pnl_pct:.2f}%)"
            )

    total_sign = "+" if result.total_unrealized_pnl_cny >= 0 else ""
    total_pct = (
        (result.total_unrealized_pnl_cny / result.total_cost_cny * 100)
        if result.total_cost_cny
        else 0.0
    )
    lines.append(
        f"\n总市值：¥{result.total_market_value_cny:,.2f}\n"
        f"总成本：¥{result.total_cost_cny:,.2f}\n"
        f"总浮盈：{total_sign}¥{result.total_unrealized_pnl_cny:,.2f} ({total_sign}{total_pct:.2f}%)"
    )
    return lines


@router.message(Command("position"))
async def cmd_position(message: types.Message) -> None:
    try:
        result = await pnl.compute_user_pnl(message.from_user.id)
    except quotes.RateUnavailableError:
        await message.reply("暂时无法获取外币汇率，请稍后重试")
        return
    if not result.positions:
        await message.reply("你当前没有持仓")
        return
    await message.reply("\n".join(_position_lines(result)))


@router.message(Command("pnl"))
async def cmd_pnl(message: types.Message) -> None:
    try:
        result = await pnl.compute_user_pnl(message.from_user.id)
    except quotes.RateUnavailableError:
        await message.reply("暂时无法获取外币汇率，请稍后重试")
        return
    if not result.positions:
        await message.reply("你当前没有持仓，无法计算损益")
        return

    total_sign = "+" if result.total_unrealized_pnl_cny >= 0 else ""
    total_pct = (
        (result.total_unrealized_pnl_cny / result.total_cost_cny * 100)
        if result.total_cost_cny
        else 0.0
    )

    lines = [f"{result.username} 损益汇总：\n"]
    for p in result.positions:
        sign = "+" if p.unrealized_pnl_cny >= 0 else ""
        if p.is_foreign:
            sign_orig = "+" if p.unrealized_pnl >= 0 else ""
            lines.append(
                f"{p.symbol}  浮盈 {sign_orig}{p.unrealized_pnl:.2f} {p.currency}"
                f"  (≈¥{sign}{p.unrealized_pnl_cny:.2f})"
            )
        else:
            sign_orig = "+" if p.unrealized_pnl >= 0 else ""
            lines.append(
                f"{p.symbol}  浮盈 {sign_orig}{p.unrealized_pnl:.2f}"
                f" ({sign_orig}{p.unrealized_pnl_pct:.2f}%)"
            )
    lines.append(
        f"\n总浮盈：{total_sign}¥{result.total_unrealized_pnl_cny:,.2f} ({total_sign}{total_pct:.2f}%)"
    )
    await message.reply("\n".join(lines))


@router.message(Command("lb", "leaderboard"))
async def cmd_leaderboard(message: types.Message) -> None:
    try:
        board = await pnl.compute_leaderboard()
    except quotes.RateUnavailableError:
        await message.reply("暂时无法获取外币汇率，请稍后重试")
        return

    if not board:
        await message.reply("还没有人有持仓，开搞吧！")
        return

    lines = ["收益率排行榜（CNY口径）：\n"]
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    for i, p in enumerate(board):
        ret = p.total_unrealized_pnl_cny / p.total_cost_cny * 100
        sign = "+" if ret >= 0 else ""
        medal = medals.get(i, f"{i + 1}.")
        lines.append(
            f"{medal} {p.username}  {sign}{ret:.2f}%  ({sign}¥{p.total_unrealized_pnl_cny:,.2f})"
        )

    await message.reply("\n".join(lines))


def _trade_lines(trades: list[models.Trade], limit: int = 20) -> str:
    lines = []
    for t in trades[-limit:]:
        side_label = "买入" if t.side == Side.BUY else "卖出"
        date = t.trade_ts[:10]
        lines.append(
            f"#{t.id}  {date}  {side_label} {t.symbol} × {t.qty}"
            f" @ {_fmt_money(t.price, t.currency)}"
        )
    return "\n".join(lines)


@router.message(Command("trades"))
async def cmd_trades(message: types.Message) -> None:
    trades = await models.get_trades(message.from_user.id)
    if not trades:
        await message.reply("你还没有任何交易记录")
        return
    await message.reply(
        f"你的交易记录（最近{min(len(trades), 20)}条）：\n\n"
        f"{_trade_lines(trades)}\n\n使用 /del ID 删除某条记录"
    )


@router.message(Command("del"))
async def cmd_delete(message: types.Message, command: CommandObject) -> None:
    if not command.args:
        trades = await models.get_trades(message.from_user.id)
        if not trades:
            await message.reply("你还没有任何交易记录")
            return
        await message.reply(
            f"用法：/del ID\n\n你的交易记录：\n{_trade_lines(trades)}"
        )
        return
    try:
        trade_id = int(command.args.strip())
    except ValueError:
        await message.reply("ID 必须为数字")
        return

    trade = await models.get_trade_by_id(trade_id)
    if trade is None:
        await message.reply(f"未找到记录 #{trade_id}")
        return
    if trade.user_id != message.from_user.id:
        await message.reply("你只能删除自己的交易记录")
        return

    deleted = await models.delete_trade(trade_id, message.from_user.id)
    if deleted:
        await message.reply(f"已删除交易记录 #{trade_id}")
    else:
        await message.reply(f"未找到记录 #{trade_id}")
