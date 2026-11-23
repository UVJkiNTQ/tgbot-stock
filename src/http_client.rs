use chrono::{DateTime, FixedOffset, Utc};
use chrono_tz::Tz;
use reqwest::Client;
use serde_json::Value;
use std::time::Duration;

const YAHOO_CHART_HOSTS: [&str; 2] = [
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
];

pub fn build_http_client() -> Client {
    Client::builder()
        .user_agent("Mozilla/5.0")
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(15))
        .pool_max_idle_per_host(4)
        .build()
        .expect("failed to build reqwest client")
}

#[derive(Debug, Clone)]
pub enum ExchangeTz {
    Named(Tz),
    Fixed(FixedOffset),
}

impl ExchangeTz {
    pub fn from_yahoo_meta(meta: &Value) -> Self {
        if let Some(name) = meta.get("exchangeTimezoneName").and_then(Value::as_str) {
            if let Ok(tz) = name.parse::<Tz>() {
                return Self::Named(tz);
            }
        }
        if let Some(offset) = meta.get("gmtoffset").and_then(Value::as_i64) {
            if let Some(fixed) = FixedOffset::east_opt(offset as i32) {
                return Self::Fixed(fixed);
            }
        }
        Self::Fixed(FixedOffset::east_opt(0).unwrap())
    }

    pub fn timestamp_to_naive(&self, ts: i64) -> Option<chrono::NaiveDateTime> {
        let dt_utc = DateTime::<Utc>::from_timestamp(ts, 0)?;
        match self {
            Self::Named(tz) => Some(dt_utc.with_timezone(tz).naive_local()),
            Self::Fixed(off) => Some(dt_utc.with_timezone(off).naive_local()),
        }
    }
}

pub async fn fetch_yahoo_chart(
    client: &Client,
    ticker: &str,
    range: &str,
    interval: &str,
    include_prepost: bool,
) -> Option<Value> {
    let prepost = if include_prepost { "true" } else { "false" };
    for host in YAHOO_CHART_HOSTS {
        let url = format!("{host}/v8/finance/chart/{ticker}");
        let resp = match client
            .get(&url)
            .query(&[
                ("range", range),
                ("interval", interval),
                ("includePrePost", prepost),
            ])
            .timeout(Duration::from_secs(10))
            .send()
            .await
        {
            Ok(r) => r,
            Err(_) => continue,
        };
        if resp.status().as_u16() == 404 {
            return None;
        }
        if !resp.status().is_success() {
            continue;
        }
        let payload: Value = match resp.json().await {
            Ok(v) => v,
            Err(_) => continue,
        };
        if let Some(first) = payload
            .pointer("/chart/result/0")
            .filter(|v| v.is_object())
        {
            return Some(first.clone());
        }
        return None;
    }
    None
}
