import os

import aiosqlite
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Sequence

import config


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
    qty: int
    currency: str
    rate: float  # exchange rate to CNY at trade time
    trade_ts: str


async def init_db() -> None:
    db_dir = os.path.dirname(os.path.abspath(config.DB_PATH))
    os.makedirs(db_dir, exist_ok=True)
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                price REAL NOT NULL,
                qty INTEGER NOT NULL,
                trade_ts TEXT NOT NULL
            )"""
        )
        for col, col_def in [
            ("currency", "TEXT NOT NULL DEFAULT 'CNY'"),
            ("rate", "REAL NOT NULL DEFAULT 1.0"),
        ]:
            try:
                await db.execute(
                    f"ALTER TABLE trades ADD COLUMN {col} {col_def}"
                )
            except aiosqlite.OperationalError:
                pass

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(user_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_side ON trades(side)"
        )
        await db.commit()


async def insert_trade(
    user_id: int,
    username: str,
    symbol: str,
    side: Side,
    price: float,
    qty: int,
    currency: str,
    rate: float,
) -> Trade:
    ts = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO trades (user_id, username, symbol, side, price, qty, currency, rate, trade_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, symbol.upper(), side.value, price, qty, currency, rate, ts),
        )
        await db.commit()
        trade_id = cur.lastrowid
    return Trade(
        id=trade_id,
        user_id=user_id,
        username=username,
        symbol=symbol.upper(),
        side=side,
        price=price,
        qty=qty,
        currency=currency,
        rate=rate,
        trade_ts=ts,
    )


def _row_to_trade(r: aiosqlite.Row) -> Trade:
    return Trade(
        id=r["id"],
        user_id=r["user_id"],
        username=r["username"],
        symbol=r["symbol"],
        side=Side(r["side"]),
        price=r["price"],
        qty=r["qty"],
        currency=r["currency"] or "CNY",
        rate=r["rate"] or 1.0,
        trade_ts=r["trade_ts"],
    )


async def get_trades(user_id: int, symbol: str | None = None) -> list[Trade]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if symbol:
            cur = await db.execute(
                "SELECT * FROM trades WHERE user_id = ? AND symbol = ? ORDER BY trade_ts",
                (user_id, symbol.upper()),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM trades WHERE user_id = ? ORDER BY trade_ts",
                (user_id,),
            )
        rows = await cur.fetchall()
    return [_row_to_trade(r) for r in rows]


async def get_all_trades() -> list[Trade]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM trades ORDER BY trade_ts")
        rows = await cur.fetchall()
    return [_row_to_trade(r) for r in rows]


async def get_distinct_symbols() -> list[str]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT symbol FROM trades ORDER BY symbol")
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def get_trade_by_id(trade_id: int) -> Trade | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
        row = await cur.fetchone()
    return _row_to_trade(row) if row else None


async def delete_trade(trade_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM trades WHERE id = ? AND user_id = ?", (trade_id, user_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def get_user_summary(user_id: int) -> dict[str, int]:
    """Return {symbol: net_qty} for a user."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT symbol, "
            "SUM(CASE WHEN side='BUY' THEN qty ELSE -qty END) AS net_qty "
            "FROM trades WHERE user_id = ? GROUP BY symbol HAVING net_qty != 0",
            (user_id,),
        )
        rows = await cur.fetchall()
    return {r["symbol"]: r["net_qty"] for r in rows}


async def get_distinct_users() -> list[tuple[int, str]]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT DISTINCT user_id, username FROM trades ORDER BY user_id"
        )
        rows = await cur.fetchall()
    seen: dict[int, str] = {}
    for uid, uname in rows:
        seen[uid] = uname
    return [(uid, uname) for uid, uname in seen.items()]
