use crate::instrument::{detect_market, MARKETS};
use crate::models::types::{
    units_to_quantity, DatabaseUpdateResult, ModelError, PositionEntry, Side, QTY_SCALE,
};
use rusqlite::{types::ValueRef, Connection, OptionalExtension, Row};
use rust_decimal::prelude::*;
use std::collections::HashMap;

pub const DB_SCHEMA_VERSION: i64 = 3;
pub const QTY_NORMALIZED_VERSION: i64 = 1;
const MARKET_SQL: &str = "'A','BSE','FUND','HK','JP','US'";

pub fn schema_version(conn: &Connection) -> Result<i64, ModelError> {
    let ver: i64 = conn.query_row("PRAGMA user_version", [], |row| row.get(0))?;
    Ok(ver)
}

pub fn create_qty_integrity_triggers(conn: &Connection) -> Result<(), ModelError> {
    let condition = "typeof(NEW.qty) != 'integer' OR NEW.qty <= 0";
    conn.execute(
        &format!(
            "CREATE TRIGGER IF NOT EXISTS trades_qty_integer_insert \
             BEFORE INSERT ON trades WHEN {condition} \
             BEGIN SELECT RAISE(ABORT, 'qty must be positive integer units'); END"
        ),
        [],
    )?;
    conn.execute(
        &format!(
            "CREATE TRIGGER IF NOT EXISTS trades_qty_integer_update \
             BEFORE UPDATE OF qty ON trades WHEN {condition} \
             BEGIN SELECT RAISE(ABORT, 'qty must be positive integer units'); END"
        ),
        [],
    )?;
    Ok(())
}

pub fn create_market_integrity_triggers(conn: &Connection) -> Result<(), ModelError> {
    let condition = format!("typeof(NEW.market) != 'text' OR NEW.market NOT IN ({MARKET_SQL})");
    conn.execute(
        &format!(
            "CREATE TRIGGER IF NOT EXISTS trades_market_valid_insert \
             BEFORE INSERT ON trades WHEN {condition} \
             BEGIN SELECT RAISE(ABORT, 'invalid market'); END"
        ),
        [],
    )?;
    conn.execute(
        &format!(
            "CREATE TRIGGER IF NOT EXISTS trades_market_valid_update \
             BEFORE UPDATE OF market ON trades WHEN {condition} \
             BEGIN SELECT RAISE(ABORT, 'invalid market'); END"
        ),
        [],
    )?;
    Ok(())
}

pub fn backfill_markets(conn: &Connection) -> Result<i64, ModelError> {
    let mut stmt = conn.prepare("SELECT id, symbol, market FROM trades ORDER BY id")?;
    let rows = stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<String>>(2)?,
            ))
        })?
        .collect::<Result<Vec<_>, _>>()?;

    let mut updates: Vec<(String, i64)> = Vec::new();
    for (id, symbol, current) in rows {
        let inferred = detect_market(&symbol).unwrap_or_else(|_| "US".to_string());
        let needs_update = match current.as_deref() {
            None | Some("") => true,
            Some("A") if inferred == "FUND" => true,
            _ => false,
        };
        if needs_update {
            updates.push((inferred, id));
        }
    }

    let count = updates.len() as i64;
    if !updates.is_empty() {
        let mut upd = conn.prepare("UPDATE trades SET market = ?1 WHERE id = ?2")?;
        for (market, id) in updates {
            upd.execute(rusqlite::params![market, id])?;
        }
    }
    Ok(count)
}

pub fn ensure_market_schema(conn: &Connection) -> Result<bool, ModelError> {
    let table_sql: Option<String> = conn
        .query_row(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'trades'",
            [],
            |row| row.get(0),
        )
        .optional()?
        .flatten();

    if let Some(sql) = &table_sql {
        let upper = sql.to_uppercase();
        if MARKETS
            .iter()
            .all(|m| upper.contains(&format!("'{m}'")))
        {
            return Ok(false);
        }
    } else {
        return Ok(false);
    }

    for trigger in [
        "trades_qty_integer_insert",
        "trades_qty_integer_update",
        "trades_market_valid_insert",
        "trades_market_valid_update",
    ] {
        conn.execute(&format!("DROP TRIGGER IF EXISTS {trigger}"), [])?;
    }
    for index in [
        "idx_trades_user",
        "idx_trades_symbol",
        "idx_trades_instrument",
        "idx_trades_side",
    ] {
        conn.execute(&format!("DROP INDEX IF EXISTS {index}"), [])?;
    }

    conn.execute("ALTER TABLE trades RENAME TO trades_before_market_upgrade", [])?;
    conn.execute(
        &format!(
            "CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                price REAL NOT NULL,
                qty INTEGER NOT NULL,
                leverage REAL NOT NULL DEFAULT 1.0,
                market TEXT NOT NULL CHECK(market IN ({MARKET_SQL})),
                trade_ts TEXT NOT NULL,
                currency TEXT NOT NULL DEFAULT 'CNY',
                rate REAL NOT NULL DEFAULT 1.0
            )"
        ),
        [],
    )?;
    conn.execute(
        "INSERT INTO trades
            (id, user_id, username, symbol, side, price, qty, leverage,
             market, trade_ts, currency, rate)
        SELECT id, user_id, username, symbol, side, price, qty, leverage,
               market, trade_ts, currency, rate
        FROM trades_before_market_upgrade",
        [],
    )?;
    conn.execute("DROP TABLE trades_before_market_upgrade", [])?;
    Ok(true)
}

pub fn stored_qty_to_units(val: ValueRef<'_>, version: i64) -> Result<i64, ModelError> {
    if version >= QTY_NORMALIZED_VERSION {
        match val {
            ValueRef::Integer(i) => Ok(i),
            _ => Err(ModelError::DatabaseUpdate(
                "已规整数据库中出现非整数 qty".to_string(),
            )),
        }
    } else {
        let dec = match val {
            ValueRef::Integer(i) => Decimal::from(i),
            ValueRef::Real(f) => Decimal::from_str(&format!("{f}"))
                .map_err(|_| ModelError::DatabaseUpdate("invalid legacy qty".to_string()))?,
            ValueRef::Text(bytes) => {
                let s = std::str::from_utf8(bytes)
                    .map_err(|_| ModelError::DatabaseUpdate("invalid legacy qty".to_string()))?;
                Decimal::from_str(s)
                    .map_err(|_| ModelError::DatabaseUpdate("invalid legacy qty".to_string()))?
            }
            _ => {
                return Err(ModelError::DatabaseUpdate(
                    "invalid legacy qty".to_string(),
                ))
            }
        };
        let units = (dec * Decimal::from(QTY_SCALE)).round();
        units
            .to_i64()
            .ok_or_else(|| ModelError::DatabaseUpdate("legacy qty overflow".to_string()))
    }
}

pub fn bind_stored_qty(units: i64, version: i64) -> rusqlite::types::Value {
    if version >= QTY_NORMALIZED_VERSION {
        rusqlite::types::Value::Integer(units)
    } else {
        rusqlite::types::Value::Text(units_to_quantity(units).normalize().to_string())
    }
}

pub fn row_leverage(row: &Row<'_>, idx: usize) -> f64 {
    row.get::<_, Option<f64>>(idx)
        .ok()
        .flatten()
        .unwrap_or(1.0)
}

pub fn position_entries_from_rows(
    rows: &[(String, i64, f64)],
    symbol: &str,
    market: &str,
) -> Vec<PositionEntry> {
    let mut quantities: HashMap<u64, (f64, i64)> = HashMap::new();
    let mut order: Vec<u64> = Vec::new();
    for (side, units, leverage) in rows {
        let delta = if side == Side::Buy.as_str() {
            *units
        } else {
            -*units
        };
        let key = leverage.to_bits();
        let entry = quantities.entry(key).or_insert_with(|| {
            order.push(key);
            (*leverage, 0)
        });
        entry.1 += delta;
    }
    order
        .into_iter()
        .filter_map(|key| {
            let (leverage, qty) = quantities[&key];
            if qty != 0 {
                Some(PositionEntry {
                    symbol: symbol.to_string(),
                    leverage,
                    qty,
                    market: market.to_string(),
                })
            } else {
                None
            }
        })
        .collect()
}

pub fn init_schema(conn: &Connection) -> Result<(), ModelError> {
    let table_existed: bool = conn
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trades'",
            [],
            |_| Ok(()),
        )
        .optional()?
        .is_some();

    conn.execute(
        &format!(
            "CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                price REAL NOT NULL,
                qty INTEGER NOT NULL,
                leverage REAL NOT NULL DEFAULT 1.0,
                market TEXT NOT NULL CHECK(market IN ({MARKET_SQL})),
                trade_ts TEXT NOT NULL
            )"
        ),
        [],
    )?;

    for (col, col_def) in [
        ("currency", "TEXT NOT NULL DEFAULT 'CNY'"),
        ("rate", "REAL NOT NULL DEFAULT 1.0"),
        ("leverage", "REAL NOT NULL DEFAULT 1.0"),
        ("market", "TEXT"),
    ] {
        let _ = conn.execute(&format!("ALTER TABLE trades ADD COLUMN {col} {col_def}"), []);
    }

    backfill_markets(conn)?;
    ensure_market_schema(conn)?;

    let invalid_market: Option<(i64, String)> = conn
        .query_row(
            &format!(
                "SELECT id, market FROM trades WHERE market NOT IN ({MARKET_SQL}) LIMIT 1"
            ),
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    if let Some((id, market)) = invalid_market {
        return Err(ModelError::DatabaseUpdate(format!(
            "交易 #{id} 的市场 '{market}' 无效"
        )));
    }

    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(user_id)", [])?;
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)", [])?;
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_instrument ON trades(symbol, market)",
        [],
    )?;
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_side ON trades(side)", [])?;

    let current_version = schema_version(conn)?;
    if !table_existed || (1..DB_SCHEMA_VERSION).contains(&current_version) {
        conn.execute(&format!("PRAGMA user_version = {DB_SCHEMA_VERSION}"), [])?;
    }
    if schema_version(conn)? >= DB_SCHEMA_VERSION {
        create_qty_integrity_triggers(conn)?;
        create_market_integrity_triggers(conn)?;
    }
    Ok(())
}

pub fn migrate_database(conn: &mut Connection) -> Result<DatabaseUpdateResult, ModelError> {
    conn.execute_batch("BEGIN IMMEDIATE")?;
    let res = (|| -> Result<DatabaseUpdateResult, ModelError> {
        let old_version = schema_version(conn)?;
        let columns = table_columns(conn)?;

        let mut market_column_added = false;
        if !columns.iter().any(|c| c == "market") {
            conn.execute("ALTER TABLE trades ADD COLUMN market TEXT", [])?;
            market_column_added = true;
        }
        let market_rows_updated = backfill_markets(conn)?;
        let market_schema_rebuilt = ensure_market_schema(conn)?;

        if old_version >= DB_SCHEMA_VERSION {
            let changed =
                market_column_added || market_schema_rebuilt || market_rows_updated > 0;
            create_qty_integrity_triggers(conn)?;
            create_market_integrity_triggers(conn)?;
            if changed {
                conn.execute_batch("COMMIT")?;
            } else {
                conn.execute_batch("ROLLBACK")?;
            }
            return Ok(DatabaseUpdateResult {
                updated: changed,
                rows_updated: 0,
                old_version,
                new_version: old_version,
                market_rows_updated,
            });
        }

        let mut rows_updated = 0_i64;
        if old_version < QTY_NORMALIZED_VERSION {
            let invalid: Option<(i64, String)> = conn
                .query_row(
                    "SELECT id, CAST(qty AS TEXT) FROM trades \
                     WHERE typeof(qty) NOT IN ('integer', 'real') \
                     OR qty <= 0 \
                     OR qty > ?1 \
                     OR ABS(qty * ?2 - ROUND(qty * ?3)) > 0.000001 \
                     LIMIT 1",
                    rusqlite::params![i64::MAX / QTY_SCALE, QTY_SCALE, QTY_SCALE],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .optional()?;
            if let Some((id, qty_s)) = invalid {
                return Err(ModelError::DatabaseUpdate(format!(
                    "交易 #{id} 的数量 '{qty_s}' 无法转换为 0.01 股单位"
                )));
            }
            rows_updated = conn.query_row("SELECT COUNT(*) FROM trades", [], |row| row.get(0))?;
            conn.execute(
                "UPDATE trades SET qty = CAST(ROUND(qty * ?1) AS INTEGER)",
                rusqlite::params![QTY_SCALE],
            )?;
        }

        let columns = table_columns(conn)?;
        if !columns.iter().any(|c| c == "leverage") {
            conn.execute(
                "ALTER TABLE trades ADD COLUMN leverage REAL NOT NULL DEFAULT 1.0",
                [],
            )?;
        }

        let invalid_lev: Option<(i64, String)> = conn
            .query_row(
                "SELECT id, CAST(leverage AS TEXT) FROM trades \
                 WHERE typeof(leverage) NOT IN ('integer', 'real') \
                 OR leverage < 1 LIMIT 1",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        if let Some((id, lev_s)) = invalid_lev {
            return Err(ModelError::DatabaseUpdate(format!(
                "交易 #{id} 的杠杆 '{lev_s}' 无效"
            )));
        }

        create_qty_integrity_triggers(conn)?;
        create_market_integrity_triggers(conn)?;
        conn.execute(&format!("PRAGMA user_version = {DB_SCHEMA_VERSION}"), [])?;
        conn.execute_batch("COMMIT")?;

        Ok(DatabaseUpdateResult {
            updated: true,
            rows_updated,
            old_version,
            new_version: DB_SCHEMA_VERSION,
            market_rows_updated,
        })
    })();

    if res.is_err() {
        let _ = conn.execute_batch("ROLLBACK");
    }
    res
}

fn table_columns(conn: &Connection) -> Result<Vec<String>, ModelError> {
    let mut stmt = conn.prepare("PRAGMA table_info(trades)")?;
    let cols = stmt
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<Result<Vec<_>, _>>()?;
    Ok(cols)
}
