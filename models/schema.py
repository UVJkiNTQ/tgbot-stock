import os
from decimal import Decimal

import aiosqlite

import config
from .types import (
    DatabaseUpdateError,
    DatabaseUpdateResult,
    PositionEntry,
    QTY_SCALE,
    SQLITE_MAX_INTEGER,
    Side,
    units_to_quantity,
)


# This release has one migration only: legacy v0 -> current v1. Quantity
# normalization and leverage backfill are applied together in one transaction.
DB_SCHEMA_VERSION = 1


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


def stored_qty_to_units(stored_value: int | float, version: int) -> int:
    if version >= DB_SCHEMA_VERSION:
        if isinstance(stored_value, bool) or not isinstance(stored_value, int):
            raise DatabaseUpdateError("已规整数据库中出现非整数 qty")
        return stored_value
    # Legacy rows store shares directly. Convert through decimal text so no
    # binary float participates in application-level quantity arithmetic.
    return int((Decimal(str(stored_value)) * QTY_SCALE).to_integral_value())


def units_to_stored_qty(units: int, version: int) -> int | str:
    if version >= DB_SCHEMA_VERSION:
        return units
    # SQLite applies numeric affinity to this exact decimal string. This path
    # exists only during the compatibility window before /update.
    return format(units_to_quantity(units), "f")


def row_leverage(row: aiosqlite.Row) -> float:
    return float(row["leverage"]) if "leverage" in row.keys() else 1.0


def position_entries_from_rows(
    rows: list[aiosqlite.Row], version: int, symbol: str
) -> list[PositionEntry]:
    """Aggregate open quantities into independent leverage buckets."""
    quantities: dict[float, int] = {}
    for row in rows:
        units = stored_qty_to_units(row["qty"], version)
        delta = units if row["side"] == Side.BUY.value else -units
        leverage = row_leverage(row)
        quantities[leverage] = quantities.get(leverage, 0) + delta
    return [
        PositionEntry(symbol=symbol, leverage=leverage, qty=qty)
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
                trade_ts TEXT NOT NULL
            )"""
        )
        for col, col_def in [
            ("currency", "TEXT NOT NULL DEFAULT 'CNY'"),
            ("rate", "REAL NOT NULL DEFAULT 1.0"),
            ("leverage", "REAL NOT NULL DEFAULT 1.0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_def}")
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
        if not table_existed:
            await db.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
        if await schema_version(db) >= DB_SCHEMA_VERSION:
            await create_qty_integrity_triggers(db)
        await db.commit()


async def update_database() -> DatabaseUpdateResult:
    """Idempotently migrate the legacy database from v0 to the single v1."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # The version is checked while holding the write lock, so two
            # concurrent /update calls cannot migrate the rows twice.
            old_version = await schema_version(db)
            if old_version >= DB_SCHEMA_VERSION:
                await db.rollback()
                return DatabaseUpdateResult(
                    updated=False,
                    rows_updated=0,
                    old_version=old_version,
                    new_version=old_version,
                )

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
    )
