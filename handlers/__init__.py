"""Bot router composition and compatibility exports.

The command implementations are split by responsibility so this module stays
stable as the public entry point used by ``main.py``.
"""

from aiogram import Router

import models
import pnl
import quotes
from .reports import (
    cmd_delete,
    cmd_leaderboard,
    cmd_pnl,
    cmd_position,
    cmd_trades,
    format_trade_time,
    position_lines,
    router as reports_router,
    trade_lines,
)
from .system import HELP_TEXT, cmd_help, cmd_update
from .system import router as system_router
from .callbacks import (
    insert_confirmed_trades,
    on_buy_confirm,
    on_cancel,
    on_close_confirm,
    on_sell_confirm,
    router as trade_callbacks_router,
)
from .trading import (
    cmd_buy,
    cmd_buya,
    cmd_close,
    cmd_quote,
    cmd_sell,
    cmd_sella,
    router as trade_commands_router,
)
from .common import (
    PRICE_DEVIATION_PCT,
    TradeConfirm,
    callback_owner,
    confirm_keyboard,
    format_leverage,
    format_money,
    format_qty,
    needs_deviation_reconfirm,
    parse_amount_trade_args,
    parse_close_args,
    parse_min_unit,
    parse_leverage,
    parse_trade_args,
    price_deviation,
    verify_confirm_owner,
)


router = Router(name="root")
router.include_router(system_router)
router.include_router(trade_commands_router)
router.include_router(trade_callbacks_router)
router.include_router(reports_router)


# Keep the prior module-level names available to callers while new code uses
# the descriptive names in the focused modules.
_price_deviation = price_deviation
_confirm_kb = confirm_keyboard
_callback_owner = callback_owner
_fmt_money = format_money
_fmt_qty = format_qty
_fmt_leverage = format_leverage
_parse_leverage = parse_leverage
_parse_trade_args = parse_trade_args
_parse_amount_trade_args = parse_amount_trade_args
_parse_min_unit = parse_min_unit
_parse_close_args = parse_close_args
_verify_confirm_owner = verify_confirm_owner
_needs_deviation_reconfirm = needs_deviation_reconfirm
_insert_confirmed_trade = insert_confirmed_trades
_position_lines = position_lines
_trade_lines = trade_lines


__all__ = [
    "HELP_TEXT",
    "PRICE_DEVIATION_PCT",
    "TradeConfirm",
    "cmd_buy",
    "cmd_buya",
    "cmd_close",
    "cmd_delete",
    "cmd_help",
    "cmd_leaderboard",
    "cmd_pnl",
    "cmd_position",
    "cmd_quote",
    "cmd_sell",
    "cmd_sella",
    "cmd_trades",
    "cmd_update",
    "models",
    "pnl",
    "quotes",
    "router",
]
