from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Router, types
from aiogram.filters import Command, CommandObject

import config
import models
import pnl
import quotes
from .common import format_leverage, format_money, format_qty, parse_leverage
from models import Side


router = Router(name="reports")

try:
    _DISPLAY_TIMEZONE = ZoneInfo(config.DISPLAY_TIMEZONE)
except ZoneInfoNotFoundError:
    _DISPLAY_TIMEZONE = timezone.utc


def format_trade_time(trade_ts: str) -> str:
    """Render a stored UTC timestamp in the configured local timezone."""
    try:
        timestamp = datetime.fromisoformat(trade_ts)
    except (TypeError, ValueError):
        # Keep malformed/legacy data visible instead of breaking /trades.
        return trade_ts[:10]

    # Older rows may contain a date or a naive ISO timestamp. Treat those as
    # UTC so all records follow the same storage convention.
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(_DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def position_lines(result: pnl.UserPnl) -> list[str]:
    lines = [f"{result.username} 持仓汇总：\n"]
    for position in result.positions:
        direction = "  [空]" if position.qty < 0 else ""
        if not position.quote_available:
            lines.append(
                f"{position.name} ({position.symbol}) × "
                f"{format_qty(position.qty)}{direction}"
                f"  [{position.currency}] "
                f"[{format_leverage(position.leverage)}]\n"
                f"  开仓均价 {position.avg_cost:.4f}\n"
                "  ⚠️ 行情不可用，暂不计入市值、浮盈和汇总"
            )
            continue

        sign = "+" if position.unrealized_pnl >= 0 else ""
        sign_cny = "+" if position.unrealized_pnl_cny >= 0 else ""
        entry_label = "开仓均价" if position.qty < 0 else "成本"

        if position.is_foreign:
            lines.append(
                f"{position.name} ({position.symbol}) × "
                f"{format_qty(position.qty)}{direction}"
                f"  [{position.currency}] "
                f"[{format_leverage(position.leverage)}]\n"
                f"  {entry_label} {position.avg_cost:.4f}  "
                f"现价 {position.current_price:.4f}\n"
                f"  汇率 {position.rate:.4f}  "
                f"¥{entry_label} {position.avg_cost_cny:.4f}"
                f"  ¥现价 {position.current_price_cny:.4f}\n"
                f"  浮盈 {sign}{position.unrealized_pnl:.2f} "
                f"{position.currency}"
                f" (≈¥{sign_cny}{position.unrealized_pnl_cny:.2f})"
            )
        else:
            lines.append(
                f"{position.name} ({position.symbol}) × "
                f"{format_qty(position.qty)}{direction}"
                f"  [{format_leverage(position.leverage)}]\n"
                f"  {entry_label} {position.avg_cost:.4f}  "
                f"现价 {position.current_price:.4f}\n"
                f"  浮盈 {sign}{position.unrealized_pnl:.2f} "
                f"({sign}{position.unrealized_pnl_pct:.2f}%)"
            )

    total_sign = "+" if result.total_unrealized_pnl_cny >= 0 else ""
    total_pct = (
        result.total_unrealized_pnl_cny / result.total_cost_cny * 100
        if result.total_cost_cny
        else 0.0
    )
    value_label = (
        "净市值（空头按负值）："
        if any(position.qty < 0 for position in result.positions)
        else "总市值："
    )
    lines.append(
        f"\n{value_label}¥{result.total_market_value_cny:,.2f}\n"
        f"总成本：¥{result.total_cost_cny:,.2f}\n"
        f"总浮盈：{total_sign}¥{result.total_unrealized_pnl_cny:,.2f} "
        f"({total_sign}{total_pct:.2f}%)"
    )
    unavailable_count = sum(
        not position.quote_available for position in result.positions
    )
    if unavailable_count:
        lines.append(
            f"\n⚠️ {unavailable_count} 个持仓行情不可用，市值、浮盈和汇总暂未计入"
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
    await message.reply("\n".join(position_lines(result)))


@router.message(Command("pnl"))
async def cmd_pnl(message: types.Message) -> None:
    try:
        result = await pnl.compute_user_pnl(message.from_user.id)
    except quotes.RateUnavailableError:
        await message.reply("暂时无法获取外币汇率，请稍后重试")
        return
    if not result.positions and not result.realized:
        await message.reply("你还没有交易记录，无法计算损益")
        return

    lines = [f"{result.username} 损益汇总：\n"]
    lines.append("已实现收益：")
    if result.realized:
        for item in result.realized:
            sign = "+" if item.realized_pnl_cny >= 0 else ""
            sign_orig = "+" if item.realized_pnl >= 0 else ""
            return_pct = item.realized_pnl_cny / item.closed_cost_cny * 100
            if item.is_foreign:
                detail = (
                    f"{sign_orig}{item.realized_pnl:.2f} {item.currency}  "
                    f"(≈¥{sign}{item.realized_pnl_cny:.2f}, "
                    f"{sign}{return_pct:.2f}%)"
                )
            else:
                detail = (
                    f"{sign}¥{item.realized_pnl_cny:.2f} "
                    f"({sign}{return_pct:.2f}%)"
                )
            lines.append(
                f"{item.symbol} [{format_leverage(item.leverage)}]  {detail}"
            )
    else:
        lines.append("暂无已平仓交易")

    lines.append("\n浮动盈亏：")
    if result.positions:
        for position in result.positions:
            if not position.quote_available:
                direction = " [空]" if position.qty < 0 else ""
                leverage = f" [{format_leverage(position.leverage)}]"
                lines.append(
                    f"{position.symbol}{direction}{leverage}  "
                    "⚠️ 行情不可用，暂不计入浮盈"
                )
                continue

            sign = "+" if position.unrealized_pnl_cny >= 0 else ""
            direction = " [空]" if position.qty < 0 else ""
            leverage = f" [{format_leverage(position.leverage)}]"
            sign_orig = "+" if position.unrealized_pnl >= 0 else ""
            if position.is_foreign:
                lines.append(
                    f"{position.symbol}{direction}{leverage}  "
                    f"{sign_orig}{position.unrealized_pnl:.2f}"
                    f" {position.currency}"
                    f"  (≈¥{sign}{position.unrealized_pnl_cny:.2f})"
                )
            else:
                lines.append(
                    f"{position.symbol}{direction}{leverage}  "
                    f"{sign_orig}{position.unrealized_pnl:.2f}"
                    f" ({sign_orig}{position.unrealized_pnl_pct:.2f}%)"
                )
    else:
        lines.append("暂无未平仓持仓")

    realized_sign = "+" if result.total_realized_pnl_cny >= 0 else ""
    unrealized_sign = "+" if result.total_unrealized_pnl_cny >= 0 else ""
    total_sign = "+" if result.total_pnl_cny >= 0 else ""
    total_pct = (
        result.total_pnl_cny / result.total_pnl_cost_cny * 100
        if result.total_pnl_cost_cny
        else 0.0
    )
    lines.append(
        f"\n已实现合计：{realized_sign}¥{result.total_realized_pnl_cny:,.2f}\n"
        f"浮动合计：{unrealized_sign}¥{result.total_unrealized_pnl_cny:,.2f}\n"
        f"历史总收益：{total_sign}¥{result.total_pnl_cny:,.2f} "
        f"({total_sign}{total_pct:.2f}%)"
    )
    await message.reply("\n".join(lines))


@router.message(Command("lb", "leaderboard"))
async def cmd_leaderboard(
    message: types.Message, command: CommandObject
) -> None:
    mode = (command.args or "u").strip().lower()
    if mode not in {"u", "r"}:
        await message.reply(
            "用法：/lb [u|r]\n"
            "u = 历史总收益（包括当前浮盈）\n"
            "r = 已实现收益（不计当前持仓盈亏）"
        )
        return

    include_unrealized = mode == "u"
    try:
        board = await pnl.compute_leaderboard(include_unrealized)
    except quotes.RateUnavailableError:
        await message.reply("暂时无法获取外币汇率，请稍后重试")
        return

    if not board:
        empty_message = (
            "还没有人有交易记录，开搞吧！"
            if include_unrealized
            else "还没有人产生已实现收益"
        )
        await message.reply(empty_message)
        return

    title = (
        "历史总收益排行榜（含浮盈，CNY口径）：\n"
        if include_unrealized
        else "已实现收益排行榜（不含浮盈，CNY口径）：\n"
    )
    lines = [title]
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    for index, user_pnl in enumerate(board):
        if include_unrealized:
            amount = user_pnl.total_pnl_cny
            basis = user_pnl.total_pnl_cost_cny
        else:
            amount = user_pnl.total_realized_pnl_cny
            basis = user_pnl.total_closed_cost_cny
        ret = amount / basis * 100
        sign = "+" if ret >= 0 else ""
        amount_sign = "+" if amount >= 0 else ""
        medal = medals.get(index, f"{index + 1}.")
        lines.append(
            f"{medal} {user_pnl.username}  {sign}{ret:.2f}%  "
            f"({amount_sign}¥{amount:,.2f})"
        )

    await message.reply("\n".join(lines))


def trade_lines(trades: list[models.Trade], limit: int = 20) -> str:
    lines = []
    for trade in trades[-limit:]:
        side_label = "买入" if trade.side == Side.BUY else "卖出"
        date = format_trade_time(trade.trade_ts)
        lines.append(
            f"#{trade.id}  {date}  {side_label} {trade.symbol}"
            f" × {format_qty(trade.qty)}"
            f" @ {format_money(trade.price, trade.currency)}"
            f"  {format_leverage(trade.leverage)}"
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
        f"{trade_lines(trades)}\n\n使用 /del ID 删除某条记录"
    )


@router.message(Command("del"))
async def cmd_delete(message: types.Message, command: CommandObject) -> None:
    if not command.args:
        trades = await models.get_trades(message.from_user.id)
        if not trades:
            await message.reply("你还没有任何交易记录")
            return
        await message.reply(
            "用法：\n"
            "/del ID [ID ...] — 删除一条或多条记录\n"
            "/del SYMBOL Nx — 删除指定代码和杠杆的全部记录\n\n"
            f"你的交易记录：\n{trade_lines(trades)}"
        )
        return

    parts = command.args.strip().split()
    id_tokens = [part[1:] if part.startswith("#") else part for part in parts]
    if all(token.isascii() and token.isdigit() for token in id_tokens):
        trade_ids = list(dict.fromkeys(int(token) for token in id_tokens))
        if not trade_ids or any(trade_id <= 0 for trade_id in trade_ids):
            await message.reply("ID 必须为正整数")
            return

        if len(trade_ids) == 1:
            trade_id = trade_ids[0]
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
            return

        deleted_ids = await models.delete_trades(trade_ids, message.from_user.id)
        missing_ids = [trade_id for trade_id in trade_ids if trade_id not in deleted_ids]
        lines = []
        if deleted_ids:
            lines.append(
                "已删除交易记录："
                + " ".join(f"#{trade_id}" for trade_id in deleted_ids)
            )
        if missing_ids:
            lines.append(
                "未找到或无权删除："
                + " ".join(f"#{trade_id}" for trade_id in missing_ids)
            )
        await message.reply("\n".join(lines))
        return

    if len(parts) != 2:
        await message.reply(
            "用法：/del ID [ID ...] 或 /del SYMBOL Nx"
        )
        return

    symbol, raw_leverage = parts
    try:
        leverage = parse_leverage(raw_leverage)
        deleted_count = await models.delete_trades_by_symbol_leverage(
            message.from_user.id,
            symbol,
            leverage,
        )
    except ValueError:
        await message.reply("代码或杠杆格式无效；杠杆示例：1x、2.5x、5x")
        return

    if deleted_count:
        await message.reply(
            f"已删除 {symbol.upper()} [{format_leverage(leverage)}] "
            f"的全部 {deleted_count} 条交易记录"
        )
    else:
        await message.reply(
            f"未找到 {symbol.upper()} [{format_leverage(leverage)}] 的交易记录"
        )
