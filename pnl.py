from dataclasses import dataclass, field

import models
import quotes
from models import Side, Trade


@dataclass
class Position:
    symbol: str
    market: str
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
class RealizedPnl:
    symbol: str
    market: str
    leverage: float
    currency: str
    realized_pnl: float
    realized_pnl_cny: float
    closed_cost_cny: float

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
    realized: list[RealizedPnl] = field(default_factory=list)
    total_realized_pnl_cny: float = 0.0
    total_closed_cost_cny: float = 0.0

    @property
    def total_pnl_cny(self) -> float:
        return self.total_realized_pnl_cny + self.total_unrealized_pnl_cny

    @property
    def total_pnl_cost_cny(self) -> float:
        """Return denominator: closed entry costs plus open entry costs."""
        return self.total_closed_cost_cny + self.total_cost_cny


@dataclass
class TradeHistory:
    qty: int = 0
    avg_cost: float = 0.0
    avg_cost_cny: float = 0.0
    leverage: float = 1.0
    currency: str = "CNY"
    realized_pnl: float = 0.0
    realized_pnl_cny: float = 0.0
    closed_cost_cny: float = 0.0


def calculate_trade_history(trades: list[Trade]) -> TradeHistory:
    """Account for one symbol/market/leverage bucket using average cost.

    Opposite-side trades realize PnL for the matched quantity. A trade that
    crosses zero opens the excess quantity at that trade's price. Historical
    CNY PnL uses the FX rate stored on each trade, never today's FX rate.
    """
    state = TradeHistory()

    for trade in trades:
        trade_qty = trade.qty if trade.side == Side.BUY else -trade.qty
        trade_cost_cny = trade.price * trade.rate
        state.currency = trade.currency or state.currency
        state.leverage = trade.leverage

        if state.qty == 0:
            state.qty = trade_qty
            state.avg_cost = trade.price
            state.avg_cost_cny = trade_cost_cny
            continue

        same_direction = (
            (state.qty > 0 and trade_qty > 0)
            or (state.qty < 0 and trade_qty < 0)
        )
        if same_direction:
            old_size = abs(state.qty)
            added_size = abs(trade_qty)
            new_size = old_size + added_size
            state.avg_cost = (
                state.avg_cost * old_size + trade.price * added_size
            ) / new_size
            state.avg_cost_cny = (
                state.avg_cost_cny * old_size + trade_cost_cny * added_size
            ) / new_size
            state.qty += trade_qty
            continue

        old_size = abs(state.qty)
        closing_size = min(old_size, abs(trade_qty))
        direction = 1 if state.qty > 0 else -1
        closed_quantity = closing_size / models.QTY_SCALE
        state.realized_pnl += (
            (trade.price - state.avg_cost)
            * closed_quantity
            * direction
            * state.leverage
        )
        state.realized_pnl_cny += (
            (trade_cost_cny - state.avg_cost_cny)
            * closed_quantity
            * direction
            * state.leverage
        )
        state.closed_cost_cny += state.avg_cost_cny * closed_quantity

        new_qty = state.qty + trade_qty
        if abs(trade_qty) < old_size:
            state.qty = new_qty
        elif abs(trade_qty) == old_size:
            state.qty = 0
            state.avg_cost = 0.0
            state.avg_cost_cny = 0.0
        else:
            state.qty = new_qty
            state.avg_cost = trade.price
            state.avg_cost_cny = trade_cost_cny

    if state.qty == 0:
        state.avg_cost = 0.0
        state.avg_cost_cny = 0.0
    return state


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
    state = calculate_trade_history(trades)
    if state.qty == 0:
        return 0.0, 0.0, 1.0
    return state.avg_cost, state.avg_cost_cny, state.leverage


def calculate_cost_basis(trades: list[Trade]) -> tuple[float, float]:
    """Backward-compatible cost-basis view without leverage."""
    avg_cost, avg_cost_cny, _leverage = calculate_position_state(trades)
    return avg_cost, avg_cost_cny


async def compute_user_pnl(
    user_id: int, include_unrealized: bool = True
) -> UserPnl:
    all_trades = await models.get_trades(user_id)
    username = all_trades[0].username if all_trades else "unknown"

    trades_by_bucket: dict[tuple[str, str, float], list[Trade]] = {}
    for trade in all_trades:
        key = (trade.symbol, trade.market, trade.leverage)
        trades_by_bucket.setdefault(key, []).append(trade)

    histories = {
        key: calculate_trade_history(trades)
        for key, trades in trades_by_bucket.items()
    }
    open_instruments = (
        list(
            dict.fromkeys(
                (symbol, market)
                for (symbol, market, _leverage), history in histories.items()
                if history.qty != 0
            )
        )
        if include_unrealized
        else []
    )
    qmap = await quotes.get_quotes(open_instruments) if open_instruments else {}

    positions: list[Position] = []
    realized: list[RealizedPnl] = []
    total_cost_cny = 0.0
    total_market_value_cny = 0.0
    rate_map: dict[str, float] = {}

    for (sym, market, leverage), history in histories.items():
        if history.closed_cost_cny:
            realized.append(
                RealizedPnl(
                    symbol=sym,
                    market=market,
                    leverage=leverage,
                    currency=history.currency,
                    realized_pnl=history.realized_pnl,
                    realized_pnl_cny=history.realized_pnl_cny,
                    closed_cost_cny=history.closed_cost_cny,
                )
            )

        if history.qty == 0 or not include_unrealized:
            continue

        net_qty = history.qty
        avg_cost = history.avg_cost
        avg_cost_cny = history.avg_cost_cny
        currency = history.currency or quotes.market_currency(sym, market)

        if currency not in rate_map:
            rate_map[currency] = await quotes.get_rate(currency)
        cur_rate = rate_map[currency]

        q = qmap.get((quotes.normalize_symbol(sym), market))
        name = q.name if q else sym
        cur_price = q.price if q else 0.0

        cur_price_cny = cur_price * cur_rate
        mv = net_qty * cur_price / models.QTY_SCALE
        mv_cny = mv * cur_rate
        entry_value = abs(net_qty) * avg_cost / models.QTY_SCALE
        cost_cny = abs(net_qty) * avg_cost_cny / models.QTY_SCALE
        pnl = (
            mv - net_qty * avg_cost / models.QTY_SCALE
        ) * history.leverage
        pnl_cny = (
            mv_cny - net_qty * avg_cost_cny / models.QTY_SCALE
        ) * history.leverage
        pnl_pct = pnl / entry_value * 100 if entry_value else 0.0

        positions.append(Position(
            symbol=sym,
            market=market,
            name=name,
            qty=net_qty,
            avg_cost=avg_cost,
            current_price=cur_price,
            currency=currency,
            rate=cur_rate,
            leverage=history.leverage,
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

    return UserPnl(
        user_id=user_id,
        username=username,
        positions=sorted(positions, key=lambda p: p.unrealized_pnl_cny, reverse=True),
        total_unrealized_pnl_cny=sum(p.unrealized_pnl_cny for p in positions),
        total_market_value_cny=total_market_value_cny,
        total_cost_cny=total_cost_cny,
        realized=sorted(realized, key=lambda item: item.realized_pnl_cny, reverse=True),
        total_realized_pnl_cny=sum(item.realized_pnl_cny for item in realized),
        total_closed_cost_cny=sum(item.closed_cost_cny for item in realized),
    )


async def compute_leaderboard(include_unrealized: bool = True) -> list[UserPnl]:
    users = await models.get_distinct_users()
    results: list[UserPnl] = []
    for uid, _uname in users:
        p = (
            await compute_user_pnl(uid)
            if include_unrealized
            else await compute_user_pnl(uid, include_unrealized=False)
        )
        basis = p.total_pnl_cost_cny if include_unrealized else p.total_closed_cost_cny
        if basis > 0:
            results.append(p)

    def return_rate(result: UserPnl) -> float:
        if include_unrealized:
            return result.total_pnl_cny / result.total_pnl_cost_cny
        return result.total_realized_pnl_cny / result.total_closed_cost_cny

    results.sort(
        key=return_rate,
        reverse=True,
    )
    return results
