from datetime import datetime, timezone

import aiosqlite

import config
from instrument import (
    normalize_symbol,
    parse_symbol,
    qualified_symbol,
    resolve_instrument,
)
from .schema import (
    position_entries_from_rows,
    row_leverage,
    schema_version,
    stored_qty_to_units,
    units_to_stored_qty,
)
from .types import (
    PositionEntry,
    Side,
    Trade,
    normalize_leverage,
    plan_trade,
    validate_qty_units,
)


async def insert_trade(
    user_id: int,
    username: str,
    symbol: str,
    side: Side,
    price: float,
    qty: int,
    currency: str,
    rate: float,
    leverage: int | float | None = None,
    market: str | None = None,
) -> Trade:
    qty_units = validate_qty_units(qty)
    ts = datetime.now(timezone.utc).isoformat()
    canonical_symbol, canonical_market = resolve_instrument(symbol, market)

    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        version = await schema_version(db)
        position_rows = await (
            await db.execute(
                "SELECT side, qty, leverage FROM trades "
                "WHERE user_id = ? AND symbol = ? AND market = ? "
                "ORDER BY trade_ts, id",
                (user_id, canonical_symbol, canonical_market),
            )
        ).fetchall()
        entries = position_entries_from_rows(
            position_rows, version, canonical_symbol, canonical_market
        )
        trade_plan = plan_trade(entries, side, leverage)
        effective_leverage = trade_plan.leverage
        stored_qty = units_to_stored_qty(qty_units, version)
        cur = await db.execute(
            "INSERT INTO trades "
            "(user_id, username, symbol, market, side, price, qty, leverage, "
            "currency, rate, trade_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                username,
                canonical_symbol,
                canonical_market,
                side.value,
                price,
                stored_qty,
                effective_leverage,
                currency,
                rate,
                ts,
            ),
        )
        await db.commit()
        trade_id = cur.lastrowid

    return Trade(
        id=trade_id,
        user_id=user_id,
        username=username,
        symbol=canonical_symbol,
        side=side,
        price=price,
        qty=qty_units,
        leverage=effective_leverage,
        currency=currency,
        rate=rate,
        trade_ts=ts,
        market=canonical_market,
    )


async def insert_close_trades(
    user_id: int,
    username: str,
    symbol: str,
    side: Side | None,
    price: float,
    currency: str,
    rate: float,
    leverage: int | float | str | None = None,
    market: str | None = None,
) -> list[Trade]:
    """Atomically close all matching leverage buckets for a symbol.

    ``side`` limits an ALL operation to buckets closed by that side, while an
    optional ``leverage`` limits it to one bucket. Passing neither (the
    /close command) closes every long and short bucket.
    """
    canonical_symbol, canonical_market = resolve_instrument(symbol, market)
    ts = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        version = await schema_version(db)
        position_rows = await (
            await db.execute(
                "SELECT side, qty, leverage FROM trades "
                "WHERE user_id = ? AND symbol = ? AND market = ? "
                "ORDER BY trade_ts, id",
                (user_id, canonical_symbol, canonical_market),
            )
        ).fetchall()
        entries = position_entries_from_rows(
            position_rows, version, canonical_symbol, canonical_market
        )
        target_leverage = (
            normalize_leverage(leverage) if leverage is not None else None
        )
        targets = [
            entry
            for entry in entries
            if (
                side is None
                or side == (Side.SELL if entry.qty > 0 else Side.BUY)
            )
            and (
                target_leverage is None
                or entry.leverage == target_leverage
            )
        ]
        if not targets:
            await db.rollback()
            return []

        trades: list[Trade] = []
        for entry in sorted(targets, key=lambda item: item.leverage):
            closing_side = Side.SELL if entry.qty > 0 else Side.BUY
            qty_units = abs(entry.qty)
            stored_qty = units_to_stored_qty(qty_units, version)
            cur = await db.execute(
                "INSERT INTO trades "
                "(user_id, username, symbol, market, side, price, qty, leverage, "
                "currency, rate, trade_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    username,
                    canonical_symbol,
                    canonical_market,
                    closing_side.value,
                    price,
                    stored_qty,
                    entry.leverage,
                    currency,
                    rate,
                    ts,
                ),
            )
            trades.append(
                Trade(
                    id=cur.lastrowid,
                    user_id=user_id,
                    username=username,
                    symbol=canonical_symbol,
                    side=closing_side,
                    price=price,
                    qty=qty_units,
                    leverage=entry.leverage,
                    currency=currency,
                    rate=rate,
                    trade_ts=ts,
                    market=canonical_market,
                )
            )
        await db.commit()
    return trades


def _row_to_trade(row: aiosqlite.Row, version: int) -> Trade:
    return Trade(
        id=row["id"],
        user_id=row["user_id"],
        username=row["username"],
        symbol=row["symbol"],
        side=Side(row["side"]),
        price=row["price"],
        qty=stored_qty_to_units(row["qty"], version),
        leverage=row_leverage(row),
        currency=row["currency"] or "CNY",
        rate=row["rate"] or 1.0,
        trade_ts=row["trade_ts"],
        market=row["market"],
    )


async def get_trades(
    user_id: int, symbol: str | None = None, market: str | None = None
) -> list[Trade]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        version = await schema_version(db)
        if symbol:
            base, forced_market = parse_symbol(symbol)
            requested_market = market or forced_market
            if requested_market is not None:
                canonical_symbol, canonical_market = resolve_instrument(
                    base, requested_market
                )
                cur = await db.execute(
                    "SELECT * FROM trades "
                    "WHERE user_id = ? AND symbol = ? AND market = ? "
                    "ORDER BY trade_ts",
                    (user_id, canonical_symbol, canonical_market),
                )
            else:
                cur = await db.execute(
                    "SELECT * FROM trades WHERE user_id = ? AND symbol = ? "
                    "ORDER BY market, trade_ts",
                    (user_id, normalize_symbol(base)),
                )
        else:
            cur = await db.execute(
                "SELECT * FROM trades WHERE user_id = ? ORDER BY trade_ts, id",
                (user_id,),
            )
        rows = await cur.fetchall()
    return [_row_to_trade(row, version) for row in rows]


async def get_position_entries(
    user_id: int, symbol: str | None = None, market: str | None = None
) -> list[PositionEntry]:
    """Return open positions grouped independently by symbol and leverage."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        version = await schema_version(db)
        if symbol:
            base, forced_market = parse_symbol(symbol)
            requested_market = market or forced_market
            if requested_market is not None:
                canonical_symbol, canonical_market = resolve_instrument(
                    base, requested_market
                )
                cur = await db.execute(
                    "SELECT symbol, market, side, qty, leverage FROM trades "
                    "WHERE user_id = ? AND symbol = ? AND market = ? "
                    "ORDER BY trade_ts, id",
                    (user_id, canonical_symbol, canonical_market),
                )
            else:
                cur = await db.execute(
                    "SELECT symbol, market, side, qty, leverage FROM trades "
                    "WHERE user_id = ? AND symbol = ? "
                    "ORDER BY market, trade_ts, id",
                    (user_id, normalize_symbol(base)),
                )
        else:
            cur = await db.execute(
                "SELECT symbol, market, side, qty, leverage FROM trades "
                "WHERE user_id = ? ORDER BY symbol, market, trade_ts, id",
                (user_id,),
            )
        rows = await cur.fetchall()

    rows_by_instrument: dict[tuple[str, str], list[aiosqlite.Row]] = {}
    for row in rows:
        key = (row["symbol"], row["market"])
        rows_by_instrument.setdefault(key, []).append(row)

    entries: list[PositionEntry] = []
    for (row_symbol, row_market), instrument_rows in rows_by_instrument.items():
        entries.extend(
            position_entries_from_rows(
                instrument_rows, version, row_symbol, row_market
            )
        )
    return entries


async def get_all_trades() -> list[Trade]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        version = await schema_version(db)
        cur = await db.execute("SELECT * FROM trades ORDER BY trade_ts, id")
        rows = await cur.fetchall()
    return [_row_to_trade(row, version) for row in rows]


async def get_distinct_symbols() -> list[str]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT DISTINCT symbol, market FROM trades ORDER BY symbol, market"
        )
        rows = await cur.fetchall()
    return [qualified_symbol(row[0], row[1]) for row in rows]


async def get_trade_by_id(trade_id: int) -> Trade | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        version = await schema_version(db)
        cur = await db.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
        row = await cur.fetchone()
    return _row_to_trade(row, version) if row else None


async def delete_trade(trade_id: int, user_id: int) -> bool:
    return bool(await delete_trades([trade_id], user_id))


async def delete_trades(trade_ids: list[int], user_id: int) -> list[int]:
    """Atomically delete the requested IDs owned by a user.

    Unknown IDs and records belonging to other users are left untouched. The
    returned IDs are the subset that was actually deleted, in request order.
    """
    requested_ids = list(dict.fromkeys(trade_ids))
    if not requested_ids:
        return []

    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        rows = await (
            await db.execute(
                "SELECT id FROM trades WHERE user_id = ?",
                (user_id,),
            )
        ).fetchall()
        owned_ids = {int(row[0]) for row in rows}
        deleted_ids = [trade_id for trade_id in requested_ids if trade_id in owned_ids]
        await db.executemany(
            "DELETE FROM trades WHERE id = ? AND user_id = ?",
            [(trade_id, user_id) for trade_id in deleted_ids],
        )
        await db.commit()
    return deleted_ids


async def delete_trades_by_symbol_leverage(
    user_id: int,
    symbol: str,
    leverage: int | float | str,
    market: str | None = None,
) -> int:
    """Delete every trade in one exact instrument/leverage bucket."""
    canonical_symbol, canonical_market = resolve_instrument(symbol, market)
    canonical_leverage = normalize_leverage(leverage)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM trades "
            "WHERE user_id = ? AND symbol = ? AND market = ? AND leverage = ?",
            (
                user_id,
                canonical_symbol,
                canonical_market,
                canonical_leverage,
            ),
        )
        await db.commit()
        return cur.rowcount


async def get_user_summary(user_id: int) -> dict[str, int]:
    """Return {qualified symbol: signed net units} for a user."""
    summary: dict[str, int] = {}
    for entry in await get_position_entries(user_id):
        key = qualified_symbol(entry.symbol, entry.market)
        summary[key] = summary.get(key, 0) + entry.qty
    return {symbol: qty for symbol, qty in summary.items() if qty != 0}


async def get_distinct_users() -> list[tuple[int, str]]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT DISTINCT user_id, username FROM trades ORDER BY user_id"
        )
        rows = await cur.fetchall()
    seen: dict[int, str] = {}
    for user_id, username in rows:
        seen[user_id] = username
    return list(seen.items())
