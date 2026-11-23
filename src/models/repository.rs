use crate::instrument::{normalize_symbol, parse_symbol, resolve_instrument};
#[cfg(test)]
use crate::instrument::qualified_symbol;
use crate::models::schema::{
    bind_stored_qty, init_schema, migrate_database, position_entries_from_rows, row_leverage,
    schema_version, stored_qty_to_units,
};
use crate::models::types::{
    normalize_leverage_f64, plan_trade, validate_qty_units, DatabaseUpdateResult, ModelError,
    PositionEntry, Side, Trade,
};
use chrono::Utc;
use rusqlite::{params, Connection, OptionalExtension, Row};
#[cfg(test)]
use std::collections::BTreeMap;
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::Path;
use std::sync::Mutex;

pub struct Db {
    conn: Mutex<Connection>,
}

impl Db {
    pub fn open(path: &Path) -> Result<Self, ModelError> {
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                fs::create_dir_all(parent)
                    .map_err(|e| ModelError::Sqlite(format!("failed to create db dir: {e}")))?;
            }
        }
        let conn = Connection::open(path)?;
        conn.execute_batch("PRAGMA busy_timeout = 5000; PRAGMA cache_size = -1024;")?;
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    pub fn init(&self) -> Result<(), ModelError> {
        let conn = self.conn.lock().unwrap();
        init_schema(&conn)
    }

    pub fn update_database(&self) -> Result<DatabaseUpdateResult, ModelError> {
        let mut conn = self.conn.lock().unwrap();
        migrate_database(&mut conn)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn insert_trade(
        &self,
        user_id: i64,
        username: &str,
        symbol: &str,
        side: Side,
        price: f64,
        qty: i64,
        currency: &str,
        rate: f64,
        leverage: Option<f64>,
        market: Option<&str>,
    ) -> Result<Trade, ModelError> {
        let qty_units = validate_qty_units(qty).map_err(ModelError::InvalidInput)?;
        let ts = Utc::now().to_rfc3339();
        let (canonical_symbol, canonical_market) =
            resolve_instrument(symbol, market).map_err(ModelError::InvalidInput)?;

        let conn = self.conn.lock().unwrap();
        conn.execute_batch("BEGIN IMMEDIATE")?;
        let res = (|| -> Result<Trade, ModelError> {
            let version = schema_version(&conn)?;
            let pos_rows = fetch_position_rows(&conn, user_id, &canonical_symbol, &canonical_market, version)?;
            let entries = position_entries_from_rows(&pos_rows, &canonical_symbol, &canonical_market);
            let recent_leverage = if !entries.is_empty() {
                pos_rows.last().map(|r| r.2)
            } else {
                None
            };
            let trade_plan = plan_trade(&entries, side, leverage, recent_leverage)?;
            let effective_leverage = trade_plan.leverage;
            let stored_qty = bind_stored_qty(qty_units, version);

            conn.execute(
                "INSERT INTO trades \
                 (user_id, username, symbol, market, side, price, qty, leverage, currency, rate, trade_ts) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
                params![
                    user_id,
                    username,
                    canonical_symbol,
                    canonical_market,
                    side.as_str(),
                    price,
                    stored_qty,
                    effective_leverage,
                    currency,
                    rate,
                    ts,
                ],
            )?;
            let trade_id = conn.last_insert_rowid();
            conn.execute_batch("COMMIT")?;

            Ok(Trade {
                id: trade_id,
                user_id,
                username: username.to_string(),
                symbol: canonical_symbol,
                side,
                price,
                qty: qty_units,
                currency: currency.to_string(),
                rate,
                trade_ts: ts,
                leverage: effective_leverage,
                market: canonical_market,
            })
        })();

        if res.is_err() {
            let _ = conn.execute_batch("ROLLBACK");
        }
        res
    }

    pub fn get_latest_trade_leverage(
        &self,
        user_id: i64,
        symbol: &str,
        market: Option<&str>,
    ) -> Result<Option<f64>, ModelError> {
        let (canonical_symbol, canonical_market) =
            resolve_instrument(symbol, market).map_err(ModelError::InvalidInput)?;
        let conn = self.conn.lock().unwrap();
        let val: Option<f64> = conn
            .query_row(
                "SELECT leverage FROM trades \
                 WHERE user_id = ?1 AND symbol = ?2 AND market = ?3 \
                 ORDER BY trade_ts DESC, id DESC LIMIT 1",
                params![user_id, canonical_symbol, canonical_market],
                |row| Ok(row_leverage(row, 0)),
            )
            .optional()?;
        Ok(val)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn insert_close_trades(
        &self,
        user_id: i64,
        username: &str,
        symbol: &str,
        side: Option<Side>,
        price: f64,
        currency: &str,
        rate: f64,
        leverage: Option<f64>,
        market: Option<&str>,
    ) -> Result<Vec<Trade>, ModelError> {
        let (canonical_symbol, canonical_market) =
            resolve_instrument(symbol, market).map_err(ModelError::InvalidInput)?;
        let ts = Utc::now().to_rfc3339();
        let target_leverage = match leverage {
            Some(l) => Some(normalize_leverage_f64(l).map_err(ModelError::InvalidInput)?),
            None => None,
        };

        let conn = self.conn.lock().unwrap();
        conn.execute_batch("BEGIN IMMEDIATE")?;
        let res = (|| -> Result<Vec<Trade>, ModelError> {
            let version = schema_version(&conn)?;
            let pos_rows = fetch_position_rows(&conn, user_id, &canonical_symbol, &canonical_market, version)?;
            let entries = position_entries_from_rows(&pos_rows, &canonical_symbol, &canonical_market);

            let mut targets: Vec<PositionEntry> = entries
                .into_iter()
                .filter(|entry| {
                    let closing_side = if entry.qty > 0 { Side::Sell } else { Side::Buy };
                    let side_ok = side.is_none_or(|s| s == closing_side);
                    let lev_ok =
                        target_leverage.is_none_or(|tl| (entry.leverage - tl).abs() < 1e-9);
                    side_ok && lev_ok
                })
                .collect();

            if targets.is_empty() {
                conn.execute_batch("ROLLBACK")?;
                return Ok(Vec::new());
            }

            targets.sort_by(|a, b| a.leverage.total_cmp(&b.leverage));
            let mut trades = Vec::with_capacity(targets.len());
            for entry in targets {
                let closing_side = if entry.qty > 0 { Side::Sell } else { Side::Buy };
                let qty_units = entry.qty.abs();
                let stored_qty = bind_stored_qty(qty_units, version);
                conn.execute(
                    "INSERT INTO trades \
                     (user_id, username, symbol, market, side, price, qty, leverage, currency, rate, trade_ts) \
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
                    params![
                        user_id,
                        username,
                        canonical_symbol,
                        canonical_market,
                        closing_side.as_str(),
                        price,
                        stored_qty,
                        entry.leverage,
                        currency,
                        rate,
                        ts,
                    ],
                )?;
                let trade_id = conn.last_insert_rowid();
                trades.push(Trade {
                    id: trade_id,
                    user_id,
                    username: username.to_string(),
                    symbol: canonical_symbol.clone(),
                    side: closing_side,
                    price,
                    qty: qty_units,
                    currency: currency.to_string(),
                    rate,
                    trade_ts: ts.clone(),
                    leverage: entry.leverage,
                    market: canonical_market.clone(),
                });
            }
            conn.execute_batch("COMMIT")?;
            Ok(trades)
        })();

        if res.is_err() {
            let _ = conn.execute_batch("ROLLBACK");
        }
        res
    }

    pub fn get_trades(
        &self,
        user_id: i64,
        symbol: Option<&str>,
        market: Option<&str>,
    ) -> Result<Vec<Trade>, ModelError> {
        let conn = self.conn.lock().unwrap();
        let version = schema_version(&conn)?;
        let cols = "id, user_id, username, symbol, side, price, qty, leverage, currency, rate, trade_ts, market";

        if let Some(sym) = symbol {
            let (base, forced_market) = parse_symbol(sym).map_err(ModelError::InvalidInput)?;
            let requested_market = market.map(str::to_string).or(forced_market);
            if let Some(req_m) = requested_market {
                let (csym, cmkt) = resolve_instrument(&base, Some(&req_m))
                    .map_err(ModelError::InvalidInput)?;
                let mut stmt = conn.prepare(&format!(
                    "SELECT {cols} FROM trades WHERE user_id = ?1 AND symbol = ?2 AND market = ?3 ORDER BY trade_ts"
                ))?;
                return collect_trades(stmt.query(params![user_id, csym, cmkt])?, version);
            } else {
                let norm = normalize_symbol(&base).map_err(ModelError::InvalidInput)?;
                let mut stmt = conn.prepare(&format!(
                    "SELECT {cols} FROM trades WHERE user_id = ?1 AND symbol = ?2 ORDER BY market, trade_ts"
                ))?;
                return collect_trades(stmt.query(params![user_id, norm])?, version);
            }
        }

        let mut stmt = conn.prepare(&format!(
            "SELECT {cols} FROM trades WHERE user_id = ?1 ORDER BY trade_ts, id"
        ))?;
        collect_trades(stmt.query(params![user_id])?, version)
    }

    pub fn get_position_entries(
        &self,
        user_id: i64,
        symbol: Option<&str>,
        market: Option<&str>,
    ) -> Result<Vec<PositionEntry>, ModelError> {
        let conn = self.conn.lock().unwrap();
        let version = schema_version(&conn)?;

        let mut raw_rows: Vec<(String, String, String, i64, f64)> = Vec::new();
        let mut extract = |mut rows: rusqlite::Rows<'_>| -> Result<(), ModelError> {
            while let Some(row) = rows.next()? {
                let sym: String = row.get(0)?;
                let mkt: String = row.get(1)?;
                let side: String = row.get(2)?;
                let qty = stored_qty_to_units(row.get_ref(3)?, version)?;
                let lev = row_leverage(row, 4);
                raw_rows.push((sym, mkt, side, qty, lev));
            }
            Ok(())
        };

        if let Some(sym) = symbol {
            let (base, forced_market) = parse_symbol(sym).map_err(ModelError::InvalidInput)?;
            let requested_market = market.map(str::to_string).or(forced_market);
            if let Some(req_m) = requested_market {
                let (csym, cmkt) = resolve_instrument(&base, Some(&req_m))
                    .map_err(ModelError::InvalidInput)?;
                let mut stmt = conn.prepare(
                    "SELECT symbol, market, side, qty, leverage FROM trades \
                     WHERE user_id = ?1 AND symbol = ?2 AND market = ?3 ORDER BY trade_ts, id",
                )?;
                extract(stmt.query(params![user_id, csym, cmkt])?)?;
            } else {
                let norm = normalize_symbol(&base).map_err(ModelError::InvalidInput)?;
                let mut stmt = conn.prepare(
                    "SELECT symbol, market, side, qty, leverage FROM trades \
                     WHERE user_id = ?1 AND symbol = ?2 ORDER BY market, trade_ts, id",
                )?;
                extract(stmt.query(params![user_id, norm])?)?;
            }
        } else {
            let mut stmt = conn.prepare(
                "SELECT symbol, market, side, qty, leverage FROM trades \
                 WHERE user_id = ?1 ORDER BY symbol, market, trade_ts, id",
            )?;
            extract(stmt.query(params![user_id])?)?;
        }

        let mut grouped: HashMap<(String, String), Vec<(String, i64, f64)>> = HashMap::new();
        let mut order: Vec<(String, String)> = Vec::new();
        for (sym, mkt, side, qty, lev) in raw_rows {
            let key = (sym, mkt);
            let bucket = grouped.entry(key.clone()).or_insert_with(|| {
                order.push(key);
                Vec::new()
            });
            bucket.push((side, qty, lev));
        }

        let mut entries = Vec::new();
        for key in order {
            let rows = &grouped[&key];
            entries.extend(position_entries_from_rows(rows, &key.0, &key.1));
        }
        Ok(entries)
    }

    pub fn get_trade_by_id(&self, trade_id: i64) -> Result<Option<Trade>, ModelError> {
        let conn = self.conn.lock().unwrap();
        let version = schema_version(&conn)?;
        let mut stmt = conn.prepare(
            "SELECT id, user_id, username, symbol, side, price, qty, leverage, currency, rate, trade_ts, market \
             FROM trades WHERE id = ?1",
        )?;
        let mut rows = stmt.query(params![trade_id])?;
        if let Some(row) = rows.next()? {
            Ok(Some(row_to_trade(row, version)?))
        } else {
            Ok(None)
        }
    }

    pub fn delete_trade(&self, trade_id: i64, user_id: i64) -> Result<bool, ModelError> {
        Ok(!self.delete_trades(&[trade_id], user_id)?.is_empty())
    }

    pub fn delete_trades(&self, trade_ids: &[i64], user_id: i64) -> Result<Vec<i64>, ModelError> {
        let mut requested = Vec::new();
        let mut seen = HashSet::new();
        for &id in trade_ids {
            if seen.insert(id) {
                requested.push(id);
            }
        }
        if requested.is_empty() {
            return Ok(Vec::new());
        }

        let conn = self.conn.lock().unwrap();
        conn.execute_batch("BEGIN IMMEDIATE")?;
        let res = (|| -> Result<Vec<i64>, ModelError> {
            let mut stmt = conn.prepare("SELECT id FROM trades WHERE user_id = ?1")?;
            let owned: HashSet<i64> = stmt
                .query_map(params![user_id], |row| row.get::<_, i64>(0))?
                .collect::<Result<HashSet<_>, _>>()?;
            let deleted: Vec<i64> = requested
                .into_iter()
                .filter(|id| owned.contains(id))
                .collect();
            {
                let mut del =
                    conn.prepare("DELETE FROM trades WHERE id = ?1 AND user_id = ?2")?;
                for &id in &deleted {
                    del.execute(params![id, user_id])?;
                }
            }
            conn.execute_batch("COMMIT")?;
            Ok(deleted)
        })();
        if res.is_err() {
            let _ = conn.execute_batch("ROLLBACK");
        }
        res
    }

    pub fn delete_trades_by_symbol_leverage(
        &self,
        user_id: i64,
        symbol: &str,
        leverage: f64,
        market: Option<&str>,
    ) -> Result<usize, ModelError> {
        let (csym, cmkt) =
            resolve_instrument(symbol, market).map_err(ModelError::InvalidInput)?;
        let clev = normalize_leverage_f64(leverage).map_err(ModelError::InvalidInput)?;
        let conn = self.conn.lock().unwrap();
        let count = conn.execute(
            "DELETE FROM trades WHERE user_id = ?1 AND symbol = ?2 AND market = ?3 AND leverage = ?4",
            params![user_id, csym, cmkt, clev],
        )?;
        Ok(count)
    }

    #[cfg(test)]
    pub fn get_user_summary(&self, user_id: i64) -> Result<BTreeMap<String, i64>, ModelError> {
        let mut summary: BTreeMap<String, i64> = BTreeMap::new();
        for entry in self.get_position_entries(user_id, None, None)? {
            let key = qualified_symbol(&entry.symbol, &entry.market)
                .map_err(ModelError::InvalidInput)?;
            *summary.entry(key).or_insert(0) += entry.qty;
        }
        summary.retain(|_, &mut qty| qty != 0);
        Ok(summary)
    }

    pub fn get_distinct_users(&self) -> Result<Vec<(i64, String)>, ModelError> {
        let conn = self.conn.lock().unwrap();
        let mut stmt =
            conn.prepare("SELECT DISTINCT user_id, username FROM trades ORDER BY user_id")?;
        let rows = stmt
            .query_map([], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)))?
            .collect::<Result<Vec<_>, _>>()?;
        let mut seen: Vec<(i64, String)> = Vec::new();
        let mut idx: HashMap<i64, usize> = HashMap::new();
        for (uid, uname) in rows {
            if let Some(&pos) = idx.get(&uid) {
                seen[pos].1 = uname;
            } else {
                idx.insert(uid, seen.len());
                seen.push((uid, uname));
            }
        }
        Ok(seen)
    }
}

fn fetch_position_rows(
    conn: &Connection,
    user_id: i64,
    symbol: &str,
    market: &str,
    version: i64,
) -> Result<Vec<(String, i64, f64)>, ModelError> {
    let mut stmt = conn.prepare(
        "SELECT side, qty, leverage FROM trades \
         WHERE user_id = ?1 AND symbol = ?2 AND market = ?3 ORDER BY trade_ts, id",
    )?;
    let mut rows = stmt.query(params![user_id, symbol, market])?;
    let mut out = Vec::new();
    while let Some(row) = rows.next()? {
        let side: String = row.get(0)?;
        let qty = stored_qty_to_units(row.get_ref(1)?, version)?;
        let lev = row_leverage(row, 2);
        out.push((side, qty, lev));
    }
    Ok(out)
}

fn row_to_trade(row: &Row<'_>, version: i64) -> Result<Trade, ModelError> {
    let side_str: String = row.get(4)?;
    let side = Side::from_db(&side_str).map_err(ModelError::DatabaseUpdate)?;
    let qty = stored_qty_to_units(row.get_ref(6)?, version)?;
    let leverage = row_leverage(row, 7);
    let currency: String = row
        .get::<_, Option<String>>(8)?
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "CNY".to_string());
    let rate: f64 = row
        .get::<_, Option<f64>>(9)?
        .filter(|&r| r != 0.0)
        .unwrap_or(1.0);
    Ok(Trade {
        id: row.get(0)?,
        user_id: row.get(1)?,
        username: row.get(2)?,
        symbol: row.get(3)?,
        side,
        price: row.get(5)?,
        qty,
        leverage,
        currency,
        rate,
        trade_ts: row.get(10)?,
        market: row.get(11)?,
    })
}

fn collect_trades(mut rows: rusqlite::Rows<'_>, version: i64) -> Result<Vec<Trade>, ModelError> {
    let mut out = Vec::new();
    while let Some(row) = rows.next()? {
        out.push(row_to_trade(row, version)?);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::types::quantity_to_units;
    use tempfile::TempDir;

    fn open_temp() -> (TempDir, Db) {
        let dir = TempDir::new().unwrap();
        let db = Db::open(&dir.path().join("trades.db")).unwrap();
        db.init().unwrap();
        (dir, db)
    }

    #[test]
    fn negative_position_and_close_all() {
        let (_dir, db) = open_temp();
        db.insert_trade(1, "tester", "600000", Side::Sell, 10.0, quantity_to_units("100").unwrap(), "CNY", 1.0, None, None).unwrap();
        db.insert_trade(1, "tester", "600000", Side::Buy, 8.0, quantity_to_units("40").unwrap(), "CNY", 1.0, Some(1.0), None).unwrap();
        let summary = db.get_user_summary(1).unwrap();
        assert_eq!(summary.get("600000.A"), Some(&-6000));

        let closed = db.insert_close_trades(1, "tester", "600000", None, 8.5, "CNY", 1.0, None, None).unwrap();
        assert_eq!(closed.len(), 1);
        assert_eq!(closed[0].side, Side::Buy);
        assert_eq!(closed[0].qty, 6000);
        assert!(db.get_user_summary(1).unwrap().is_empty());
    }

    #[test]
    fn same_code_stock_and_fund_are_independent() {
        let (_dir, db) = open_temp();
        let s = db.insert_trade(1, "t", "002714", Side::Buy, 40.0, 10000, "CNY", 1.0, None, Some("A")).unwrap();
        let f = db.insert_trade(1, "t", "002714", Side::Buy, 1.2, 2000, "CNY", 1.0, None, Some("FUND")).unwrap();
        assert_eq!(s.market, "A");
        assert_eq!(f.market, "FUND");
        db.insert_close_trades(1, "t", "002714", None, 42.0, "CNY", 1.0, None, Some("A")).unwrap();
        let rem = db.get_position_entries(1, Some("002714"), None).unwrap();
        assert_eq!(rem.len(), 1);
        assert_eq!(rem[0].market, "FUND");
    }

    #[test]
    fn leverage_mismatch_and_fallback() {
        let (_dir, db) = open_temp();
        db.insert_trade(1, "t", "600000", Side::Buy, 10.0, 10000, "CNY", 1.0, Some(5.0), None).unwrap();
        let added = db.insert_trade(1, "t", "600000", Side::Buy, 11.0, 5000, "CNY", 1.0, None, None).unwrap();
        assert_eq!(added.leverage, 5.0);
        let err = db.insert_trade(1, "t", "600000", Side::Sell, 12.0, 5000, "CNY", 1.0, Some(2.0), None);
        assert!(matches!(err, Err(ModelError::LeverageMismatch(_))));
    }

    #[test]
    fn legacy_v0_migration_scales_qty_and_fixes_otc_fund() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("legacy.db");
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    price REAL NOT NULL,
                    qty INTEGER NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'CNY',
                    rate REAL NOT NULL DEFAULT 1.0,
                    trade_ts TEXT NOT NULL
                );
                INSERT INTO trades (user_id, username, symbol, side, price, qty, currency, rate, trade_ts)
                VALUES (1, 't', '600000', 'BUY', 10, 300, 'CNY', 1, '2026-01-01'),
                       (1, 't', '010042', 'BUY', 1.2, 20, 'CNY', 1, '2026-01-01');",
            )
            .unwrap();
        }
        let db = Db::open(&path).unwrap();
        db.init().unwrap();
        let before = db.get_user_summary(1).unwrap();
        assert_eq!(before.get("600000.A"), Some(&30000));
        assert_eq!(before.get("010042.F"), Some(&2000));
        let r1 = db.update_database().unwrap();
        assert!(r1.updated);
        assert_eq!(r1.rows_updated, 2);
        let r2 = db.update_database().unwrap();
        assert!(!r2.updated);
        assert_eq!(db.get_user_summary(1).unwrap(), before);
    }

    #[test]
    fn v2_database_upgrades_to_v3_with_jp_and_triggers() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("v2.db");
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE trades (
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
                );
                INSERT INTO trades (user_id, username, symbol, side, price, qty, market, trade_ts)
                VALUES (1, 't', '600000', 'BUY', 10, 100, 'A', '2026-01-01');
                PRAGMA user_version = 2;",
            )
            .unwrap();
        }
        let db = Db::open(&path).unwrap();
        db.init().unwrap();
        let jp = db
            .insert_trade(1, "t", "7203.JP", Side::Buy, 3312.0, 100, "JPY", 0.042, None, None)
            .unwrap();
        assert_eq!(jp.market, "JP");
        let summary = db.get_user_summary(1).unwrap();
        assert_eq!(summary.get("7203.JP"), Some(&100));
        assert_eq!(summary.get("600000.A"), Some(&100));
    }

    #[test]
    fn delete_trades_enforces_ownership_and_bucket() {
        let (_dir, db) = open_temp();
        let t1 = db.insert_trade(1, "u1", "600000", Side::Buy, 10.0, 100, "CNY", 1.0, Some(5.0), None).unwrap();
        let t2 = db.insert_trade(1, "u1", "600000", Side::Buy, 11.0, 100, "CNY", 1.0, Some(2.0), None).unwrap();
        let t3 = db.insert_trade(2, "u2", "AAPL", Side::Buy, 200.0, 100, "USD", 7.0, None, None).unwrap();

        let deleted = db.delete_trades(&[t1.id, t3.id, 999], 1).unwrap();
        assert_eq!(deleted, vec![t1.id]);
        assert_eq!(db.get_trades(2, None, None).unwrap().len(), 1);

        let removed = db.delete_trades_by_symbol_leverage(1, "600000", 2.0, None).unwrap();
        assert_eq!(removed, 1);
        assert!(db.get_trades(1, None, None).unwrap().is_empty());
        assert_eq!(t2.leverage, 2.0);
    }
}
