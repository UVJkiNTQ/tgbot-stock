use crate::cache::TtlCache;
use crate::instrument::qualified_symbol;
use reqwest::Client;
use serde_json::Value;
use std::collections::HashSet;
use std::sync::Mutex;
use std::time::Duration;

pub const MAX_QUERY_LENGTH: usize = 40;
const SEARCH_CACHE_TTL_SECS: u64 = 300;
const SEARCH_CACHE_MAX: usize = 256;
const YAHOO_SEARCH_URL: &str = "https://query1.finance.yahoo.com/v1/finance/search";
const TENCENT_SEARCH_URL: &str = "https://smartbox.gtimg.cn/s3/";

#[derive(Debug, Clone)]
pub struct SearchUnavailableError;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SearchResult {
    pub symbol: String,
    pub market: String,
    pub name: String,
    pub exchange: String,
    pub rank: u8,
    pub quote_type: String,
}

impl SearchResult {
    pub fn display_symbol(&self) -> String {
        qualified_symbol(&self.symbol, &self.market).unwrap_or_else(|_| self.symbol.clone())
    }
}

fn has_non_ascii(text: &str) -> bool {
    text.chars().any(|c| (c as u32) > 127)
}

fn is_us_exchange(ex: &str) -> bool {
    matches!(
        ex,
        "NYQ" | "NMS" | "NGM" | "NCM" | "PCX" | "ASE" | "PNK" | "OTC" | "OQX" | "BTS" | "NAS" | "NYS" | "NYE"
    )
}

fn is_local_exchange(ex: &str) -> bool {
    matches!(ex, "SHH" | "SHZ" | "HKG" | "JPX")
}

fn is_otc_exchange(ex: &str) -> bool {
    matches!(ex, "PNK" | "OTC" | "OQX")
}

pub fn parse_yahoo_result(quote: &Value) -> Option<SearchResult> {
    let quote_type = quote
        .get("quoteType")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_uppercase();
    if !matches!(quote_type.as_str(), "EQUITY" | "ETF") {
        return None;
    }

    let raw_symbol = quote
        .get("symbol")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_uppercase();
    let exchange = quote
        .get("exchange")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_uppercase();
    let exchange_name = quote
        .get("exchDisp")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    let name = quote
        .get("longname")
        .or_else(|| quote.get("shortname"))
        .and_then(Value::as_str)
        .unwrap_or(&raw_symbol)
        .trim()
        .to_string();

    if raw_symbol.is_empty() || name.is_empty() {
        return None;
    }

    let (symbol, market) = if let Some(s) = raw_symbol.strip_suffix(".T") {
        (s.to_string(), "JP")
    } else if let Some(s) = raw_symbol.strip_suffix(".HK") {
        (crate::instrument::zfill(s, 5), "HK")
    } else if let Some(s) = raw_symbol.strip_suffix(".SS") {
        (s.to_string(), "A")
    } else if let Some(s) = raw_symbol.strip_suffix(".SZ") {
        (s.to_string(), "A")
    } else if let Some(s) = raw_symbol.strip_suffix(".BJ") {
        (s.to_string(), "BSE")
    } else if is_us_exchange(&exchange) {
        (raw_symbol.clone(), "US")
    } else {
        return None;
    };

    if symbol.is_empty() {
        return None;
    }
    if matches!(market, "A" | "BSE") && !(symbol.len() == 6 && symbol.bytes().all(|b| b.is_ascii_digit())) {
        return None;
    }
    if market == "HK" && !symbol.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    if market == "JP" && !symbol.replace('A', "").bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }

    let rank = if is_local_exchange(&exchange) {
        0
    } else if is_otc_exchange(&exchange) {
        2
    } else if is_us_exchange(&exchange) {
        1
    } else {
        3
    };

    Some(SearchResult {
        symbol,
        market: market.to_string(),
        name,
        exchange: exchange_name,
        rank,
        quote_type,
    })
}

pub fn parse_tencent_hint(text: &str) -> Vec<SearchResult> {
    let Some(start) = text.find("v_hint=\"") else {
        return Vec::new();
    };
    let rest = &text[start + 8..];
    let Some(end) = rest.rfind('"') else {
        return Vec::new();
    };
    let raw_body = &rest[..end];
    if raw_body.is_empty() || raw_body == "N" {
        return Vec::new();
    }

    let body: String = serde_json::from_str(&format!("\"{raw_body}\""))
        .unwrap_or_else(|_| unescape_unicode_fallback(raw_body));

    let mut results = Vec::new();
    for entry in body.split('^') {
        let fields: Vec<&str> = entry.split('~').collect();
        if fields.len() < 5 {
            continue;
        }
        let (raw_market, raw_code, name, _pinyin, kind) =
            (fields[0], fields[1], fields[2], fields[3], fields[4]);
        if !kind.to_uppercase().starts_with("GP") {
            continue;
        }
        let mut code = raw_code.trim().to_string();
        if code.is_empty() {
            continue;
        }

        let (market, exchange, rank) = match raw_market {
            "sh" | "sz" => ("A", "沪深", 0),
            "bj" => ("BSE", "北交所", 0),
            "hk" => {
                code = crate::instrument::zfill(&code, 5);
                if code.starts_with('8') {
                    continue;
                }
                ("HK", "港股", 0)
            }
            "us" => {
                code = code.split('.').next().unwrap_or("").to_uppercase();
                ("US", "美股", 1)
            }
            _ => continue,
        };

        if matches!(market, "A" | "BSE") && !(code.len() == 6 && code.bytes().all(|b| b.is_ascii_digit())) {
            continue;
        }
        if market == "HK" && !code.bytes().all(|b| b.is_ascii_digit()) {
            continue;
        }
        let clean_name = name.trim();
        results.push(SearchResult {
            symbol: code.clone(),
            market: market.to_string(),
            name: if clean_name.is_empty() {
                code
            } else {
                clean_name.to_string()
            },
            exchange: exchange.to_string(),
            rank,
            quote_type: "EQUITY".to_string(),
        });
    }
    results
}

fn unescape_unicode_fallback(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\\' && chars.peek() == Some(&'u') {
            chars.next();
            let hex: String = (0..4).filter_map(|_| chars.next()).collect();
            if let Ok(code) = u32::from_str_radix(&hex, 16) {
                if let Some(ch) = char::from_u32(code) {
                    out.push(ch);
                    continue;
                }
            }
            out.push_str("\\u");
            out.push_str(&hex);
        } else {
            out.push(c);
        }
    }
    out
}

pub fn finalize_results(results: Vec<SearchResult>, limit: usize) -> Vec<SearchResult> {
    let mut indexed: Vec<(usize, SearchResult)> = results.into_iter().enumerate().collect();
    indexed.sort_by_key(|(idx, r)| (r.rank, if r.quote_type == "EQUITY" { 0u8 } else { 1u8 }, *idx));
    if indexed.iter().any(|(_, r)| r.quote_type == "EQUITY") {
        indexed.retain(|(_, r)| r.quote_type == "EQUITY");
    }

    let mut selected = Vec::new();
    let mut seen_symbols: HashSet<(String, String)> = HashSet::new();
    let mut seen_names: HashSet<(String, String)> = HashSet::new();
    for (_, result) in indexed {
        let sym_key = (result.symbol.clone(), result.market.clone());
        let name_key = (result.name.to_lowercase(), result.market.clone());
        if seen_symbols.contains(&sym_key) || seen_names.contains(&name_key) {
            continue;
        }
        seen_symbols.insert(sym_key);
        seen_names.insert(name_key);
        selected.push(result);
        if selected.len() >= limit {
            break;
        }
    }
    selected
}

fn should_expand(query: &str, results: &[SearchResult]) -> bool {
    !has_non_ascii(query)
        && !query.contains(' ')
        && !query.bytes().all(|b| b.is_ascii_digit())
        && !results.iter().any(|r| r.quote_type == "EQUITY")
}

pub struct SearchService {
    http: Client,
    cache: Mutex<TtlCache<(String, usize), Vec<SearchResult>>>,
}

impl SearchService {
    pub fn new(http: Client) -> Self {
        Self {
            http,
            cache: Mutex::new(TtlCache::new(
                Duration::from_secs(SEARCH_CACHE_TTL_SECS),
                SEARCH_CACHE_MAX,
            )),
        }
    }

    pub async fn search(
        &self,
        query: &str,
        limit: usize,
    ) -> Result<Vec<SearchResult>, SearchUnavailableError> {
        let query = query.trim();
        if query.is_empty() || query.chars().count() > MAX_QUERY_LENGTH {
            return Ok(Vec::new());
        }
        let limit = limit.clamp(1, 10);
        let cache_key = (query.to_lowercase(), limit);
        if let Some(cached) = self.cache.lock().unwrap().get(&cache_key) {
            return Ok(cached);
        }

        let providers: &[&str] = if has_non_ascii(query) {
            &["tencent"]
        } else {
            &["yahoo", "tencent"]
        };

        let mut results: Vec<SearchResult> = Vec::new();
        let mut failures = 0usize;

        for &provider in providers {
            let res = match provider {
                "yahoo" => self.yahoo_search(query).await,
                _ => self.tencent_search(query).await,
            };
            match res {
                Ok(mut list) => {
                    if provider == "yahoo" && should_expand(query, &list) {
                        if let Ok(extra) = self.yahoo_search(&format!("{query} corp")).await {
                            list.extend(extra);
                        }
                    }
                    results = list;
                    if !results.is_empty() {
                        break;
                    }
                }
                Err(_) => {
                    failures += 1;
                }
            }
        }

        if results.is_empty() && failures == providers.len() {
            return Err(SearchUnavailableError);
        }

        let final_list = finalize_results(results, limit);
        self.cache
            .lock()
            .unwrap()
            .insert(cache_key, final_list.clone());
        Ok(final_list)
    }

    async fn yahoo_search(&self, query: &str) -> Result<Vec<SearchResult>, String> {
        let resp = self
            .http
            .get(YAHOO_SEARCH_URL)
            .query(&[
                ("q", query),
                ("quotesCount", "20"),
                ("newsCount", "0"),
                ("listsCount", "0"),
            ])
            .timeout(Duration::from_secs(8))
            .send()
            .await
            .map_err(|e| e.to_string())?;
        if !resp.status().is_success() {
            return Err(format!("HTTP {}", resp.status()));
        }
        let payload: Value = resp.json().await.map_err(|e| e.to_string())?;
        let mut out = Vec::new();
        if let Some(quotes) = payload.get("quotes").and_then(Value::as_array) {
            for q in quotes {
                if let Some(r) = parse_yahoo_result(q) {
                    out.push(r);
                }
            }
        }
        Ok(out)
    }

    async fn tencent_search(&self, query: &str) -> Result<Vec<SearchResult>, String> {
        let resp = self
            .http
            .get(TENCENT_SEARCH_URL)
            .query(&[("q", query), ("t", "all")])
            .header("Referer", "https://gu.qq.com/")
            .timeout(Duration::from_secs(8))
            .send()
            .await
            .map_err(|e| e.to_string())?;
        if !resp.status().is_success() {
            return Err(format!("HTTP {}", resp.status()));
        }
        let text = resp.text().await.map_err(|e| e.to_string())?;
        Ok(parse_tencent_hint(&text))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn yahoo_and_tencent_search_parsing() {
        let jp = parse_yahoo_result(&serde_json::json!({
            "quoteType": "EQUITY",
            "symbol": "6727.T",
            "exchange": "JPX",
            "exchDisp": "Tokyo Stock Exchange",
            "longname": "Wacom Co., Ltd."
        }))
        .unwrap();
        assert_eq!(jp.display_symbol(), "6727.JP");
        assert_eq!(jp.rank, 0);

        let text = "v_hint=\"sh~600519~\\u8d35\\u5dde\\u8305\\u53f0~gzmt~GP-A^hk~00700~\\u817e\\u8baf\\u63a7\\u80a1~txkg~GP^hk~80700~\\u817e\\u8baf\\u63a7\\u80a1r~txkgr~GP^us~tcehy.ps~\\u817e\\u8baf\\u63a7\\u80a1(adr)~txkgadr~GP^hk~13005~\\u817e\\u8baf\\u6cd5\\u5174~txfx~QZ\";";
        let res = parse_tencent_hint(text);
        let syms: Vec<String> = res.iter().map(SearchResult::display_symbol).collect();
        assert_eq!(syms, vec!["600519.A", "00700.HK", "TCEHY.US"]);
        assert_eq!(res[0].name, "贵州茅台");
    }

    #[test]
    fn finalize_prefers_primary_and_deduplicates_otc() {
        let raw = vec![
            SearchResult {
                symbol: "WACMY".into(),
                market: "US".into(),
                name: "Wacom Co., Ltd.".into(),
                exchange: "OTC".into(),
                rank: 2,
                quote_type: "EQUITY".into(),
            },
            SearchResult {
                symbol: "6727".into(),
                market: "JP".into(),
                name: "Wacom Co., Ltd.".into(),
                exchange: "Tokyo".into(),
                rank: 0,
                quote_type: "EQUITY".into(),
            },
            SearchResult {
                symbol: "WACMF".into(),
                market: "US".into(),
                name: "Wacom Co., Ltd.".into(),
                exchange: "OTC".into(),
                rank: 2,
                quote_type: "EQUITY".into(),
            },
        ];
        let out = finalize_results(raw, 5);
        let syms: Vec<String> = out.iter().map(SearchResult::display_symbol).collect();
        assert_eq!(syms, vec!["6727.JP", "WACMY.US"]);
    }
}
