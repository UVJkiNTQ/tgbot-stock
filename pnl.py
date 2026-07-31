from dataclasses import dataclass, field

import models
import quotes
from models import Side, Trade


@dataclass
class Position:
    symbol: str
    name: str
    qty: int
    avg_cost: float
    current_price: float
    currency: str
    rate: float  # current rate to CNY

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


def calculate_cost_basis(trades: list[Trade]) -> tuple[float, float]:
    """Return remaining average cost in original currency and CNY.

    Sells remove shares at the running average cost.  This matters after a
    complete exit and re-entry: historical buys must no longer affect the new
    position's cost basis.
    """
    qty = 0
    cost = 0.0
    cost_cny = 0.0
    for trade in trades:
        if trade.side == Side.BUY:
            qty += trade.qty
            cost += trade.price * trade.qty
            cost_cny += trade.price * trade.qty * trade.rate
            continue

        if qty <= 0:
            continue
        sold_qty = min(trade.qty, qty)
        cost -= cost / qty * sold_qty
        cost_cny -= cost_cny / qty * sold_qty
        qty -= sold_qty
        if qty == 0:
            cost = 0.0
            cost_cny = 0.0

    if qty <= 0:
        return 0.0, 0.0
    return cost / qty, cost_cny / qty


async def compute_user_pnl(user_id: int) -> UserPnl:
    summary = await models.get_user_summary(user_id)

    if not summary:
        all_trades = await models.get_trades(user_id)
        username = all_trades[0].username if all_trades else "unknown"
        return UserPnl(
            user_id=user_id, username=username, positions=[],
            total_unrealized_pnl_cny=0.0, total_market_value_cny=0.0, total_cost_cny=0.0,
        )

    symbols = list(summary.keys())
    qmap = await quotes.get_quotes(symbols)

    positions: list[Position] = []
    total_cost_cny = 0.0
    total_market_value_cny = 0.0

    rate_map: dict[str, float] = {}

    for sym, net_qty in summary.items():
        trades = await models.get_trades(user_id, sym)
        avg_cost, avg_cost_cny = calculate_cost_basis(trades)
        currency = trades[-1].currency or quotes.market_currency(sym)

        if currency not in rate_map:
            rate_map[currency] = await quotes.get_rate(currency)
        cur_rate = rate_map[currency]

        q = qmap.get(quotes.normalize_symbol(sym))
        name = q.name if q else sym
        cur_price = q.price if q else 0.0

        cur_price_cny = cur_price * cur_rate
        mv = net_qty * cur_price
        mv_cny = mv * cur_rate
        cost_cny = net_qty * avg_cost_cny
        pnl = mv - net_qty * avg_cost
        pnl_cny = mv_cny - cost_cny
        pnl_pct = (cur_price / avg_cost - 1) * 100 if avg_cost else 0.0

        positions.append(Position(
            symbol=sym,
            name=name,
            qty=net_qty,
            avg_cost=avg_cost,
            current_price=cur_price,
            currency=currency,
            rate=cur_rate,
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
        total_unrealized_pnl_cny=total_market_value_cny - total_cost_cny,
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
