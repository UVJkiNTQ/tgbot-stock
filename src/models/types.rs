use rust_decimal::prelude::*;
use std::str::FromStr;

pub const QTY_SCALE: i64 = 100;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Buy,
    Sell,
}

impl Side {
    pub fn as_str(&self) -> &'static str {
        match self {
            Side::Buy => "BUY",
            Side::Sell => "SELL",
        }
    }

    pub fn from_db(s: &str) -> Result<Self, String> {
        match s {
            "BUY" => Ok(Side::Buy),
            "SELL" => Ok(Side::Sell),
            _ => Err(format!("invalid side: {s}")),
        }
    }
}

#[derive(Debug, Clone)]
pub struct Trade {
    pub id: i64,
    pub user_id: i64,
    pub username: String,
    pub symbol: String,
    pub side: Side,
    pub price: f64,
    pub qty: i64, // integer hundredth-share units; 100 = 1 share
    pub currency: String,
    pub rate: f64,
    pub trade_ts: String,
    pub leverage: f64,
    pub market: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PositionEntry {
    pub symbol: String,
    pub leverage: f64,
    pub qty: i64, // signed hundredth-share units
    pub market: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct TradePlan {
    pub leverage: f64,
    pub current_qty: i64,
    pub new_entry: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DatabaseUpdateResult {
    pub updated: bool,
    pub rows_updated: i64,
    pub old_version: i64,
    pub new_version: i64,
    pub market_rows_updated: i64,
}

#[derive(Debug, Clone)]
pub enum ModelError {
    LeverageMismatch(String),
    DatabaseUpdate(String),
    InvalidInput(String),
    Sqlite(String),
}

impl std::fmt::Display for ModelError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ModelError::LeverageMismatch(s)
            | ModelError::DatabaseUpdate(s)
            | ModelError::InvalidInput(s)
            | ModelError::Sqlite(s) => write!(f, "{s}"),
        }
    }
}

impl std::error::Error for ModelError {}

impl From<rusqlite::Error> for ModelError {
    fn from(e: rusqlite::Error) -> Self {
        ModelError::Sqlite(e.to_string())
    }
}

pub fn quantity_to_units(raw: &str) -> Result<i64, String> {
    let dec = Decimal::from_str(raw.trim()).map_err(|_| "invalid quantity".to_string())?;
    if dec <= Decimal::ZERO {
        return Err("quantity must be positive and finite".to_string());
    }
    let units = dec
        .checked_mul(Decimal::from(QTY_SCALE))
        .ok_or_else(|| "quantity is too large".to_string())?;
    if !units.fract().is_zero() {
        return Err("quantity supports at most two decimal places".to_string());
    }
    units
        .to_i64()
        .filter(|&v| v > 0)
        .ok_or_else(|| "quantity is too large".to_string())
}

pub fn units_to_quantity(units: i64) -> Decimal {
    Decimal::new(units, 2)
}

pub fn validate_qty_units(qty_units: i64) -> Result<i64, String> {
    if qty_units <= 0 {
        return Err("qty must be positive".to_string());
    }
    Ok(qty_units)
}

pub fn normalize_leverage(raw: &str) -> Result<f64, String> {
    let dec = Decimal::from_str(raw.trim()).map_err(|_| "invalid leverage".to_string())?;
    if dec < Decimal::ONE {
        return Err("leverage must be finite and at least 1".to_string());
    }
    let normalized = dec
        .to_f64()
        .ok_or_else(|| "leverage is too large".to_string())?;
    if !normalized.is_finite() || normalized < 1.0 {
        return Err("leverage is too large".to_string());
    }
    Ok(normalized)
}

pub fn normalize_leverage_f64(val: f64) -> Result<f64, String> {
    if !val.is_finite() || val < 1.0 {
        return Err("leverage must be finite and at least 1".to_string());
    }
    Ok(val)
}

pub fn format_leverage_num(leverage: f64) -> String {
    if (leverage - leverage.round()).abs() < 1e-12 && leverage.abs() < 1e12 {
        format!("{}x", leverage.round() as i64)
    } else {
        let s = format!("{leverage:.6}");
        let trimmed = s.trim_end_matches('0').trim_end_matches('.');
        format!("{trimmed}x")
    }
}

pub fn plan_trade(
    entries: &[PositionEntry],
    side: Side,
    requested_leverage: Option<f64>,
    fallback_leverage: Option<f64>,
) -> Result<TradePlan, ModelError> {
    let mut leverage = if let Some(req) = requested_leverage {
        normalize_leverage_f64(req).map_err(ModelError::InvalidInput)?
    } else if !entries.is_empty() {
        if let Some(fb) = fallback_leverage {
            normalize_leverage_f64(fb).map_err(ModelError::InvalidInput)?
        } else {
            1.0
        }
    } else {
        1.0
    };

    let order_sign: i64 = if side == Side::Buy { 1 } else { -1 };
    let opposite_entries: Vec<&PositionEntry> = entries
        .iter()
        .filter(|e| (e.qty > 0 && order_sign < 0) || (e.qty < 0 && order_sign > 0))
        .collect();

    if requested_leverage.is_none() && !opposite_entries.is_empty() {
        return Err(ModelError::LeverageMismatch(
            "普通反向交易必须显式填写目标杠杆，例如 1x；也可以使用 ALL 或 /close 一次平仓"
                .to_string(),
        ));
    }

    if requested_leverage.is_none() && !entries.is_empty() && fallback_leverage.is_none() {
        let same_direction: Vec<&PositionEntry> = entries
            .iter()
            .filter(|e| (e.qty > 0 && order_sign > 0) || (e.qty < 0 && order_sign < 0))
            .collect();
        if same_direction.len() == 1 {
            leverage = same_direction[0].leverage;
        } else {
            return Err(ModelError::LeverageMismatch(
                "无法确定沿用的杠杆，请显式填写目标杠杆".to_string(),
            ));
        }
    }

    let current_qty = entries
        .iter()
        .find(|e| (e.leverage - leverage).abs() < 1e-9)
        .map(|e| e.qty)
        .unwrap_or(0);

    if current_qty == 0 && !opposite_entries.is_empty() {
        let available = opposite_entries
            .iter()
            .map(|e| format_leverage_num(e.leverage))
            .collect::<Vec<_>>()
            .join("、");
        return Err(ModelError::LeverageMismatch(format!(
            "没有 {} 的对应反向持仓；可平仓杠杆：{}。请填写正确杠杆，或使用 ALL / /close",
            format_leverage_num(leverage),
            available
        )));
    }

    Ok(TradePlan {
        leverage,
        current_qty,
        new_entry: !entries.is_empty() && current_qty == 0,
    })
}
