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


def _weighted_avg_cost_cny(trades: list[Trade]) -> float:
    buy_trades = [t for t in trades if t.side == Side.BUY]
    if not buy_trades:
        return 0.0
    total_cny = sum(t.price * t.qty * t.rate for t in buy_trades)
    total_qty = sum(t.qty for t in buy_trades)
    return total_cny / total_qty if total_qty else 0.0


def _weighted_avg_cost(trades: list[Trade]) -> float:
    buy_trades = [t for t in trades if t.side == Side.BUY]
    if not buy_trades:
        return 0.0
    return sum(t.price * t.qty for t in buy_trades) / sum(t.qty for t in buy_trades)


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
        avg_cost = _weighted_avg_cost(trades)
        avg_cost_cny = _weighted_avg_cost_cny(trades)
        currency = trades[0].currency or quotes.market_currency(sym)

        if currency not in rate_map:
            rate_map[currency] = await quotes.get_rate(currency)
        cur_rate = rate_map[currency]

        q = qmap.get(sym)
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
