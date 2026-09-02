from aiogram import Router, types
from aiogram.filters import Command, CommandObject

import models
import pnl
import quotes
from .common import format_leverage, format_money, format_qty
from models import Side


router = Router(name="reports")


def position_lines(result: pnl.UserPnl) -> list[str]:
    lines = [f"{result.username} 持仓汇总：\n"]
    for position in result.positions:
        sign = "+" if position.unrealized_pnl >= 0 else ""
        sign_cny = "+" if position.unrealized_pnl_cny >= 0 else ""
        direction = "  [空]" if position.qty < 0 else ""
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
    if not result.positions:
        await message.reply("你当前没有持仓，无法计算损益")
        return

    total_sign = "+" if result.total_unrealized_pnl_cny >= 0 else ""
    total_pct = (
        result.total_unrealized_pnl_cny / result.total_cost_cny * 100
        if result.total_cost_cny
        else 0.0
    )

    lines = [f"{result.username} 损益汇总：\n"]
    for position in result.positions:
        sign = "+" if position.unrealized_pnl_cny >= 0 else ""
        direction = " [空]" if position.qty < 0 else ""
        leverage = f" [{format_leverage(position.leverage)}]"
        sign_orig = "+" if position.unrealized_pnl >= 0 else ""
        if position.is_foreign:
            lines.append(
                f"{position.symbol}{direction}{leverage}  浮盈"
                f" {sign_orig}{position.unrealized_pnl:.2f}"
                f" {position.currency}"
                f"  (≈¥{sign}{position.unrealized_pnl_cny:.2f})"
            )
        else:
            lines.append(
                f"{position.symbol}{direction}{leverage}  浮盈"
                f" {sign_orig}{position.unrealized_pnl:.2f}"
                f" ({sign_orig}{position.unrealized_pnl_pct:.2f}%)"
            )
    lines.append(
        f"\n总浮盈：{total_sign}¥{result.total_unrealized_pnl_cny:,.2f} "
        f"({total_sign}{total_pct:.2f}%)"
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
    for index, user_pnl in enumerate(board):
        ret = (
            user_pnl.total_unrealized_pnl_cny
            / user_pnl.total_cost_cny
            * 100
        )
        sign = "+" if ret >= 0 else ""
        medal = medals.get(index, f"{index + 1}.")
        lines.append(
            f"{medal} {user_pnl.username}  {sign}{ret:.2f}%  "
            f"({sign}¥{user_pnl.total_unrealized_pnl_cny:,.2f})"
        )

    await message.reply("\n".join(lines))


def trade_lines(trades: list[models.Trade], limit: int = 20) -> str:
    lines = []
    for trade in trades[-limit:]:
        side_label = "买入" if trade.side == Side.BUY else "卖出"
        date = trade.trade_ts[:10]
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
            f"用法：/del ID\n\n你的交易记录：\n{trade_lines(trades)}"
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
