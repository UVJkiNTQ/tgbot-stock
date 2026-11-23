use crate::cache::TtlCache;
use crate::chartstyle::{hex_color, ChartFont};
use crate::http_client::{fetch_yahoo_chart, ExchangeTz};
use crate::instrument::{resolve_instrument, yahoo_symbol, zfill};
use crate::quotes::Quote;
use chrono::NaiveDate;
use reqwest::Client;
use serde_json::Value;
use std::sync::Mutex;
use std::time::Duration;
use tiny_skia::{Paint, PathBuilder, Pixmap, Rect, Stroke, Transform};

const KLINE_CACHE_TTL_SECS: u64 = 60;
const KLINE_CACHE_MAX: usize = 128;
const KLINE_FETCH_DAYS: usize = 120;
const VISIBLE_BARS: usize = 90;
const CHART_WIDTH: u32 = 1000;
const CHART_HEIGHT: u32 = 460;
const MARGIN_LEFT: f32 = 20.0;
const MARGIN_RIGHT: f32 = 86.0;
const MARGIN_TOP: f32 = 36.0;
const MARGIN_BOTTOM: f32 = 36.0;
const UP_COLOR: &str = "#d64541";
const DOWN_COLOR: &str = "#1e9e6a";

#[derive(Debug, Clone)]
pub struct DailyBar {
    pub day: NaiveDate,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: Option<f64>,
}

#[derive(Debug, Clone)]
pub struct DailySeries {
    pub symbol: String,
    pub bars: Vec<DailyBar>,
    pub name: String,
}

fn provider_code(symbol: &str, market: &str) -> Result<String, String> {
    let (base, _) = resolve_instrument(symbol, Some(market))?;
    match market {
        "HK" => Ok(format!("hk{}", zfill(&base, 5))),
        "BSE" => Ok(format!("bj{}", zfill(&base, 6))),
        "A" => {
            if matches!(base.as_bytes().first(), Some(b'5' | b'6' | b'9')) {
                Ok(format!("sh{base}"))
            } else {
                Ok(format!("sz{base}"))
            }
        }
        _ => Err(format!("no daily source for {symbol} ({market})")),
    }
}

fn normalize_bar(
    day: NaiveDate,
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    volume: Option<f64>,
) -> Option<DailyBar> {
    if [open, high, low, close]
        .iter()
        .any(|v| !v.is_finite() || *v <= 0.0)
    {
        return None;
    }
    Some(DailyBar {
        day,
        open,
        high: high.max(open).max(close),
        low: low.min(open).min(close),
        close,
        volume: volume.filter(|v| v.is_finite() && *v > 0.0),
    })
}

fn json_as_f64(v: &Value) -> Option<f64> {
    v.as_f64()
        .or_else(|| v.as_str().and_then(|s| s.parse::<f64>().ok()))
}

pub fn parse_tencent_rows(rows: &[Value]) -> Vec<DailyBar> {
    let mut bars = Vec::with_capacity(rows.len());
    for row in rows {
        let Some(arr) = row.as_array() else {
            continue;
        };
        if arr.len() < 6 {
            continue;
        }
        let Some(date_s) = arr[0].as_str().map(|s| &s[..s.len().min(10)]) else {
            continue;
        };
        let Ok(day) = NaiveDate::parse_from_str(date_s, "%Y-%m-%d") else {
            continue;
        };
        let (Some(open), Some(close), Some(high), Some(low)) = (
            json_as_f64(&arr[1]),
            json_as_f64(&arr[2]),
            json_as_f64(&arr[3]),
            json_as_f64(&arr[4]),
        ) else {
            continue;
        };
        let volume = json_as_f64(&arr[5]);
        if let Some(bar) = normalize_bar(day, open, high, low, close, volume) {
            bars.push(bar);
        }
    }
    bars.sort_by_key(|b| b.day);
    bars
}

pub fn parse_sina_rows(rows: &[Value]) -> Vec<DailyBar> {
    let mut bars = Vec::with_capacity(rows.len());
    for row in rows {
        let Some(obj) = row.as_object() else {
            continue;
        };
        let Some(date_s) = obj
            .get("day")
            .and_then(Value::as_str)
            .map(|s| &s[..s.len().min(10)])
        else {
            continue;
        };
        let Ok(day) = NaiveDate::parse_from_str(date_s, "%Y-%m-%d") else {
            continue;
        };
        let (Some(open), Some(high), Some(low), Some(close)) = (
            obj.get("open").and_then(json_as_f64),
            obj.get("high").and_then(json_as_f64),
            obj.get("low").and_then(json_as_f64),
            obj.get("close").and_then(json_as_f64),
        ) else {
            continue;
        };
        let volume = obj.get("volume").and_then(json_as_f64);
        if let Some(bar) = normalize_bar(day, open, high, low, close, volume) {
            bars.push(bar);
        }
    }
    bars.sort_by_key(|b| b.day);
    bars
}

pub fn parse_yahoo_chart(result: &Value) -> Vec<DailyBar> {
    let Some(timestamps) = result.get("timestamp").and_then(Value::as_array) else {
        return Vec::new();
    };
    let Some(q) = result.pointer("/indicators/quote/0") else {
        return Vec::new();
    };
    let (Some(opens), Some(highs), Some(lows), Some(closes)) = (
        q.get("open").and_then(Value::as_array),
        q.get("high").and_then(Value::as_array),
        q.get("low").and_then(Value::as_array),
        q.get("close").and_then(Value::as_array),
    ) else {
        return Vec::new();
    };
    let volumes = q.get("volume").and_then(Value::as_array);
    let tz = ExchangeTz::from_yahoo_meta(result.get("meta").unwrap_or(&Value::Null));

    let mut bars = Vec::with_capacity(timestamps.len());
    for (idx, ts_val) in timestamps.iter().enumerate() {
        let Some(ts) = ts_val.as_i64() else {
            continue;
        };
        let (Some(open), Some(high), Some(low), Some(close)) = (
            opens.get(idx).and_then(Value::as_f64),
            highs.get(idx).and_then(Value::as_f64),
            lows.get(idx).and_then(Value::as_f64),
            closes.get(idx).and_then(Value::as_f64),
        ) else {
            continue;
        };
        let volume = volumes.and_then(|v| v.get(idx)).and_then(Value::as_f64);
        let Some(day) = tz.timestamp_to_naive(ts).map(|dt| dt.date()) else {
            continue;
        };
        if let Some(bar) = normalize_bar(day, open, high, low, close, volume) {
            bars.push(bar);
        }
    }
    bars.sort_by_key(|b| b.day);
    bars
}

pub struct KlineService {
    http: Client,
    cache: Mutex<TtlCache<(String, String), DailySeries>>,
}

impl KlineService {
    pub fn new(http: Client) -> Self {
        Self {
            http,
            cache: Mutex::new(TtlCache::new(
                Duration::from_secs(KLINE_CACHE_TTL_SECS),
                KLINE_CACHE_MAX,
            )),
        }
    }

    pub async fn get_daily(&self, quote: &Quote) -> Option<DailySeries> {
        if quote.market == "FUND" {
            return None;
        }
        let key = (quote.symbol.clone(), quote.market.clone());
        if let Some(cached) = self.cache.lock().unwrap().get(&key) {
            return Some(cached);
        }

        let mut bars = match quote.market.as_str() {
            "US" | "JP" => {
                let ticker = yahoo_symbol(&quote.symbol, &quote.market).ok()?;
                let node = fetch_yahoo_chart(&self.http, &ticker, "6mo", "1d", false).await?;
                parse_yahoo_chart(&node)
            }
            "BSE" => self.fetch_sina(&quote.symbol, &quote.market).await,
            _ => self.fetch_tencent(&quote.symbol, &quote.market).await,
        };

        if bars.len() < 2 {
            return None;
        }
        if bars.len() > KLINE_FETCH_DAYS {
            bars.drain(0..bars.len() - KLINE_FETCH_DAYS);
        }

        let series = DailySeries {
            symbol: quote.symbol.clone(),
            bars,
            name: quote.name.clone(),
        };
        self.cache.lock().unwrap().insert(key, series.clone());
        Some(series)
    }

    async fn fetch_tencent(&self, symbol: &str, market: &str) -> Vec<DailyBar> {
        let Ok(code) = provider_code(symbol, market) else {
            return Vec::new();
        };
        let url = format!(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{KLINE_FETCH_DAYS},qfq"
        );
        let resp = match self
            .http
            .get(&url)
            .header("Referer", "https://gu.qq.com/")
            .timeout(Duration::from_secs(10))
            .send()
            .await
        {
            Ok(r) if r.status().is_success() => r,
            _ => return Vec::new(),
        };
        let payload: Value = match resp.json().await {
            Ok(v) => v,
            Err(_) => return Vec::new(),
        };
        let Some(node) = payload.get("data").and_then(|d| d.get(&code)) else {
            return Vec::new();
        };
        let rows = node
            .get("qfqday")
            .or_else(|| node.get("day"))
            .and_then(Value::as_array);
        rows.map(|r| parse_tencent_rows(r)).unwrap_or_default()
    }

    async fn fetch_sina(&self, symbol: &str, market: &str) -> Vec<DailyBar> {
        let Ok(code) = provider_code(symbol, market) else {
            return Vec::new();
        };
        let url = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData";
        let resp = match self
            .http
            .get(url)
            .query(&[
                ("symbol", code.as_str()),
                ("scale", "240"),
                ("ma", "1"),
                ("datalen", &KLINE_FETCH_DAYS.to_string()),
            ])
            .header("Referer", "https://finance.sina.com.cn")
            .timeout(Duration::from_secs(10))
            .send()
            .await
        {
            Ok(r) if r.status().is_success() => r,
            _ => return Vec::new(),
        };
        let payload: Value = match resp.json().await {
            Ok(v) => v,
            Err(_) => return Vec::new(),
        };
        payload
            .as_array()
            .map(|r| parse_sina_rows(r))
            .unwrap_or_default()
    }
}

fn decimals(price: f64) -> usize {
    if price.abs() < 10.0 {
        3
    } else {
        2
    }
}

fn stroke_line(pixmap: &mut Pixmap, x0: f32, y0: f32, x1: f32, y1: f32, color: tiny_skia::Color, width: f32) {
    let mut pb = PathBuilder::new();
    pb.move_to(x0, y0);
    pb.line_to(x1, y1);
    if let Some(path) = pb.finish() {
        let mut paint = Paint::default();
        paint.set_color(color);
        paint.anti_alias = true;
        let stroke = Stroke {
            width,
            ..Stroke::default()
        };
        pixmap.stroke_path(&path, &paint, &stroke, Transform::identity(), None);
    }
}

fn fill_rect(pixmap: &mut Pixmap, x: f32, y: f32, w: f32, h: f32, color: tiny_skia::Color) {
    if w <= 0.0 || h <= 0.0 {
        return;
    }
    if let Some(rect) = Rect::from_xywh(x, y, w, h) {
        let mut paint = Paint::default();
        paint.set_color(color);
        pixmap.fill_rect(rect, &paint, Transform::identity(), None);
    }
}

pub fn render_chart(
    series: &DailySeries,
    prev_close: Option<f64>,
    font: &ChartFont,
) -> Option<Vec<u8>> {
    let start = series.bars.len().saturating_sub(VISIBLE_BARS);
    let bars = &series.bars[start..];
    if bars.is_empty() {
        return None;
    }

    let mut pixmap = Pixmap::new(CHART_WIDTH, CHART_HEIGHT)?;
    pixmap.fill(tiny_skia::Color::WHITE);

    let x0 = MARGIN_LEFT;
    let x1 = CHART_WIDTH as f32 - MARGIN_RIGHT;
    let y0 = MARGIN_TOP;
    let y1 = CHART_HEIGHT as f32 - MARGIN_BOTTOM;
    let plot_width = x1 - x0;
    let plot_height = y1 - y0;
    let price_bottom = y0 + plot_height * 0.76;
    let volume_top = price_bottom + 10.0;

    let mut hi = f64::NEG_INFINITY;
    let mut lo = f64::INFINITY;
    for b in bars {
        hi = hi.max(b.high);
        lo = lo.min(b.low);
    }
    let pad = if hi - lo < 1e-9 {
        (hi.abs() * 0.002).max(0.01)
    } else {
        (hi - lo) * 0.06
    };
    lo -= pad;
    hi += pad;
    let span = hi - lo;

    let count = bars.len();
    let step = plot_width / count as f32;
    let price_y = |v: f64| -> f32 {
        price_bottom - ((v - lo) / span) as f32 * (price_bottom - y0)
    };

    let grid_color = hex_color("#e3e6ea", 255);
    let label_color = hex_color("#4a4f55", 255);

    for tick in 0..5 {
        let value = lo + span * (tick as f64) / 4.0;
        let y = price_y(value);
        stroke_line(&mut pixmap, x0, y, x1, y, grid_color, 1.0);
        let dec = decimals(value);
        font.draw_text(
            &mut pixmap,
            x1 + 6.0,
            y - 7.0,
            &format!("{value:.dec$}"),
            13.0,
            label_color,
        );
    }

    let max_vol = bars.iter().filter_map(|b| b.volume).fold(0.0_f64, f64::max);
    let body_w = (step * 0.62).min(14.0).max(1.0);
    let up_col = hex_color(UP_COLOR, 255);
    let dn_col = hex_color(DOWN_COLOR, 255);
    let up_vol = hex_color("#f3c1c0", 255);
    let dn_vol = hex_color("#bfe3d4", 255);

    for (index, bar) in bars.iter().enumerate() {
        let center = x0 + step * (index as f32 + 0.5);
        let is_up = bar.close >= bar.open;
        let color = if is_up { up_col } else { dn_col };

        stroke_line(
            &mut pixmap,
            center,
            price_y(bar.high),
            center,
            price_y(bar.low),
            color,
            1.0,
        );

        let top = price_y(bar.open.max(bar.close));
        let mut bottom = price_y(bar.open.min(bar.close));
        if bottom - top < 1.0 {
            bottom = top + 1.0;
        }
        fill_rect(&mut pixmap, center - body_w / 2.0, top, body_w, bottom - top, color);

        if max_vol > 0.0 {
            if let Some(vol) = bar.volume.filter(|&v| v > 0.0) {
                let bh = (vol / max_vol) as f32 * (y1 - volume_top);
                let light = if is_up { up_vol } else { dn_vol };
                fill_rect(&mut pixmap, center - body_w / 2.0, y1 - bh, body_w, bh, light);
            }
        }
    }

    let last = bars.last().unwrap();
    let last_y = price_y(last.close);
    stroke_line(&mut pixmap, x0, last_y, x1, last_y, hex_color("#b0b6bc", 255), 1.0);
    let ldec = decimals(last.close);
    font.draw_text(
        &mut pixmap,
        x1 + 6.0,
        last_y - 7.0,
        &format!("{:.ldec$}", last.close),
        13.0,
        hex_color("#222222", 255),
    );

    let tick_indices: Vec<usize> = if count >= 5 {
        (0..5)
            .map(|t| ((count - 1) as f64 * t as f64 / 4.0).round() as usize)
            .collect()
    } else {
        (0..count).collect()
    };
    for idx in tick_indices {
        let label = bars[idx].day.format("%m-%d").to_string();
        let tw = font.text_width(&label, 13.0);
        let tx = (x0 + step * (idx as f32 + 0.5) - tw / 2.0)
            .max(x0)
            .min(x1);
        font.draw_text(&mut pixmap, tx, y1 + 8.0, &label, 13.0, label_color);
    }

    let previous_close = match prev_close {
        Some(p) if p > 0.0 => p,
        _ => {
            if count >= 2 {
                bars[count - 2].close
            } else {
                last.close
            }
        }
    };
    let change_pct = if previous_close != 0.0 {
        (last.close - previous_close) / previous_close * 100.0
    } else {
        0.0
    };
    let line_color = if change_pct >= 0.0 { up_col } else { dn_col };
    let raw_title = if !series.name.is_empty() && series.name != series.symbol && font.has_cjk_font {
        format!("{}({})", series.name, series.symbol)
    } else {
        series.symbol.clone()
    };
    let summary = format!("{:.ldec$}  {change_pct:+.2}%", last.close);
    let sw = font.text_width(&summary, 18.0);
    let title = font.fit_text(&raw_title, 18.0, x1 - sw - x0 - 16.0);
    font.draw_text(&mut pixmap, x0, 12.0, &title, 18.0, hex_color("#222222", 255));
    font.draw_text(&mut pixmap, x1 - sw, 12.0, &summary, 18.0, line_color);

    let axis_color = hex_color("#9aa3ad", 255);
    stroke_line(&mut pixmap, x0, price_bottom, x1, price_bottom, axis_color, 1.0);
    stroke_line(&mut pixmap, x0, y1, x1, y1, axis_color, 1.0);

    pixmap.encode_png().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tencent_and_yahoo_kline_parse_and_render() {
        let rows = vec![
            serde_json::json!(["2026-11-20", "10.20", "10.10", "10.40", "10.00", "23456.00"]),
            serde_json::json!(["2026-11-19", "10.00", "10.20", "10.30", "9.90", "12345.00"]),
        ];
        let bars = parse_tencent_rows(&rows);
        assert_eq!(bars.len(), 2);
        assert!(bars[0].day < bars[1].day);

        let font = ChartFont::load(None);
        let png = render_chart(
            &DailySeries {
                symbol: "600000".into(),
                bars,
                name: "浦发银行".into(),
            },
            None,
            &font,
        )
        .unwrap();
        assert!(png.starts_with(b"\x89PNG\r\n\x1a\n"));

        let yahoo_payload = serde_json::json!({
            "meta": {"exchangeTimezoneName": "America/New_York"},
            "timestamp": [1763562600, 1763649000, 1763735400],
            "indicators": {
                "quote": [{
                    "open": [100.0, null, 102.0],
                    "high": [105.0, null, 106.0],
                    "low": [99.0, null, 101.0],
                    "close": [104.0, null, 105.5],
                    "volume": [1000, null, 2000]
                }]
            }
        });
        let ybars = parse_yahoo_chart(&yahoo_payload);
        assert_eq!(ybars.len(), 2);
        assert_eq!(ybars[0].close, 104.0);
        assert_eq!(ybars[1].volume, Some(2000.0));
    }
}
