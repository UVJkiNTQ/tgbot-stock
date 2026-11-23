use crate::cache::TtlCache;
use crate::chartstyle::{hex_color, ChartFont};
use crate::http_client::{fetch_yahoo_chart, ExchangeTz};
use crate::instrument::{resolve_instrument, yahoo_symbol, zfill};
use crate::quotes::Quote;
use chrono::{NaiveDate, NaiveDateTime};
use reqwest::Client;
use serde_json::Value;
use std::sync::Mutex;
use std::time::Duration;
use tiny_skia::{
    FillRule, Paint, PathBuilder, Pixmap, Rect, Stroke, Transform,
};

const INTRADAY_CACHE_TTL_SECS: u64 = 20;
const INTRADAY_CACHE_MAX: usize = 128;
const CHART_WIDTH: u32 = 1000;
const CHART_HEIGHT: u32 = 460;
const MARGIN_LEFT: f32 = 78.0;
const MARGIN_RIGHT: f32 = 88.0;
const MARGIN_TOP: f32 = 36.0;
const MARGIN_BOTTOM: f32 = 36.0;
const UP_COLOR: &str = "#d64541";
const DOWN_COLOR: &str = "#1e9e6a";
const FLAT_COLOR: &str = "#555555";
const AVG_COLOR: &str = "#f0a020";

#[derive(Debug, Clone)]
pub struct IntradayPoint {
    pub time: NaiveDateTime,
    pub price: f64,
    pub avg: Option<f64>,
    pub volume: Option<f64>,
}

#[derive(Debug, Clone)]
pub struct Intraday {
    pub symbol: String,
    pub prev_close: f64,
    pub points: Vec<IntradayPoint>,
    pub name: String,
}

fn tencent_code(symbol: &str, market: &str) -> Result<String, String> {
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
        _ => Err(format!("no minute source for {symbol} ({market})")),
    }
}

fn attach_vwap(points: &mut [IntradayPoint]) {
    let mut price_volume = 0.0;
    let mut volume_total = 0.0;
    for point in points.iter_mut() {
        if let Some(v) = point.volume {
            if v > 0.0 {
                price_volume += point.price * v;
                volume_total += v;
            }
        }
        point.avg = Some(if volume_total > 0.0 {
            price_volume / volume_total
        } else {
            point.price
        });
    }
}

pub fn parse_tencent_rows(rows: &[String], date_str: &str) -> Vec<IntradayPoint> {
    let Ok(day) = NaiveDate::parse_from_str(date_str, "%Y%m%d") else {
        return Vec::new();
    };
    let mut points = Vec::with_capacity(rows.len());
    let mut cumulative_volume = 0.0_f64;

    for row in rows {
        let parts: Vec<&str> = row.split_whitespace().collect();
        if parts.len() < 2 {
            continue;
        }
        let hhmm = parts[0];
        if hhmm.len() != 4 || !hhmm.bytes().all(|b| b.is_ascii_digit()) {
            continue;
        }
        let Ok(price) = parts[1].parse::<f64>() else {
            continue;
        };
        if !price.is_finite() || price <= 0.0 {
            continue;
        }
        let mut volume = None;
        if parts.len() >= 3 {
            if let Ok(current) = parts[2].parse::<f64>() {
                if current.is_finite() {
                    if current >= cumulative_volume {
                        volume = Some(current - cumulative_volume);
                    }
                    cumulative_volume = current.max(cumulative_volume);
                }
            }
        }
        let hour: u32 = hhmm[..2].parse().unwrap_or(0);
        let minute: u32 = hhmm[2..].parse().unwrap_or(0);
        if let Some(dt) = day.and_hms_opt(hour, minute, 0) {
            points.push(IntradayPoint {
                time: dt,
                price,
                avg: None,
                volume,
            });
        }
    }
    attach_vwap(&mut points);
    points
}

pub fn parse_yahoo_chart(result: &Value) -> Vec<IntradayPoint> {
    let Some(timestamps) = result.get("timestamp").and_then(Value::as_array) else {
        return Vec::new();
    };
    let Some(quote_node) = result.pointer("/indicators/quote/0") else {
        return Vec::new();
    };
    let closes = quote_node.get("close").and_then(Value::as_array);
    let volumes = quote_node.get("volume").and_then(Value::as_array);
    let Some(closes) = closes else {
        return Vec::new();
    };

    let tz = ExchangeTz::from_yahoo_meta(result.get("meta").unwrap_or(&Value::Null));
    let mut points = Vec::with_capacity(timestamps.len());

    for (idx, ts_val) in timestamps.iter().enumerate() {
        let Some(ts) = ts_val.as_i64() else {
            continue;
        };
        let Some(price) = closes.get(idx).and_then(Value::as_f64) else {
            continue;
        };
        if !price.is_finite() || price <= 0.0 {
            continue;
        }
        let volume = volumes
            .and_then(|arr| arr.get(idx))
            .and_then(Value::as_f64)
            .filter(|v| v.is_finite() && *v > 0.0);
        let Some(dt) = tz.timestamp_to_naive(ts) else {
            continue;
        };
        points.push(IntradayPoint {
            time: dt,
            price,
            avg: None,
            volume,
        });
    }
    attach_vwap(&mut points);
    points
}

pub struct IntradayService {
    http: Client,
    cache: Mutex<TtlCache<(String, String), Intraday>>,
}

impl IntradayService {
    pub fn new(http: Client) -> Self {
        Self {
            http,
            cache: Mutex::new(TtlCache::new(
                Duration::from_secs(INTRADAY_CACHE_TTL_SECS),
                INTRADAY_CACHE_MAX,
            )),
        }
    }

    pub async fn get_intraday(&self, quote: &Quote) -> Option<Intraday> {
        if quote.market == "FUND" {
            return None;
        }
        let key = (quote.symbol.clone(), quote.market.clone());
        if let Some(cached) = self.cache.lock().unwrap().get(&key) {
            return Some(cached);
        }

        let points = if matches!(quote.market.as_str(), "US" | "JP") {
            let ticker = yahoo_symbol(&quote.symbol, &quote.market).ok()?;
            let node = fetch_yahoo_chart(&self.http, &ticker, "1d", "1m", false).await?;
            parse_yahoo_chart(&node)
        } else {
            self.fetch_tencent(&quote.symbol, &quote.market).await
        };

        if points.len() < 2 {
            return None;
        }

        let prev_close = if quote.prev_close.is_finite() && quote.prev_close > 0.0 {
            quote.prev_close
        } else {
            points[0].price
        };

        let data = Intraday {
            symbol: quote.symbol.clone(),
            prev_close,
            points,
            name: quote.name.clone(),
        };
        self.cache.lock().unwrap().insert(key, data.clone());
        Some(data)
    }

    async fn fetch_tencent(&self, symbol: &str, market: &str) -> Vec<IntradayPoint> {
        let Ok(code) = tencent_code(symbol, market) else {
            return Vec::new();
        };
        let url = format!("https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}");
        let resp = match self
            .http
            .get(&url)
            .header("Referer", "https://gu.qq.com/")
            .timeout(Duration::from_secs(8))
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
        let Some(body) = payload
            .get("data")
            .and_then(|d| d.get(&code))
            .and_then(|n| n.get("data"))
        else {
            return Vec::new();
        };
        let date_str = body
            .get("date")
            .and_then(Value::as_str)
            .unwrap_or("");
        if date_str.is_empty() {
            return Vec::new();
        }
        let rows: Vec<String> = body
            .get("data")
            .and_then(Value::as_array)
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        parse_tencent_rows(&rows, date_str)
    }
}

fn price_decimals(price: f64) -> usize {
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

pub fn render_chart(data: &Intraday, font: &ChartFont) -> Option<Vec<u8>> {
    let points = &data.points;
    if points.len() < 2 {
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

    let mut lo = data.prev_close;
    let mut hi = data.prev_close;
    for p in points {
        lo = lo.min(p.price);
        hi = hi.max(p.price);
        if let Some(a) = p.avg {
            lo = lo.min(a);
            hi = hi.max(a);
        }
    }
    let pad = if hi - lo < 1e-9 {
        (hi.abs() * 0.002).max(0.01)
    } else {
        (hi - lo) * 0.06
    };
    lo -= pad;
    hi += pad;
    let span = hi - lo;

    let count = points.len();
    let x_positions: Vec<f32> = (0..count)
        .map(|i| x0 + plot_width * (i as f32) / ((count - 1) as f32))
        .collect();

    let price_y = |v: f64| -> f32 {
        price_bottom - ((v - lo) / span) as f32 * (price_bottom - y0)
    };

    let grid_color = hex_color("#e3e6ea", 255);
    let axis_color = hex_color("#9aa3ad", 255);
    let label_color = hex_color("#4a4f55", 255);
    let muted_color = hex_color("#8a9096", 255);

    for step in 0..5 {
        let value = lo + span * (step as f64) / 4.0;
        let y = price_y(value);
        stroke_line(&mut pixmap, x0, y, x1, y, grid_color, 1.0);
        let dec = price_decimals(value);
        font.draw_text(
            &mut pixmap,
            x1 + 6.0,
            y - 7.0,
            &format!("{value:.dec$}"),
            13.0,
            label_color,
        );
        if data.prev_close > 0.0 {
            let pct = (value - data.prev_close) / data.prev_close * 100.0;
            font.draw_text(
                &mut pixmap,
                x0 - 58.0,
                y - 7.0,
                &format!("{pct:+.2}%"),
                13.0,
                muted_color,
            );
        }
    }

    let baseline_y = price_y(data.prev_close);
    let dash_color = hex_color("#b0b6bc", 255);
    let mut dx = x0;
    while dx < x1 {
        stroke_line(&mut pixmap, dx, baseline_y, (dx + 6.0).min(x1), baseline_y, dash_color, 1.0);
        dx += 10.0;
    }
    let bdec = price_decimals(data.prev_close);
    font.draw_text(
        &mut pixmap,
        x1 + 6.0,
        baseline_y - 7.0,
        &format!("{:.bdec$}", data.prev_close),
        13.0,
        muted_color,
    );

    let last_price = points.last().unwrap().price;
    let line_hex = if last_price > data.prev_close {
        UP_COLOR
    } else if last_price < data.prev_close {
        DOWN_COLOR
    } else {
        FLAT_COLOR
    };
    let line_color = hex_color(line_hex, 255);

    let max_vol = points
        .iter()
        .filter_map(|p| p.volume)
        .fold(0.0_f64, f64::max);
    if max_vol > 0.0 {
        let bar_w = (plot_width / count as f32 * 0.6).max(1.0);
        let up_vol = hex_color("#f3c1c0", 255);
        let dn_vol = hex_color("#bfe3d4", 255);
        for (i, p) in points.iter().enumerate() {
            let Some(v) = p.volume.filter(|&v| v > 0.0) else {
                continue;
            };
            let bh = (v / max_vol) as f32 * (y1 - volume_top);
            let prev = if i > 0 { points[i - 1].price } else { p.price };
            let col = if p.price >= prev { up_vol } else { dn_vol };
            fill_rect(&mut pixmap, x_positions[i] - bar_w / 2.0, y1 - bh, bar_w, bh, col);
        }
    }

    // VWAP line
    {
        let mut pb = PathBuilder::new();
        let mut started = false;
        for (i, p) in points.iter().enumerate() {
            if let Some(avg) = p.avg {
                let y = price_y(avg);
                if !started {
                    pb.move_to(x_positions[i], y);
                    started = true;
                } else {
                    pb.line_to(x_positions[i], y);
                }
            }
        }
        if let Some(path) = pb.finish() {
            let mut paint = Paint::default();
            paint.set_color(hex_color(AVG_COLOR, 255));
            paint.anti_alias = true;
            let stroke = Stroke {
                width: 1.0,
                ..Stroke::default()
            };
            pixmap.stroke_path(&path, &paint, &stroke, Transform::identity(), None);
        }
    }

    // Translucent area fill under price line
    {
        let mut pb = PathBuilder::new();
        pb.move_to(x0, price_bottom);
        for (i, p) in points.iter().enumerate() {
            pb.line_to(x_positions[i], price_y(p.price));
        }
        pb.line_to(x1, price_bottom);
        pb.close();
        if let Some(path) = pb.finish() {
            let mut paint = Paint::default();
            paint.set_color(hex_color(line_hex, 0x24));
            paint.anti_alias = true;
            pixmap.fill_path(&path, &paint, FillRule::Winding, Transform::identity(), None);
        }
    }

    // Main price curve
    {
        let mut pb = PathBuilder::new();
        for (i, p) in points.iter().enumerate() {
            let y = price_y(p.price);
            if i == 0 {
                pb.move_to(x_positions[i], y);
            } else {
                pb.line_to(x_positions[i], y);
            }
        }
        if let Some(path) = pb.finish() {
            let mut paint = Paint::default();
            paint.set_color(line_color);
            paint.anti_alias = true;
            let stroke = Stroke {
                width: 2.0,
                ..Stroke::default()
            };
            pixmap.stroke_path(&path, &paint, &stroke, Transform::identity(), None);
        }
    }

    // Last point dot
    if let Some(circle) = PathBuilder::from_circle(
        *x_positions.last().unwrap(),
        price_y(last_price),
        3.0,
    ) {
        let mut paint = Paint::default();
        paint.set_color(line_color);
        paint.anti_alias = true;
        pixmap.fill_path(&circle, &paint, FillRule::Winding, Transform::identity(), None);
    }

    // Time ticks
    for step in 0..5 {
        let idx = ((count - 1) as f64 * step as f64 / 4.0).round() as usize;
        let label = points[idx].time.format("%H:%M").to_string();
        let tw = font.text_width(&label, 13.0);
        let tx = (x_positions[idx] - tw / 2.0).max(x0).min(x1);
        font.draw_text(&mut pixmap, tx, y1 + 8.0, &label, 13.0, label_color);
    }

    // Title & summary
    let change_pct = if data.prev_close != 0.0 {
        (last_price - data.prev_close) / data.prev_close * 100.0
    } else {
        0.0
    };
    let raw_title = if !data.name.is_empty() && data.name != data.symbol && font.has_cjk_font {
        format!("{}({})", data.name, data.symbol)
    } else {
        data.symbol.clone()
    };
    let ldec = price_decimals(last_price);
    let summary = format!("{last_price:.ldec$}  {change_pct:+.2}%");
    let sw = font.text_width(&summary, 18.0);
    let title = font.fit_text(&raw_title, 18.0, x1 - sw - x0 - 16.0);
    font.draw_text(&mut pixmap, x0, 12.0, &title, 18.0, hex_color("#222222", 255));
    font.draw_text(&mut pixmap, x1 - sw, 12.0, &summary, 18.0, line_color);

    stroke_line(&mut pixmap, x0, price_bottom, x1, price_bottom, axis_color, 1.0);
    stroke_line(&mut pixmap, x0, y1, x1, y1, axis_color, 1.0);

    pixmap.encode_png().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tencent_minute_volume_diff_and_vwap() {
        let rows = vec![
            "0930 10.00 100 1000".to_string(),
            "0931 11.00 300 3200".to_string(),
            "0932 10.50 400 4200".to_string(),
        ];
        let pts = parse_tencent_rows(&rows, "20261120");
        assert_eq!(pts.len(), 3);
        assert_eq!(pts[0].volume, Some(100.0));
        assert_eq!(pts[1].volume, Some(200.0));
        assert!((pts[2].avg.unwrap() - (10.0 * 100.0 + 11.0 * 200.0 + 10.5 * 100.0) / 400.0).abs() < 1e-9);
    }

    #[test]
    fn render_intraday_png() {
        let font = ChartFont::load(None);
        let day = NaiveDate::from_ymd_opt(2026, 11, 20).unwrap();
        let pts: Vec<IntradayPoint> = (0..20)
            .map(|i| IntradayPoint {
                time: day.and_hms_opt(9, 30 + i, 0).unwrap(),
                price: 10.0 + i as f64 * 0.05,
                avg: Some(10.0 + i as f64 * 0.03),
                volume: Some(100.0 + i as f64 * 10.0),
            })
            .collect();
        let png = render_chart(
            &Intraday {
                symbol: "600000".into(),
                prev_close: 10.2,
                points: pts,
                name: "浦发银行".into(),
            },
            &font,
        )
        .unwrap();
        assert!(png.starts_with(b"\x89PNG\r\n\x1a\n"));
        assert!(png.len() > 1000);
    }

    #[test]
    fn yahoo_minute_parse_skips_nulls() {
        let payload = serde_json::json!({
            "meta": {"exchangeTimezoneName": "America/New_York"},
            "timestamp": [1763562600, 1763562660, 1763562720],
            "indicators": {
                "quote": [{
                    "close": [10.0, null, 12.0],
                    "volume": [100, 50, 300]
                }]
            }
        });
        let pts = parse_yahoo_chart(&payload);
        assert_eq!(pts.len(), 2);
        assert_eq!(pts[0].price, 10.0);
        assert_eq!(pts[1].price, 12.0);
        assert!((pts[1].avg.unwrap() - (10.0 * 100.0 + 12.0 * 300.0) / 400.0).abs() < 1e-9);
    }
}
