import os
from decimal import Decimal

import aiosqlite

import config
from instrument import detect_market
from .types import (
    DatabaseUpdateError,
    DatabaseUpdateResult,
    PositionEntry,
    QTY_SCALE,
    SQLITE_MAX_INTEGER,
    Side,
    units_to_quantity,
)


# v0 -> v1 normalized quantities and added leverage/market identity. v1 -> v2
# expands the market domain to include the Beijing Stock Exchange.
DB_SCHEMA_VERSION = 2
QTY_NORMALIZED_VERSION = 1


async def schema_version(db: aiosqlite.Connection) -> int:
    row = await (await db.execute("PRAGMA user_version")).fetchone()
    return int(row[0]) if row else 0


async def create_qty_integrity_triggers(db: aiosqlite.Connection) -> None:
    condition = "typeof(NEW.qty) != 'integer' OR NEW.qty <= 0"
    await db.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trades_qty_integer_insert
        BEFORE INSERT ON trades WHEN {condition}
        BEGIN SELECT RAISE(ABORT, 'qty must be positive integer units'); END"""
    )
    await db.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trades_qty_integer_update
        BEFORE UPDATE OF qty ON trades WHEN {condition}
        BEGIN SELECT RAISE(ABORT, 'qty must be positive integer units'); END"""
    )


async def create_market_integrity_triggers(db: aiosqlite.Connection) -> None:
    condition = (
        "typeof(NEW.market) != 'text' OR "
        "NEW.market NOT IN ('A','HK','US','FUND','BSE')"
    )
    await db.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trades_market_valid_insert
        BEFORE INSERT ON trades WHEN {condition}
        BEGIN SELECT RAISE(ABORT, 'invalid market'); END"""
    )
    await db.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trades_market_valid_update
        BEFORE UPDATE OF market ON trades WHEN {condition}
        BEGIN SELECT RAISE(ABORT, 'invalid market'); END"""
        )


async def backfill_markets(db: aiosqlite.Connection) -> int:
    """Fill legacy market values and repair the known six-digit fund case.

    v0 did not persist market identity.  Most rows can retain the old
    inference, but a code in the fund-only ``004xxx``-``019xxx`` namespace
    must not remain in the A-share bucket after an older init has populated
    ``market``.  Explicitly stored non-empty markets are otherwise preserved
    so this routine cannot overwrite a deliberate ``.A``/``.F`` choice.
    """
    rows = await (
        await db.execute("SELECT id, symbol, market FROM trades ORDER BY id")
    ).fetchall()
    updates: list[tuple[str, int]] = []
    for row in rows:
        inferred = detect_market(row[1])
        current = row[2]
        if current is None or current == "" or (
            current == "A" and inferred == "FUND"
        ):
            updates.append((inferred, row[0]))

    if updates:
        await db.executemany(
            "UPDATE trades SET market = ? WHERE id = ?", updates
        )
    return len(updates)


async def ensure_market_schema(db: aiosqlite.Connection) -> bool:
    """Ensure the trades table CHECK constraint accepts every market.

    SQLite cannot alter a CHECK constraint in place. Existing v1 databases
    therefore need a transactional table rebuild before BSE rows can be
    inserted. All columns and IDs are copied by name; indexes and triggers
    are recreated by the caller.
    """
    table_row = await (
        await db.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'trades'"
        )
    ).fetchone()
    table_sql = (table_row[0] or "").upper() if table_row else ""
    if "BSE" in table_sql:
        return False

    for trigger in (
        "trades_qty_integer_insert",
        "trades_qty_integer_update",
        "trades_market_valid_insert",
        "trades_market_valid_update",
    ):
        await db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for index in (
        "idx_trades_user",
        "idx_trades_symbol",
        "idx_trades_instrument",
        "idx_trades_side",
    ):
        await db.execute(f"DROP INDEX IF EXISTS {index}")

    await db.execute("ALTER TABLE trades RENAME TO trades_before_bse")
    await db.execute(
        """CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
            price REAL NOT NULL,
            qty INTEGER NOT NULL,
            leverage REAL NOT NULL DEFAULT 1.0,
            market TEXT NOT NULL CHECK(market IN ('A','HK','US','FUND','BSE')),
            trade_ts TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'CNY',
            rate REAL NOT NULL DEFAULT 1.0
        )"""
    )
    await db.execute(
        """INSERT INTO trades
            (id, user_id, username, symbol, side, price, qty, leverage,
             market, trade_ts, currency, rate)
        SELECT id, user_id, username, symbol, side, price, qty, leverage,
               market, trade_ts, currency, rate
        FROM trades_before_bse"""
    )
    await db.execute("DROP TABLE trades_before_bse")
    return True


def stored_qty_to_units(stored_value: int | float, version: int) -> int:
    if version >= QTY_NORMALIZED_VERSION:
        if isinstance(stored_value, bool) or not isinstance(stored_value, int):
            raise DatabaseUpdateError("已规整数据库中出现非整数 qty")
        return stored_value
    # Legacy rows store shares directly. Convert through decimal text so no
    # binary float participates in application-level quantity arithmetic.
    return int((Decimal(str(stored_value)) * QTY_SCALE).to_integral_value())


def units_to_stored_qty(units: int, version: int) -> int | str:
    if version >= QTY_NORMALIZED_VERSION:
        return units
    # SQLite applies numeric affinity to this exact decimal string. This path
    # exists only during the compatibility window before /update.
    return format(units_to_quantity(units), "f")


def row_leverage(row: aiosqlite.Row) -> float:
    return float(row["leverage"]) if "leverage" in row.keys() else 1.0


def position_entries_from_rows(
    rows: list[aiosqlite.Row], version: int, symbol: str, market: str
) -> list[PositionEntry]:
    """Aggregate open quantities into independent leverage buckets."""
    quantities: dict[float, int] = {}
    for row in rows:
        units = stored_qty_to_units(row["qty"], version)
        delta = units if row["side"] == Side.BUY.value else -units
        leverage = row_leverage(row)
        quantities[leverage] = quantities.get(leverage, 0) + delta
    return [
        PositionEntry(
            symbol=symbol, leverage=leverage, qty=qty, market=market
        )
        for leverage, qty in quantities.items()
        if qty != 0
    ]


async def init_db() -> None:
    db_dir = os.path.dirname(os.path.abspath(config.DB_PATH))
    os.makedirs(db_dir, exist_ok=True)
    async with aiosqlite.connect(config.DB_PATH) as db:
        table_existed = (
            await (
                await db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trades'"
                )
            ).fetchone()
            is not None
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                price REAL NOT NULL,
                qty INTEGER NOT NULL,
                leverage REAL NOT NULL DEFAULT 1.0,
                market TEXT NOT NULL CHECK(market IN ('A','HK','US','FUND','BSE')),
                trade_ts TEXT NOT NULL
            )"""
        )
        for col, col_def in [
            ("currency", "TEXT NOT NULL DEFAULT 'CNY'"),
            ("rate", "REAL NOT NULL DEFAULT 1.0"),
            ("leverage", "REAL NOT NULL DEFAULT 1.0"),
            ("market", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_def}")
            except aiosqlite.OperationalError:
                pass

        # Backfill identity for v0 databases and pre-release v1 builds,
        # including rows that an older build already mislabeled A.
        await backfill_markets(db)
        await ensure_market_schema(db)
        invalid_market = await (
            await db.execute(
                "SELECT id, market FROM trades "
                "WHERE market NOT IN ('A','HK','US','FUND','BSE') LIMIT 1"
            )
        ).fetchone()
        if invalid_market is not None:
            raise DatabaseUpdateError(
                f"交易 #{invalid_market[0]} 的市场 {invalid_market[1]!r} 无效"
            )

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(user_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_instrument "
            "ON trades(symbol, market)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_side ON trades(side)"
        )
        current_version = await schema_version(db)
        if not table_existed or current_version == 1:
            await db.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
        if await schema_version(db) >= DB_SCHEMA_VERSION:
            await create_qty_integrity_triggers(db)
            await create_market_integrity_triggers(db)
        await db.commit()


async def update_database() -> DatabaseUpdateResult:
    """Idempotently migrate the database to the current schema version."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # The version is checked while holding the write lock, so two
            # concurrent /update calls cannot migrate the rows twice.
            old_version = await schema_version(db)

            columns = {
                row[1]
                for row in await (
                    await db.execute("PRAGMA table_info(trades)")
                ).fetchall()
            }
            market_column_added = False
            if "market" not in columns:
                await db.execute("ALTER TABLE trades ADD COLUMN market TEXT")
                market_column_added = True
            market_rows_updated = await backfill_markets(db)
            market_schema_rebuilt = await ensure_market_schema(db)

            if old_version >= DB_SCHEMA_VERSION:
                changed = (
                    market_column_added
                    or market_schema_rebuilt
                    or bool(market_rows_updated)
                )
                await create_qty_integrity_triggers(db)
                await create_market_integrity_triggers(db)
                if changed:
                    await db.commit()
                else:
                    await db.rollback()
                return DatabaseUpdateResult(
                    updated=changed,
                    rows_updated=0,
                    old_version=old_version,
                    new_version=old_version,
                    market_rows_updated=market_rows_updated,
                )

            rows_updated = 0
            if old_version < QTY_NORMALIZED_VERSION:
                invalid_row = await (
                    await db.execute(
                        "SELECT id, qty FROM trades "
                        "WHERE typeof(qty) NOT IN ('integer', 'real') "
                        "OR qty <= 0 "
                        "OR qty > ? "
                        "OR ABS(qty * ? - ROUND(qty * ?)) > 0.000001 "
                        "LIMIT 1",
                        (SQLITE_MAX_INTEGER // QTY_SCALE, QTY_SCALE, QTY_SCALE),
                    )
                ).fetchone()
                if invalid_row is not None:
                    raise DatabaseUpdateError(
                        f"交易 #{invalid_row[0]} 的数量 {invalid_row[1]!r} "
                        "无法转换为 0.01 股单位"
                    )

                count_row = await (
                    await db.execute("SELECT COUNT(*) FROM trades")
                ).fetchone()
                rows_updated = int(count_row[0]) if count_row else 0
                await db.execute(
                    "UPDATE trades SET qty = CAST(ROUND(qty * ?) AS INTEGER)",
                    (QTY_SCALE,),
                )

            columns = {
                row[1]
                for row in await (
                    await db.execute("PRAGMA table_info(trades)")
                ).fetchall()
            }
            if "leverage" not in columns:
                await db.execute(
                    "ALTER TABLE trades "
                    "ADD COLUMN leverage REAL NOT NULL DEFAULT 1.0"
                )

            invalid_leverage = await (
                await db.execute(
                    "SELECT id, leverage FROM trades "
                    "WHERE typeof(leverage) NOT IN ('integer', 'real') "
                    "OR leverage < 1 LIMIT 1"
                )
            ).fetchone()
            if invalid_leverage is not None:
                raise DatabaseUpdateError(
                    f"交易 #{invalid_leverage[0]} 的杠杆 "
                    f"{invalid_leverage[1]!r} 无效"
                )

            await create_qty_integrity_triggers(db)
            await create_market_integrity_triggers(db)
            await db.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    return DatabaseUpdateResult(
        updated=True,
        rows_updated=rows_updated,
        old_version=old_version,
        new_version=DB_SCHEMA_VERSION,
        market_rows_updated=market_rows_updated,
    )
