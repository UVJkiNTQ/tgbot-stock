use crate::cache::TtlCache;
use crate::config::QUOTE_CACHE_TTL_SECS;
use crate::http_client::fetch_yahoo_chart;
use crate::instrument::{
    normalize_symbol, parse_symbol, resolve_instrument, yahoo_symbol, zfill,
};
use encoding_rs::GB18030;
use reqwest::Client;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::sync::Mutex;
use std::time::Duration;

pub type InstrumentKey = (String, String);

const RATE_CACHE_TTL_SECS: u64 = 600;
const QUOTE_CACHE_MAX: usize = 512;
const RATE_CACHE_MAX: usize = 64;

#[derive(Debug, Clone)]
pub struct Quote {
    pub symbol: String,
    pub name: String,
    pub price: f64,
    pub open: f64,
    pub prev_close: f64,
    pub high: f64,
    pub low: f64,
    pub market: String,
}

#[derive(Debug, Clone)]
pub struct RateUnavailableError(pub String);

impl std::fmt::Display for RateUnavailableError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

pub fn market_currency(symbol: &str, market: Option<&str>) -> &'static str {
    let resolved = resolve_instrument(symbol, market)
        .map(|(_, m)| m)
        .unwrap_or_else(|_| "A".to_string());
    currency_for_market(&resolved)
}

pub fn quote_currency(quote: &Quote) -> &'static str {
    currency_for_market(&quote.market)
}

pub fn currency_for_market(market: &str) -> &'static str {
    match market {
        "HK" => "HKD",
        "US" => "USD",
        "JP" => "JPY",
        _ => "CNY",
    }
}

pub fn japan_alternative_code(symbol: &str) -> Option<String> {
    let (base, forced) = parse_symbol(symbol).ok()?;
    if forced.is_some() {
        return None;
    }
    if base.len() == 5
        && base.starts_with('0')
        && !base.starts_with("00")
        && base.bytes().all(|b| b.is_ascii_digit())
    {
        return Some(base[1..].to_string());
    }
    None
}

pub fn sina_code(symbol: &str, market: Option<&str>) -> Result<String, String> {
    let s = normalize_symbol(symbol)?;
    let (_, m) = resolve_instrument(&s, market)?;
    match m.as_str() {
        "HK" => Ok(format!("hk{}", zfill(&s, 5))),
        "BSE" => Ok(format!("bj{}", zfill(&s, 6))),
        "A" => {
            if matches!(s.as_bytes().first(), Some(b'5' | b'6' | b'9')) {
                Ok(format!("sh{s}"))
            } else {
                Ok(format!("sz{s}"))
            }
        }
        _ => Err(format!("Cannot build sina code for {symbol}")),
    }
}

pub fn parse_sina_fields(symbol: &str, fields: &[&str], market: Option<&str>) -> Option<Quote> {
    let (_, resolved_market) = resolve_instrument(symbol, market).ok()?;
    let (name, open_s, prev_s, price_s, high_s, low_s) = if resolved_market == "HK" {
        if fields.len() < 7 || fields[6].is_empty() {
            return None;
        }
        let name = if !fields[1].is_empty() {
            fields[1]
        } else {
            fields[0]
        };
        (name, fields[2], fields[3], fields[6], fields[4], fields[5])
    } else {
        if fields.len() < 6 || fields[3].is_empty() {
            return None;
        }
        (
            fields[0], fields[1], fields[2], fields[3], fields[4], fields[5],
        )
    };

    let price: f64 = price_s.parse().ok()?;
    Some(Quote {
        symbol: symbol.to_string(),
        name: name.to_string(),
        price,
        open: open_s.parse().unwrap_or(0.0),
        prev_close: prev_s.parse().unwrap_or(0.0),
        high: high_s.parse().unwrap_or(0.0),
        low: low_s.parse().unwrap_or(0.0),
        market: resolved_market,
    })
}

fn last_finite_positive(arr: Option<&Vec<Value>>) -> f64 {
    let Some(values) = arr else {
        return 0.0;
    };
    for item in values.iter().rev() {
        if let Some(num) = item.as_f64() {
            if num.is_finite() && num > 0.0 {
                return num;
            }
        }
    }
    0.0
}

pub fn parse_yahoo_chart_quote(sym: &str, market: &str, result: &Value) -> Option<Quote> {
    let meta = result.get("meta")?.as_object()?;
    let price = meta.get("regularMarketPrice").and_then(Value::as_f64)?;
    if !price.is_finite() || price <= 0.0 {
        return None;
    }

    let prev_close = meta
        .get("chartPreviousClose")
        .or_else(|| meta.get("previousClose"))
        .and_then(Value::as_f64)
        .unwrap_or(0.0);

    let quote_node = result.pointer("/indicators/quote/0");
    let open = meta
        .get("regularMarketOpen")
        .and_then(Value::as_f64)
        .filter(|v| v.is_finite() && *v > 0.0)
        .unwrap_or_else(|| {
            last_finite_positive(quote_node.and_then(|n| n.get("open")).and_then(Value::as_array))
        });
    let high = meta
        .get("regularMarketDayHigh")
        .and_then(Value::as_f64)
        .filter(|v| v.is_finite() && *v > 0.0)
        .unwrap_or_else(|| {
            last_finite_positive(quote_node.and_then(|n| n.get("high")).and_then(Value::as_array))
        });
    let low = meta
        .get("regularMarketDayLow")
        .and_then(Value::as_f64)
        .filter(|v| v.is_finite() && *v > 0.0)
        .unwrap_or_else(|| {
            last_finite_positive(quote_node.and_then(|n| n.get("low")).and_then(Value::as_array))
        });

    let name = if market == "JP" {
        meta.get("longName")
            .or_else(|| meta.get("shortName"))
            .and_then(Value::as_str)
            .unwrap_or(sym)
    } else {
        meta.get("shortName")
            .or_else(|| meta.get("longName"))
            .or_else(|| meta.get("symbol"))
            .and_then(Value::as_str)
            .unwrap_or(sym)
    }
    .trim();

    Some(Quote {
        symbol: sym.to_string(),
        name: if name.is_empty() {
            sym.to_string()
        } else {
            name.to_string()
        },
        price,
        open,
        prev_close,
        high,
        low,
        market: market.to_string(),
    })
}

fn extract_eastmoney_fund(sym: &str, text: &str) -> Option<Quote> {
    let name_pos = text.find("fS_name")?;
    let after_name = &text[name_pos..];
    let q1 = after_name.find('"')? + 1;
    let q2 = after_name[q1..].find('"')?;
    let name = after_name[q1..q1 + q2].trim();
    if name.is_empty() {
        return None;
    }

    let trend_pos = text.find("Data_netWorthTrend")?;
    let after_trend = &text[trend_pos..];
    let b1 = after_trend.find('[')?;
    let b2 = after_trend[b1..].find("];")?;
    let trend_json = &after_trend[b1..b1 + b2 + 1];
    let trend: Vec<Value> = serde_json::from_str(trend_json).ok()?;
    let last = trend.last()?;
    let prev = if trend.len() >= 2 {
        &trend[trend.len() - 2]
    } else {
        last
    };
    let price = last.get("y").and_then(Value::as_f64).unwrap_or(0.0);
    let prev_close = prev.get("y").and_then(Value::as_f64).unwrap_or(0.0);
    if !price.is_finite() || price <= 0.0 {
        return None;
    }
    Some(Quote {
        symbol: sym.to_string(),
        name: name.to_string(),
        price,
        open: prev_close,
        prev_close,
        high: price.max(prev_close),
        low: price.min(prev_close),
        market: "FUND".to_string(),
    })
}

pub struct QuoteService {
    http: Client,
    quote_cache: Mutex<TtlCache<InstrumentKey, Quote>>,
    rate_cache: Mutex<TtlCache<String, f64>>,
}

impl QuoteService {
    pub fn new(http: Client) -> Self {
        Self {
            http,
            quote_cache: Mutex::new(TtlCache::new(
                Duration::from_secs_f64(QUOTE_CACHE_TTL_SECS),
                QUOTE_CACHE_MAX,
            )),
            rate_cache: Mutex::new(TtlCache::new(
                Duration::from_secs(RATE_CACHE_TTL_SECS),
                RATE_CACHE_MAX,
            )),
        }
    }

    pub async fn get_quote(&self, symbol: &str, market: Option<&str>) -> Option<Quote> {
        let key = resolve_instrument(symbol, market).ok()?;
        let mut map = self.get_quotes(std::slice::from_ref(&key)).await;
        map.remove(&key)
    }

    pub async fn get_japan_alternative(&self, symbol: &str) -> Option<Quote> {
        let code = japan_alternative_code(symbol)?;
        self.get_quote(&format!("{code}.JP"), None).await
    }

    pub async fn get_quotes(&self, instruments: &[InstrumentKey]) -> HashMap<InstrumentKey, Quote> {
        let mut results: HashMap<InstrumentKey, Quote> = HashMap::new();
        let mut unique_keys: Vec<InstrumentKey> = Vec::new();
        let mut seen: HashSet<InstrumentKey> = HashSet::new();
        for (sym, mkt) in instruments {
            if let Ok(resolved) = resolve_instrument(sym, Some(mkt)) {
                if seen.insert(resolved.clone()) {
                    unique_keys.push(resolved);
                }
            }
        }

        let mut a_syms = Vec::new();
        let mut hk_syms = Vec::new();
        let mut bse_syms = Vec::new();
        let mut us_syms = Vec::new();
        let mut jp_syms = Vec::new();
        let mut fund_syms = Vec::new();

        {
            let cache = self.quote_cache.lock().unwrap();
            for key in &unique_keys {
                if let Some(q) = cache.get(key) {
                    results.insert(key.clone(), q);
                    continue;
                }
                match key.1.as_str() {
                    "FUND" => fund_syms.push(key.0.clone()),
                    "BSE" => bse_syms.push(key.0.clone()),
                    "US" => us_syms.push(key.0.clone()),
                    "JP" => jp_syms.push(key.0.clone()),
                    "HK" => hk_syms.push(key.0.clone()),
                    _ => a_syms.push(key.0.clone()),
                }
            }
        }

        let mut missed_a_syms = Vec::new();
        for (market, syms) in [("A", &a_syms), ("HK", &hk_syms), ("BSE", &bse_syms)] {
            if syms.is_empty() {
                continue;
            }
            let fetched = self.fetch_sina(syms, market).await;
            {
                let mut cache = self.quote_cache.lock().unwrap();
                for (sym, q) in &fetched {
                    let key = (sym.clone(), market.to_string());
                    cache.insert(key.clone(), q.clone());
                    results.insert(key, q.clone());
                }
            }
            if market == "A" {
                for sym in syms {
                    if !fetched.contains_key(sym) {
                        missed_a_syms.push(sym.clone());
                    }
                }
            }
        }

        if !missed_a_syms.is_empty() || !fund_syms.is_empty() {
            let mut probe = Vec::new();
            let mut seen_f = HashSet::new();
            for s in missed_a_syms.into_iter().chain(fund_syms.into_iter()) {
                if seen_f.insert(s.clone()) {
                    probe.push(s);
                }
            }
            let fetched = self.fetch_funds(&probe).await;
            let mut cache = self.quote_cache.lock().unwrap();
            for (sym, q) in fetched {
                let key = (sym, "FUND".to_string());
                cache.insert(key.clone(), q.clone());
                if seen.contains(&key) {
                    results.insert(key, q);
                }
            }
        }

        if !us_syms.is_empty() {
            let fetched = self.fetch_yahoo(&us_syms, "US").await;
            let mut cache = self.quote_cache.lock().unwrap();
            for (sym, q) in fetched {
                let key = (sym, "US".to_string());
                cache.insert(key.clone(), q.clone());
                results.insert(key, q);
            }
        }

        if !jp_syms.is_empty() {
            let fetched = self.fetch_yahoo(&jp_syms, "JP").await;
            let mut cache = self.quote_cache.lock().unwrap();
            for (sym, q) in fetched {
                let key = (sym, "JP".to_string());
                cache.insert(key.clone(), q.clone());
                results.insert(key, q);
            }
        }

        results
    }

    async fn fetch_sina(&self, symbols: &[String], market: &str) -> HashMap<String, Quote> {
        let mut results = HashMap::new();
        let mut codes = Vec::new();
        let mut code_map = HashMap::new();
        for sym in symbols {
            if let Ok(sc) = sina_code(sym, Some(market)) {
                code_map.insert(sc.clone(), sym.clone());
                codes.push(sc);
            }
        }
        if codes.is_empty() {
            return results;
        }

        let url = format!("https://hq.sinajs.cn/list={}", codes.join(","));
        let resp = match self
            .http
            .get(&url)
            .header("Referer", "https://finance.sina.com.cn")
            .timeout(Duration::from_secs(10))
            .send()
            .await
        {
            Ok(r) if r.status().is_success() => r,
            _ => return results,
        };
        let bytes = match resp.bytes().await {
            Ok(b) => b,
            Err(_) => return results,
        };
        let (text, _, _) = GB18030.decode(&bytes);

        for sc in &codes {
            let prefix = format!("var hq_str_{sc}=\"");
            let Some(start_idx) = text.find(&prefix) else {
                continue;
            };
            let rest = &text[start_idx + prefix.len()..];
            let Some(end_idx) = rest.find('"') else {
                continue;
            };
            let fields: Vec<&str> = rest[..end_idx].split(',').collect();
            if let Some(sym) = code_map.get(sc) {
                if let Some(q) = parse_sina_fields(sym, &fields, Some(market)) {
                    results.insert(sym.clone(), q);
                }
            }
        }
        results
    }

    async fn fetch_funds(&self, symbols: &[String]) -> HashMap<String, Quote> {
        let mut results = HashMap::new();
        for sym in symbols {
            let url = format!("https://fund.eastmoney.com/pingzhongdata/{sym}.js");
            let resp = match self
                .http
                .get(&url)
                .header("Referer", "https://fund.eastmoney.com")
                .timeout(Duration::from_secs(10))
                .send()
                .await
            {
                Ok(r) if r.status().is_success() => r,
                _ => continue,
            };
            let bytes = match resp.bytes().await {
                Ok(b) => b,
                Err(_) => continue,
            };
            let text = String::from_utf8_lossy(&bytes);
            if let Some(q) = extract_eastmoney_fund(sym, &text) {
                results.insert(sym.clone(), q);
            }
        }
        results
    }

    async fn fetch_yahoo(&self, symbols: &[String], market: &str) -> HashMap<String, Quote> {
        let mut results = HashMap::new();
        let mut handles = Vec::with_capacity(symbols.len());
        for sym in symbols {
            let http = self.http.clone();
            let sym_owned = sym.clone();
            let mkt_owned = market.to_string();
            handles.push(tokio::spawn(async move {
                let ticker = yahoo_symbol(&sym_owned, &mkt_owned).ok()?;
                let node = fetch_yahoo_chart(&http, &ticker, "1d", "1d", false).await?;
                let q = parse_yahoo_chart_quote(&sym_owned, &mkt_owned, &node)?;
                Some((sym_owned, q))
            }));
        }
        for handle in handles {
            if let Ok(Some((sym, q))) = handle.await {
                results.insert(sym, q);
            }
        }
        results
    }

    pub async fn get_rate(&self, currency: &str) -> Result<f64, RateUnavailableError> {
        let cur = currency.trim().to_uppercase();
        if cur == "CNY" {
            return Ok(1.0);
        }

        let stale = {
            let cache = self.rate_cache.lock().unwrap();
            if let Some(rate) = cache.get(&cur) {
                return Ok(rate);
            }
            cache.get_stale(&cur)
        };

        let url = format!("https://open.er-api.com/v6/latest/{cur}");
        let fetched = async {
            let resp = self
                .http
                .get(&url)
                .timeout(Duration::from_secs(10))
                .send()
                .await
                .map_err(|e| e.to_string())?;
            if !resp.status().is_success() {
                return Err(format!("HTTP {}", resp.status()));
            }
            let data: Value = resp.json().await.map_err(|e| e.to_string())?;
            if data.get("result").and_then(Value::as_str) != Some("success")
                || data.get("base_code").and_then(Value::as_str) != Some(cur.as_str())
            {
                return Err("invalid exchange-rate response".to_string());
            }
            let rate = data
                .pointer("/rates/CNY")
                .and_then(Value::as_f64)
                .ok_or_else(|| "missing CNY rate".to_string())?;
            if !rate.is_finite() || rate <= 0.0 {
                return Err("invalid exchange rate".to_string());
            }
            Ok::<f64, String>(rate)
        }
        .await;

        match fetched {
            Ok(rate) => {
                self.rate_cache.lock().unwrap().insert(cur, rate);
                Ok(rate)
            }
            Err(_) => {
                if let Some(s) = stale {
                    Ok(s)
                } else {
                    Err(RateUnavailableError(format!(
                        "unable to fetch {cur}/CNY rate"
                    )))
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sina_hk_and_a_share_parsing() {
        let hk = parse_sina_fields(
            "00700",
            &[
                "TENCENT", "腾讯控股", "470.000", "471.800", "479.800", "462.000", "475.200",
            ],
            None,
        )
        .unwrap();
        assert_eq!(hk.market, "HK");
        assert_eq!(hk.name, "腾讯控股");
        assert_eq!(hk.price, 475.2);
        assert_eq!(quote_currency(&hk), "HKD");

        let a = parse_sina_fields(
            "600000",
            &["浦发银行", "9.590", "9.710", "9.390", "9.590", "9.280"],
            None,
        )
        .unwrap();
        assert_eq!(a.market, "A");
        assert_eq!(a.price, 9.39);
        assert_eq!(sina_code("920118", None).unwrap(), "bj920118");
    }

    #[test]
    fn yahoo_chart_quote_parsing() {
        let payload = serde_json::json!({
            "meta": {
                "symbol": "7203.T",
                "longName": "Toyota Motor Corporation",
                "regularMarketPrice": 3312.0,
                "chartPreviousClose": 3378.0,
                "regularMarketDayHigh": 3384.0,
                "regularMarketDayLow": 3298.0
            },
            "indicators": {
                "quote": [{"open": [3348.0]}]
            }
        });
        let q = parse_yahoo_chart_quote("7203", "JP", &payload).unwrap();
        assert_eq!(q.name, "Toyota Motor Corporation");
        assert_eq!(q.open, 3348.0);
        assert_eq!(q.price, 3312.0);
        assert_eq!(q.prev_close, 3378.0);
    }

    #[test]
    fn japan_alternative_rules() {
        assert_eq!(japan_alternative_code("7203").as_deref(), Some("7203"));
        assert_eq!(japan_alternative_code("00700"), None);
        assert_eq!(japan_alternative_code("7203.HK"), None);
        assert_eq!(japan_alternative_code("7203.JP"), None);
    }
}
