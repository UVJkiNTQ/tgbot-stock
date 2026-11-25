use crate::cache::BoundedMap;
use crate::instrument::normalize_symbol;
use crate::models::{Db, Side, Trade, QTY_SCALE};
use crate::quotes::{market_currency, InstrumentKey, Quote, QuoteService, RateUnavailableError};
use std::collections::{HashMap, HashSet};
use std::sync::Mutex;

const REALIZED_NAME_CACHE_MAX: usize = 1024;

#[derive(Debug, Clone)]
pub struct Position {
    pub symbol: String,
    pub name: String,
    pub qty: i64,
    pub avg_cost: f64,
    pub current_price: Option<f64>,
    pub currency: String,
    pub rate: Option<f64>,
    pub leverage: f64,
    pub unrealized_pnl: f64,
    pub unrealized_pnl_pct: f64,
    pub avg_cost_cny: f64,
    pub current_price_cny: Option<f64>,
    pub unrealized_pnl_cny: f64,
    pub quote_available: bool,
    pub margin_cny: f64,
}

impl Position {
    pub fn is_foreign(&self) -> bool {
        self.currency != "CNY"
    }
}

#[derive(Debug, Clone)]
pub struct RealizedPnl {
    pub symbol: String,
    pub market: String,
    pub leverage: f64,
    pub currency: String,
    pub realized_pnl: f64,
    pub realized_pnl_cny: f64,
    pub closed_margin_cny: f64,
    pub name: String,
}

impl RealizedPnl {
    pub fn is_foreign(&self) -> bool {
        self.currency != "CNY"
    }
}

#[derive(Debug, Clone)]
pub struct UserPnl {
    pub username: String,
    pub positions: Vec<Position>,
    pub total_unrealized_pnl_cny: f64,
    pub total_market_value_cny: f64,
    pub total_cost_cny: f64,
    pub realized: Vec<RealizedPnl>,
    pub total_realized_pnl_cny: f64,
    pub total_margin_cny: f64,
    pub total_closed_margin_cny: f64,
}

impl UserPnl {
    pub fn total_pnl_cny(&self) -> f64 {
        self.total_realized_pnl_cny + self.total_unrealized_pnl_cny
    }

    pub fn total_pnl_margin_cny(&self) -> f64 {
        self.total_closed_margin_cny + self.total_margin_cny
    }
}

#[derive(Debug, Clone)]
pub struct UserTodayChange {
    pub username: String,
    pub change_cny: f64,
    pub prev_market_value_cny: f64,
}

impl UserTodayChange {
    pub fn change_pct(&self) -> f64 {
        if self.prev_market_value_cny > 0.0 {
            self.change_cny / self.prev_market_value_cny * 100.0
        } else {
            0.0
        }
    }
}

struct OpenHolding {
    symbol: String,
    market: String,
    qty: i64,
    currency: String,
}

fn compute_user_today_change(
    username: String,
    holdings: &[OpenHolding],
    qmap: &HashMap<InstrumentKey, Quote>,
    rate_map: &HashMap<String, f64>,
) -> Option<UserTodayChange> {
    let mut change_cny = 0.0;
    let mut prev_market_value_cny = 0.0;

    for h in holdings {
        let norm = normalize_symbol(&h.symbol).unwrap_or_else(|_| h.symbol.clone());
        let Some(q) = qmap.get(&(norm, h.market.clone())) else {
            continue;
        };
        if !q.price.is_finite()
            || q.price <= 0.0
            || !q.prev_close.is_finite()
            || q.prev_close <= 0.0
        {
            continue;
        }
        let cur_rate = *rate_map.get(&h.currency)?;
        let shares = h.qty as f64 / QTY_SCALE as f64;
        change_cny += shares * (q.price - q.prev_close) * cur_rate;
        prev_market_value_cny += shares.abs() * q.prev_close * cur_rate;
    }

    if prev_market_value_cny > 0.0 {
        Some(UserTodayChange {
            username,
            change_cny,
            prev_market_value_cny,
        })
    } else {
        None
    }
}

#[derive(Debug, Clone)]
pub struct TradeHistory {
    pub qty: i64,
    pub avg_cost: f64,
    pub avg_cost_cny: f64,
    pub leverage: f64,
    pub currency: String,
    pub realized_pnl: f64,
    pub realized_pnl_cny: f64,
    pub closed_cost_cny: f64,
    pub closed_margin_cny: f64,
}

impl Default for TradeHistory {
    fn default() -> Self {
        Self {
            qty: 0,
            avg_cost: 0.0,
            avg_cost_cny: 0.0,
            leverage: 1.0,
            currency: "CNY".to_string(),
            realized_pnl: 0.0,
            realized_pnl_cny: 0.0,
            closed_cost_cny: 0.0,
            closed_margin_cny: 0.0,
        }
    }
}

pub fn calculate_trade_history(trades: &[Trade]) -> TradeHistory {
    let mut state = TradeHistory::default();

    for trade in trades {
        let trade_qty = if trade.side == Side::Buy {
            trade.qty
        } else {
            -trade.qty
        };
        let trade_cost_cny = trade.price * trade.rate;
        if !trade.currency.is_empty() {
            state.currency = trade.currency.clone();
        }
        state.leverage = trade.leverage;

        if state.qty == 0 {
            state.qty = trade_qty;
            state.avg_cost = trade.price;
            state.avg_cost_cny = trade_cost_cny;
            continue;
        }

        let same_direction =
            (state.qty > 0 && trade_qty > 0) || (state.qty < 0 && trade_qty < 0);
        if same_direction {
            let old_size = state.qty.abs() as f64;
            let added_size = trade_qty.abs() as f64;
            let new_size = old_size + added_size;
            state.avg_cost = (state.avg_cost * old_size + trade.price * added_size) / new_size;
            state.avg_cost_cny =
                (state.avg_cost_cny * old_size + trade_cost_cny * added_size) / new_size;
            state.qty += trade_qty;
            continue;
        }

        let old_size = state.qty.abs();
        let closing_size = old_size.min(trade_qty.abs());
        let direction = if state.qty > 0 { 1.0 } else { -1.0 };
        let closed_quantity = closing_size as f64 / QTY_SCALE as f64;

        state.realized_pnl += (trade.price - state.avg_cost) * closed_quantity * direction;
        state.realized_pnl_cny +=
            (trade_cost_cny - state.avg_cost_cny) * closed_quantity * direction;
        state.closed_cost_cny += state.avg_cost_cny * closed_quantity;
        state.closed_margin_cny += state.avg_cost_cny * closed_quantity / state.leverage;

        let new_qty = state.qty + trade_qty;
        if trade_qty.abs() < old_size {
            state.qty = new_qty;
        } else if trade_qty.abs() == old_size {
            state.qty = 0;
            state.avg_cost = 0.0;
            state.avg_cost_cny = 0.0;
        } else {
            state.qty = new_qty;
            state.avg_cost = trade.price;
            state.avg_cost_cny = trade_cost_cny;
        }
    }

    if state.qty == 0 {
        state.avg_cost = 0.0;
        state.avg_cost_cny = 0.0;
    }
    state
}

pub struct PnlService {
    realized_name_cache: Mutex<BoundedMap<InstrumentKey, String>>,
}

impl Default for PnlService {
    fn default() -> Self {
        Self::new()
    }
}

impl PnlService {
    pub fn new() -> Self {
        Self {
            realized_name_cache: Mutex::new(BoundedMap::new(REALIZED_NAME_CACHE_MAX)),
        }
    }

    pub async fn populate_realized_names(&self, result: &mut UserPnl, quotes: &QuoteService) {
        let mut missing: Vec<InstrumentKey> = Vec::new();
        let mut missing_set: HashSet<InstrumentKey> = HashSet::new();
        {
            let cache = self.realized_name_cache.lock().unwrap();
            for item in &mut result.realized {
                let norm = normalize_symbol(&item.symbol).unwrap_or_else(|_| item.symbol.clone());
                let key = (norm, item.market.clone());
                if let Some(name) = cache.get(&key) {
                    item.name = name;
                } else if missing_set.insert(key.clone()) {
                    missing.push((item.symbol.clone(), item.market.clone()));
                }
            }
        }
        if missing.is_empty() {
            return;
        }
        let qmap = quotes.get_quotes(&missing).await;
        let mut cache = self.realized_name_cache.lock().unwrap();
        for item in &mut result.realized {
            let norm = normalize_symbol(&item.symbol).unwrap_or_else(|_| item.symbol.clone());
            let key = (norm, item.market.clone());
            if let Some(q) = qmap.get(&key) {
                if !q.name.is_empty() {
                    item.name = q.name.clone();
                    cache.insert(key, q.name.clone());
                }
            }
        }
    }

    pub async fn compute_user_pnl(
        &self,
        db: &Db,
        quotes: &QuoteService,
        user_id: i64,
        include_unrealized: bool,
    ) -> Result<UserPnl, RateUnavailableError> {
        let all_trades = db.get_trades(user_id, None, None).unwrap_or_default();
        let username = all_trades
            .first()
            .map(|t| t.username.clone())
            .unwrap_or_else(|| "unknown".to_string());

        type BucketKey = (String, String, u64);
        let mut buckets: HashMap<BucketKey, (f64, Vec<Trade>)> = HashMap::new();
        let mut bucket_order: Vec<BucketKey> = Vec::new();
        for trade in all_trades {
            let key = (
                trade.symbol.clone(),
                trade.market.clone(),
                trade.leverage.to_bits(),
            );
            let entry = buckets.entry(key.clone()).or_insert_with(|| {
                bucket_order.push(key);
                (trade.leverage, Vec::new())
            });
            entry.1.push(trade);
        }

        let mut histories: Vec<((String, String, f64), TradeHistory)> =
            Vec::with_capacity(bucket_order.len());
        let mut open_instruments: Vec<InstrumentKey> = Vec::new();
        let mut seen_open: HashSet<InstrumentKey> = HashSet::new();

        for key in bucket_order {
            let (lev, trades) = &buckets[&key];
            let hist = calculate_trade_history(trades);
            if include_unrealized && hist.qty != 0 {
                let ik = (key.0.clone(), key.1.clone());
                if seen_open.insert(ik.clone()) {
                    open_instruments.push(ik);
                }
            }
            histories.push(((key.0, key.1, *lev), hist));
        }

        let qmap = if !open_instruments.is_empty() {
            quotes.get_quotes(&open_instruments).await
        } else {
            HashMap::new()
        };

        let mut positions = Vec::new();
        let mut realized = Vec::new();
        let mut total_cost_cny = 0.0;
        let mut total_market_value_cny = 0.0;
        let mut rate_map: HashMap<String, f64> = HashMap::new();

        for ((sym, market, leverage), history) in histories {
            if history.closed_cost_cny != 0.0 {
                realized.push(RealizedPnl {
                    symbol: sym.clone(),
                    market: market.clone(),
                    leverage,
                    currency: history.currency.clone(),
                    realized_pnl: history.realized_pnl,
                    realized_pnl_cny: history.realized_pnl_cny,
                    closed_margin_cny: history.closed_margin_cny,
                    name: String::new(),
                });
            }

            if history.qty == 0 || !include_unrealized {
                continue;
            }

            let net_qty = history.qty;
            let avg_cost = history.avg_cost;
            let avg_cost_cny = history.avg_cost_cny;
            let currency = if !history.currency.is_empty() {
                history.currency.clone()
            } else {
                market_currency(&sym, Some(&market)).to_string()
            };

            let norm = normalize_symbol(&sym).unwrap_or_else(|_| sym.clone());
            let q = qmap.get(&(norm.clone(), market.clone()));
            let valid_q = q.filter(|q| q.price.is_finite() && q.price > 0.0);

            let Some(q) = valid_q else {
                let cost_cny = net_qty.abs() as f64 * avg_cost_cny / QTY_SCALE as f64;
                positions.push(Position {
                    symbol: sym.clone(),
                    name: sym,
                    qty: net_qty,
                    avg_cost,
                    current_price: None,
                    currency,
                    rate: None,
                    leverage: history.leverage,
                    unrealized_pnl: 0.0,
                    unrealized_pnl_pct: 0.0,
                    avg_cost_cny: 0.0,
                    current_price_cny: None,
                    unrealized_pnl_cny: 0.0,
                    quote_available: false,
                    margin_cny: cost_cny / history.leverage,
                });
                continue;
            };

            let cur_rate = if let Some(&r) = rate_map.get(&currency) {
                r
            } else {
                let r = quotes.get_rate(&currency).await?;
                rate_map.insert(currency.clone(), r);
                r
            };

            let name = q.name.clone();
            let cur_price = q.price;
            self.realized_name_cache
                .lock()
                .unwrap()
                .insert((norm, market.clone()), name.clone());

            let cur_price_cny = cur_price * cur_rate;
            let mv = net_qty as f64 * cur_price / QTY_SCALE as f64;
            let mv_cny = mv * cur_rate;
            let entry_value = net_qty.abs() as f64 * avg_cost / QTY_SCALE as f64;
            let cost_cny = net_qty.abs() as f64 * avg_cost_cny / QTY_SCALE as f64;
            let pnl = mv - net_qty as f64 * avg_cost / QTY_SCALE as f64;
            let pnl_cny = mv_cny - net_qty as f64 * avg_cost_cny / QTY_SCALE as f64;
            let margin_cny = cost_cny / history.leverage;
            let margin_value = entry_value / history.leverage;
            let pnl_pct = if margin_value != 0.0 {
                pnl / margin_value * 100.0
            } else {
                0.0
            };

            positions.push(Position {
                symbol: sym,
                name,
                qty: net_qty,
                avg_cost,
                current_price: Some(cur_price),
                currency,
                rate: Some(cur_rate),
                leverage: history.leverage,
                unrealized_pnl: pnl,
                unrealized_pnl_pct: pnl_pct,
                avg_cost_cny,
                current_price_cny: Some(cur_price_cny),
                unrealized_pnl_cny: pnl_cny,
                quote_available: true,
                margin_cny,
            });
            total_cost_cny += cost_cny;
            total_market_value_cny += mv_cny;
        }

        positions.sort_by(|a, b| b.unrealized_pnl_cny.total_cmp(&a.unrealized_pnl_cny));
        realized.sort_by(|a, b| b.realized_pnl_cny.total_cmp(&a.realized_pnl_cny));

        let total_unrealized_pnl_cny = positions.iter().map(|p| p.unrealized_pnl_cny).sum();
        let total_realized_pnl_cny = realized.iter().map(|r| r.realized_pnl_cny).sum();
        let total_margin_cny = positions
            .iter()
            .filter(|p| p.quote_available)
            .map(|p| p.margin_cny)
            .sum();
        let total_closed_margin_cny = realized.iter().map(|r| r.closed_margin_cny).sum();

        Ok(UserPnl {
            username,
            positions,
            total_unrealized_pnl_cny,
            total_market_value_cny,
            total_cost_cny,
            realized,
            total_realized_pnl_cny,
            total_margin_cny,
            total_closed_margin_cny,
        })
    }

    pub async fn compute_leaderboard(
        &self,
        db: &Db,
        quotes: &QuoteService,
        include_unrealized: bool,
    ) -> Result<Vec<UserPnl>, RateUnavailableError> {
        let users = db.get_distinct_users().unwrap_or_default();
        let mut results = Vec::new();
        for (uid, _) in users {
            let p = self
                .compute_user_pnl(db, quotes, uid, include_unrealized)
                .await?;
            let basis = if include_unrealized {
                p.total_pnl_margin_cny()
            } else {
                p.total_closed_margin_cny
            };
            if basis > 0.0 {
                results.push(p);
            }
        }
        results.sort_by(|a, b| {
            let ra = if include_unrealized {
                a.total_pnl_cny() / a.total_pnl_margin_cny()
            } else {
                a.total_realized_pnl_cny / a.total_closed_margin_cny
            };
            let rb = if include_unrealized {
                b.total_pnl_cny() / b.total_pnl_margin_cny()
            } else {
                b.total_realized_pnl_cny / b.total_closed_margin_cny
            };
            rb.total_cmp(&ra)
        });
        Ok(results)
    }

    pub async fn compute_today_leaderboard(
        &self,
        db: &Db,
        quotes: &QuoteService,
    ) -> Result<Vec<UserTodayChange>, RateUnavailableError> {
        let users = db.get_distinct_users().unwrap_or_default();
        let mut user_holdings: Vec<(String, Vec<OpenHolding>)> = Vec::with_capacity(users.len());
        let mut all_instruments: Vec<InstrumentKey> = Vec::new();
        let mut seen_instruments: HashSet<InstrumentKey> = HashSet::new();

        for (uid, fallback_name) in users {
            let all_trades = db.get_trades(uid, None, None).unwrap_or_default();
            if all_trades.is_empty() {
                continue;
            }
            let username = all_trades
                .first()
                .map(|t| t.username.clone())
                .unwrap_or(fallback_name);

            type BucketKey = (String, String, u64);
            let mut buckets: HashMap<BucketKey, Vec<Trade>> = HashMap::new();
            let mut order: Vec<BucketKey> = Vec::new();
            for trade in all_trades {
                let key = (
                    trade.symbol.clone(),
                    trade.market.clone(),
                    trade.leverage.to_bits(),
                );
                let list = buckets.entry(key.clone()).or_insert_with(|| {
                    order.push(key);
                    Vec::new()
                });
                list.push(trade);
            }

            let mut holdings = Vec::new();
            for key in order {
                let hist = calculate_trade_history(&buckets[&key]);
                if hist.qty == 0 {
                    continue;
                }
                let currency = if !hist.currency.is_empty() {
                    hist.currency
                } else {
                    market_currency(&key.0, Some(&key.1)).to_string()
                };
                let ik = (key.0.clone(), key.1.clone());
                if seen_instruments.insert(ik.clone()) {
                    all_instruments.push(ik);
                }
                holdings.push(OpenHolding {
                    symbol: key.0,
                    market: key.1,
                    qty: hist.qty,
                    currency,
                });
            }

            if !holdings.is_empty() {
                user_holdings.push((username, holdings));
            }
        }

        if all_instruments.is_empty() {
            return Ok(Vec::new());
        }

        let qmap = quotes.get_quotes(&all_instruments).await;
        let mut rate_map: HashMap<String, f64> = HashMap::new();
        for (_, holdings) in &user_holdings {
            for h in holdings {
                let norm = normalize_symbol(&h.symbol).unwrap_or_else(|_| h.symbol.clone());
                let valid = qmap.get(&(norm, h.market.clone())).is_some_and(|q| {
                    q.price.is_finite()
                        && q.price > 0.0
                        && q.prev_close.is_finite()
                        && q.prev_close > 0.0
                });
                if !valid {
                    continue;
                }
                if !rate_map.contains_key(&h.currency) {
                    let rate = quotes.get_rate(&h.currency).await?;
                    rate_map.insert(h.currency.clone(), rate);
                }
            }
        }

        let mut results: Vec<UserTodayChange> = user_holdings
            .into_iter()
            .filter_map(|(username, holdings)| {
                compute_user_today_change(username, &holdings, &qmap, &rate_map)
            })
            .collect();

        results.sort_by(|a, b| {
            b.change_pct()
                .total_cmp(&a.change_pct())
                .then_with(|| b.change_cny.total_cmp(&a.change_cny))
        });
        Ok(results)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::quantity_to_units;

    fn mk_trade(side: Side, price: f64, qty: &str, rate: f64, leverage: f64) -> Trade {
        Trade {
            id: 1,
            user_id: 1,
            username: "tester".into(),
            symbol: "00700".into(),
            side,
            price,
            qty: quantity_to_units(qty).unwrap(),
            currency: "HKD".into(),
            rate,
            trade_ts: "2026-01-01T00:00:00+00:00".into(),
            leverage,
            market: "HK".into(),
        }
    }

    #[test]
    fn reversal_and_realized_cny_fx() {
        let h = calculate_trade_history(&[
            mk_trade(Side::Buy, 10.0, "100", 1.0, 2.0),
            mk_trade(Side::Sell, 12.0, "150", 1.0, 2.0),
        ]);
        assert_eq!(h.qty, -5000);
        assert!((h.avg_cost - 12.0).abs() < 1e-9);
        assert!((h.realized_pnl - 200.0).abs() < 1e-9);
        assert!((h.closed_margin_cny - 500.0).abs() < 1e-9);

        let fx = calculate_trade_history(&[
            mk_trade(Side::Buy, 10.0, "100", 0.8, 1.0),
            mk_trade(Side::Sell, 12.0, "100", 0.9, 1.0),
        ]);
        assert_eq!(fx.qty, 0);
        assert!((fx.realized_pnl - 200.0).abs() < 1e-9);
        assert!((fx.realized_pnl_cny - 280.0).abs() < 1e-9);
    }

    fn mk_quote(symbol: &str, market: &str, price: f64, prev_close: f64) -> Quote {
        Quote {
            symbol: symbol.into(),
            name: symbol.into(),
            price,
            open: prev_close,
            prev_close,
            high: price.max(prev_close),
            low: price.min(prev_close),
            market: market.into(),
        }
    }

    #[test]
    fn today_change_handles_long_short_fx_and_missing_quotes() {
        let mut qmap = HashMap::new();
        qmap.insert(
            ("600000".into(), "A".into()),
            mk_quote("600000", "A", 10.5, 10.0), // +5%
        );
        qmap.insert(
            ("AAPL".into(), "US".into()),
            mk_quote("AAPL", "US", 95.0, 100.0), // -5%
        );
        qmap.insert(
            ("000001".into(), "A".into()),
            mk_quote("000001", "A", 10.0, 0.0), // invalid prev_close -> skipped
        );
        let rate_map = HashMap::from([("CNY".into(), 1.0), ("USD".into(), 7.0)]);

        // Long 100 shares of 600000 (prev MV=1000, chg=+50)
        // Short 10 shares of AAPL (prev MV=1000*7=7000, chg=-10*(-5)*7=+350)
        // Invalid 000001 skipped
        let holdings = vec![
            OpenHolding {
                symbol: "600000".into(),
                market: "A".into(),
                qty: 10000,
                currency: "CNY".into(),
            },
            OpenHolding {
                symbol: "AAPL".into(),
                market: "US".into(),
                qty: -1000,
                currency: "USD".into(),
            },
            OpenHolding {
                symbol: "000001".into(),
                market: "A".into(),
                qty: 10000,
                currency: "CNY".into(),
            },
        ];

        let res = compute_user_today_change("alice".into(), &holdings, &qmap, &rate_map).unwrap();
        assert!((res.prev_market_value_cny - 8000.0).abs() < 1e-9);
        assert!((res.change_cny - 400.0).abs() < 1e-9);
        assert!((res.change_pct() - 5.0).abs() < 1e-9);
    }

    #[test]
    fn today_change_returns_none_without_valid_quotes() {
        let qmap = HashMap::new();
        let rate_map = HashMap::from([("CNY".into(), 1.0)]);
        let holdings = vec![OpenHolding {
            symbol: "600000".into(),
            market: "A".into(),
            qty: 10000,
            currency: "CNY".into(),
        }];
        assert!(compute_user_today_change("bob".into(), &holdings, &qmap, &rate_map).is_none());
    }
}
