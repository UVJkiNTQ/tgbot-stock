import math
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

import models
import quotes


PRICE_DEVIATION_PCT = 5.0
_ACTION_LABELS = {"buy": "买入", "sell": "卖出", "close": "平仓"}


class TradeConfirm(StatesGroup):
    buying = State()
    selling = State()
    closing = State()


def price_deviation(price: float, market_price: float) -> float:
    if market_price <= 0:
        return 0.0
    return abs(price - market_price) / market_price * 100


def confirm_keyboard(action: str, user_id: int) -> InlineKeyboardMarkup:
    label = _ACTION_LABELS.get(action, action)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"确认{label}", callback_data=f"{action}_ok:{user_id}"
                ),
                InlineKeyboardButton(
                    text="取消", callback_data=f"cancel:{user_id}"
                ),
            ]
        ]
    )


def callback_owner(callback: CallbackQuery) -> int | None:
    _, _, owner = callback.data.partition(":")
    try:
        return int(owner)
    except ValueError:
        return None


def format_money(amount: float, currency: str) -> str:
    if currency == "CNY":
        return f"¥{amount:,.4f}"
    return f"{currency} {amount:,.4f}"


def format_qty(qty: int) -> str:
    """Format integer hundredth-share units with at most two decimals."""
    shares = models.units_to_quantity(qty)
    return f"{shares:.2f}".rstrip("0").rstrip(".")


def format_leverage(leverage: int | float) -> str:
    return f"{leverage:g}x"


def parse_leverage(raw: str) -> float:
    token = raw.strip().upper()
    if not token.endswith("X"):
        raise ValueError("leverage must end with x")
    value = token[:-1]
    if not value:
        raise ValueError("missing leverage value")
    return models.normalize_leverage(value)


def parse_min_unit(raw: str) -> int:
    """Parse an ``s``-suffixed lot size into hundredth-share units.

    The compact forms requested by the bot UI use leading zeroes as decimal
    places: ``1s`` is one share, ``01s`` is 0.1 share and ``001s`` is 0.01
    share.  Ordinary decimal spellings such as ``0.1s`` are accepted too.
    """
    token = raw.strip().lower()
    if not token.endswith("s"):
        raise ValueError("minimum unit must end with s")
    value = token[:-1]
    if not value:
        raise ValueError("missing minimum unit")

    if value.isascii() and value.isdigit() and value.startswith("0"):
        value = f"0.{value[1:]}"
    return models.quantity_to_units(value)


def parse_amount_trade_args(
    args: str,
) -> tuple[str, float, Decimal, int, int, float | None] | None:
    """Parse an amount-sized trade and calculate its affordable quantity.

    Returns ``(symbol, price, amount, qty_units, min_unit_units, leverage)``.
    A valid order that cannot afford one minimum unit has ``qty_units == 0``.
    """
    if not args:
        return None
    parts = args.strip().split()
    if len(parts) < 3 or len(parts) > 5:
        return None

    try:
        price = Decimal(parts[1])
        amount = Decimal(parts[2])
    except (InvalidOperation, ValueError):
        return None
    if not price.is_finite() or price <= 0:
        return None
    if not amount.is_finite() or amount <= 0:
        return None

    leverage: float | None = None
    min_unit = 1  # the project's smallest quantity: 0.01 share
    saw_min_unit = False
    try:
        for option in parts[3:]:
            suffix = option[-1:].lower()
            if suffix == "x" and leverage is None:
                leverage = parse_leverage(option)
            elif suffix == "s" and not saw_min_unit:
                min_unit = parse_min_unit(option)
                saw_min_unit = True
            else:
                return None

        unit_cost = price * models.units_to_quantity(min_unit)
        lots = (amount / unit_cost).to_integral_value(rounding=ROUND_FLOOR)
        qty = int(lots) * min_unit
        if qty:
            models.validate_qty_units(qty)

        raw_symbol = parts[0].upper()
        if raw_symbol.endswith((".F", ".A", ".US", ".HK")):
            symbol = raw_symbol
        else:
            symbol = quotes.normalize_symbol(raw_symbol)
        parsed_price = float(price)
        if not math.isfinite(parsed_price):
            return None
    except (ArithmeticError, InvalidOperation, OverflowError, ValueError):
        return None

    return symbol, parsed_price, amount, qty, min_unit, leverage


def parse_trade_args(
    args: str,
) -> tuple[str, float, int | str, float | None] | None:
    if not args:
        return None
    parts = args.strip().split()
    if len(parts) not in (3, 4):
        return None
    try:
        price = float(parts[1])
        qty: int | str
        if parts[2].upper() == "ALL":
            qty = "ALL"
        else:
            qty = models.quantity_to_units(parts[2])
        leverage = parse_leverage(parts[3]) if len(parts) == 4 else None

        raw_symbol = parts[0].upper()
        if raw_symbol.endswith((".F", ".A", ".US", ".HK")):
            symbol = raw_symbol
        else:
            symbol = quotes.normalize_symbol(raw_symbol)
    except ValueError:
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return symbol, price, qty, leverage


def parse_close_args(args: str) -> tuple[str, float] | None:
    if not args:
        return None
    parts = args.strip().split()
    if len(parts) != 2:
        return None
    parsed = parse_trade_args(f"{parts[0]} {parts[1]} ALL")
    if parsed is None:
        return None
    symbol, price, _qty, _leverage = parsed
    return symbol, price


async def verify_confirm_owner(
    callback: CallbackQuery, data: dict | None = None
) -> bool:
    if callback.from_user.id != callback_owner(callback):
        await callback.answer("只有发起人可以操作此按钮", show_alert=True)
        return False
    if data is not None and (
        not data or callback.from_user.id != data.get("user_id")
    ):
        await callback.answer("会话已过期，请重新发起", show_alert=True)
        return False
    return True


async def needs_deviation_reconfirm(
    callback: CallbackQuery,
    state: FSMContext,
    data: dict,
    action: str,
    label: str,
) -> bool:
    if (
        data.get("deviation_pct", 0.0) <= PRICE_DEVIATION_PCT
        or data.get("deviation_ack")
    ):
        return False
    await state.update_data(deviation_ack=True)
    await callback.message.edit_text(
        "⚠️ 价格偏离提示\n\n"
        f"{data['symbol']} 委托价 {data['price']:.4f} "
        f"与现价 {data['market_price']:.4f}"
        f" 偏离 {data['deviation_pct']:.1f}%"
        f"（超过 {PRICE_DEVIATION_PCT:.0f}%）\n\n"
        f"确定要按此价格{label}吗？",
        reply_markup=confirm_keyboard(action, data["user_id"]),
    )
    await callback.answer()
    return True
