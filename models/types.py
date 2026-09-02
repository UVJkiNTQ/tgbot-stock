import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum


QTY_SCALE = 100
SQLITE_MAX_INTEGER = 2**63 - 1


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Trade:
    id: int | None
    user_id: int
    username: str
    symbol: str
    side: Side
    price: float
    qty: int  # integer hundredth-share units; 100 means one share
    currency: str
    rate: float  # exchange rate to CNY at trade time
    trade_ts: str
    leverage: float = 1.0
    market: str = "A"


@dataclass(frozen=True)
class PositionEntry:
    symbol: str
    leverage: float
    qty: int  # signed integer hundredth-share units
    market: str = "A"


@dataclass(frozen=True)
class TradePlan:
    leverage: float
    current_qty: int
    new_entry: bool


@dataclass
class DatabaseUpdateResult:
    updated: bool
    rows_updated: int
    old_version: int
    new_version: int


class DatabaseUpdateError(RuntimeError):
    pass


class LeverageMismatchError(ValueError):
    pass


def quantity_to_units(qty: int | str | Decimal) -> int:
    """Convert a share quantity to exact hundredth-share integer units."""
    if isinstance(qty, float):
        raise TypeError("float quantities are not accepted; use text or Decimal")
    try:
        quantity = Decimal(str(qty))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid quantity") from exc
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("quantity must be positive and finite")
    units = quantity * QTY_SCALE
    if units != units.to_integral_value():
        raise ValueError("quantity supports at most two decimal places")
    integer_units = int(units)
    if integer_units > SQLITE_MAX_INTEGER:
        raise ValueError("quantity is too large")
    return integer_units


def units_to_quantity(units: int) -> Decimal:
    return Decimal(units) / QTY_SCALE


def validate_qty_units(qty_units: int) -> int:
    if isinstance(qty_units, bool) or not isinstance(qty_units, int):
        raise TypeError("qty must be integer hundredth-share units")
    if qty_units <= 0:
        raise ValueError("qty must be positive")
    if qty_units > SQLITE_MAX_INTEGER:
        raise ValueError("qty is too large")
    return qty_units


def normalize_leverage(leverage: int | float | str | Decimal) -> float:
    try:
        value = Decimal(str(leverage))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid leverage") from exc
    if not value.is_finite() or value < 1:
        raise ValueError("leverage must be finite and at least 1")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError("leverage is too large")
    return normalized


def plan_trade(
    entries: list[PositionEntry],
    side: Side,
    requested_leverage: int | float | str | Decimal | None,
) -> TradePlan:
    """Select the leverage bucket for a non-ALL trade.

    A same-direction order may create a new leverage bucket. An order that
    reduces or reverses an existing bucket must explicitly name that bucket's
    leverage, so an omitted or mistyped leverage cannot close the wrong entry.
    """
    leverage = (
        normalize_leverage(requested_leverage)
        if requested_leverage is not None
        else 1.0
    )
    current_qty = next(
        (entry.qty for entry in entries if entry.leverage == leverage), 0
    )
    order_sign = 1 if side == Side.BUY else -1
    opposite_entries = [
        entry for entry in entries if entry.qty * order_sign < 0
    ]

    if entries and requested_leverage is None:
        if opposite_entries:
            raise LeverageMismatchError(
                "普通反向交易必须显式填写目标杠杆，例如 1x；"
                "也可以使用 ALL 或 /close 一次平仓"
            )
        raise LeverageMismatchError(
            "已有持仓时必须显式填写杠杆；填写其他杠杆会建立新的独立条目"
        )

    if current_qty == 0 and opposite_entries:
        available = "、".join(f"{entry.leverage:g}x" for entry in opposite_entries)
        raise LeverageMismatchError(
            f"没有 {leverage:g}x 的对应反向持仓；可平仓杠杆：{available}。"
            "请填写正确杠杆，或使用 ALL / /close"
        )

    return TradePlan(
        leverage=leverage,
        current_qty=current_qty,
        new_entry=bool(entries) and current_qty == 0,
    )
