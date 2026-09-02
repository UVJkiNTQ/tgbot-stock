from dataclasses import dataclass

import models
import quotes
from models import Side, Trade


@dataclass
class Position:
    symbol: str
    name: str
    qty: int  # signed integer hundredth-share units
    avg_cost: float
    current_price: float
    currency: str
    rate: float  # current rate to CNY
    leverage: float = 1.0

    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0

    avg_cost_cny: float = 0.0
    current_price_cny: float = 0.0
    market_value_cny: float = 0.0
    cost_cny: float = 0.0
    unrealized_pnl_cny: float = 0.0

    @property
    def is_foreign(self) -> bool:
        return self.currency != "CNY"


@dataclass
class UserPnl:
    user_id: int
    username: str
    positions: list[Position]
    total_unrealized_pnl_cny: float
    total_market_value_cny: float
    total_cost_cny: float


def calculate_position_state(trades: list[Trade]) -> tuple[float, float, float]:
    """Return average entry prices and leverage for the open position.

    The input must contain one symbol-and-leverage bucket only.

    ``qty`` is signed internally: buys are positive and sells are negative.
    Trades in the same direction add to the weighted-average entry price;
    opposite trades close the existing position first.  If a trade crosses
    through zero, only the excess opens the new position at that trade's price.

    This supports both long positions (buy then sell) and short positions
    (sell then buy) without changing the persisted trade format.
    """
    qty = 0
    avg_cost = 0.0
    avg_cost_cny = 0.0
    leverage = 1.0

    for trade in trades:
        trade_qty = trade.qty if trade.side == Side.BUY else -trade.qty
        trade_cost_cny = trade.price * trade.rate

        if qty == 0:
            qty = trade_qty
            avg_cost = trade.price
            avg_cost_cny = trade_cost_cny
            leverage = trade.leverage
            continue

        same_direction = (qty > 0 and trade_qty > 0) or (qty < 0 and trade_qty < 0)
        if same_direction:
            old_size = abs(qty)
            added_size = abs(trade_qty)
            new_size = old_size + added_size
            avg_cost = (
                avg_cost * old_size + trade.price * added_size
            ) / new_size
            avg_cost_cny = (
                avg_cost_cny * old_size + trade_cost_cny * added_size
            ) / new_size
            qty += trade_qty
            continue

        old_size = abs(qty)
        closing_size = abs(trade_qty)
        new_qty = qty + trade_qty
        if closing_size < old_size:
            # Partial close: the remaining shares keep their entry price.
            qty = new_qty
        elif closing_size == old_size:
            qty = 0
            avg_cost = 0.0
            avg_cost_cny = 0.0
            leverage = 1.0
        else:
            # The trade reverses the position; its excess establishes the new
            # side and therefore the new entry price.
            qty = new_qty
            avg_cost = trade.price
            avg_cost_cny = trade_cost_cny

    if qty == 0:
        return 0.0, 0.0, 1.0
    return avg_cost, avg_cost_cny, leverage


def calculate_cost_basis(trades: list[Trade]) -> tuple[float, float]:
    """Backward-compatible cost-basis view without leverage."""
    avg_cost, avg_cost_cny, _leverage = calculate_position_state(trades)
    return avg_cost, avg_cost_cny


async def compute_user_pnl(user_id: int) -> UserPnl:
    entries = await models.get_position_entries(user_id)

    if not entries:
        all_trades = await models.get_trades(user_id)
        username = all_trades[0].username if all_trades else "unknown"
        return UserPnl(
            user_id=user_id, username=username, positions=[],
            total_unrealized_pnl_cny=0.0, total_market_value_cny=0.0, total_cost_cny=0.0,
        )

    symbols = list(dict.fromkeys(entry.symbol for entry in entries))
    qmap = await quotes.get_quotes(symbols)

    positions: list[Position] = []
    total_cost_cny = 0.0
    total_market_value_cny = 0.0

    rate_map: dict[str, float] = {}

    trades_by_symbol = {
        symbol: await models.get_trades(user_id, symbol) for symbol in symbols
    }

    for entry in entries:
        sym = entry.symbol
        net_qty = entry.qty
        trades = [
            trade
            for trade in trades_by_symbol[sym]
            if trade.leverage == entry.leverage
        ]
        avg_cost, avg_cost_cny, leverage = calculate_position_state(trades)
        currency = trades[-1].currency or quotes.market_currency(sym)

        if currency not in rate_map:
            rate_map[currency] = await quotes.get_rate(currency)
        cur_rate = rate_map[currency]

        q = qmap.get(quotes.normalize_symbol(sym))
        name = q.name if q else sym
        cur_price = q.price if q else 0.0

        cur_price_cny = cur_price * cur_rate
        mv = net_qty * cur_price / models.QTY_SCALE
        mv_cny = mv * cur_rate
        entry_value = abs(net_qty) * avg_cost / models.QTY_SCALE
        cost_cny = abs(net_qty) * avg_cost_cny / models.QTY_SCALE
        pnl = (
            mv - net_qty * avg_cost / models.QTY_SCALE
        ) * leverage
        pnl_cny = (
            mv_cny - net_qty * avg_cost_cny / models.QTY_SCALE
        ) * leverage
        pnl_pct = pnl / entry_value * 100 if entry_value else 0.0

        positions.append(Position(
            symbol=sym,
            name=name,
            qty=net_qty,
            avg_cost=avg_cost,
            current_price=cur_price,
            currency=currency,
            rate=cur_rate,
            leverage=leverage,
            market_value=mv,
            unrealized_pnl=pnl,
            unrealized_pnl_pct=pnl_pct,
            avg_cost_cny=avg_cost_cny,
            current_price_cny=cur_price_cny,
            market_value_cny=mv_cny,
            cost_cny=cost_cny,
            unrealized_pnl_cny=pnl_cny,
        ))
        total_cost_cny += cost_cny
        total_market_value_cny += mv_cny

    all_trades = await models.get_trades(user_id)
    username = all_trades[0].username if all_trades else "unknown"

    return UserPnl(
        user_id=user_id,
        username=username,
        positions=sorted(positions, key=lambda p: p.unrealized_pnl_cny, reverse=True),
        total_unrealized_pnl_cny=sum(p.unrealized_pnl_cny for p in positions),
        total_market_value_cny=total_market_value_cny,
        total_cost_cny=total_cost_cny,
    )


async def compute_leaderboard() -> list[UserPnl]:
    users = await models.get_distinct_users()
    results: list[UserPnl] = []
    for uid, _uname in users:
        p = await compute_user_pnl(uid)
        if p.total_cost_cny > 0 and p.positions:
            results.append(p)
    results.sort(
        key=lambda p: p.total_unrealized_pnl_cny / p.total_cost_cny, reverse=True
    )
    return results
