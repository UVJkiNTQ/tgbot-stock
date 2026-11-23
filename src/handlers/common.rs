use crate::instrument::normalize_symbol;
use crate::models::{
    format_leverage_num, normalize_leverage, quantity_to_units, units_to_quantity,
    validate_qty_units,
};
use crate::quotes::{quote_currency, QuoteService};
use crate::telegram::{
    Bot, CallbackQuery, FsmStorage, InlineKeyboardButton, InlineKeyboardMarkup, PendingTrade,
};
use rust_decimal::prelude::*;
use std::str::FromStr;

pub const PRICE_DEVIATION_PCT: f64 = 5.0;

const QUALIFIED_SUFFIXES: &[&str] = &[".F", ".A", ".US", ".HK", ".BSE", ".BJ", ".JP"];

pub fn html_escape(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for ch in input.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            _ => out.push(ch),
        }
    }
    out
}

fn is_qualified_symbol(raw: &str) -> bool {
    QUALIFIED_SUFFIXES.iter().any(|s| raw.ends_with(s))
        || (raw.starts_with("JP")
            && raw.len() > 2
            && raw[2..].bytes().all(|b| b.is_ascii_digit()))
}

pub fn price_deviation(price: f64, market_price: f64) -> f64 {
    if market_price <= 0.0 {
        return 0.0;
    }
    (price - market_price).abs() / market_price * 100.0
}

pub fn confirm_keyboard(action: &str, user_id: i64) -> InlineKeyboardMarkup {
    let label = match action {
        "buy" => "买入",
        "sell" => "卖出",
        "close" => "平仓",
        other => other,
    };
    InlineKeyboardMarkup {
        inline_keyboard: vec![vec![
            InlineKeyboardButton {
                text: format!("确认{label}"),
                callback_data: format!("{action}_ok:{user_id}"),
            },
            InlineKeyboardButton {
                text: "取消".to_string(),
                callback_data: format!("cancel:{user_id}"),
            },
        ]],
    }
}

pub fn callback_owner(callback: &CallbackQuery) -> Option<i64> {
    let data = callback.data.as_deref()?;
    let (_, owner) = data.split_once(':')?;
    owner.parse::<i64>().ok()
}

pub fn format_with_commas(value: f64, decimals: usize) -> String {
    let formatted = format!("{value:.decimals$}");
    let (neg, rest) = match formatted.strip_prefix('-') {
        Some(r) => (true, r),
        None => (false, formatted.as_str()),
    };
    let (int_part, frac_part) = match rest.split_once('.') {
        Some((i, f)) => (i, Some(f)),
        None => (rest, None),
    };
    let mut grouped = String::with_capacity(int_part.len() + int_part.len() / 3);
    let len = int_part.len();
    for (i, ch) in int_part.chars().enumerate() {
        if i > 0 && (len - i) % 3 == 0 {
            grouped.push(',');
        }
        grouped.push(ch);
    }
    let sign = if neg { "-" } else { "" };
    match frac_part {
        Some(f) => format!("{sign}{grouped}.{f}"),
        None => format!("{sign}{grouped}"),
    }
}

pub fn format_money(amount: f64, currency: &str) -> String {
    let num = format_with_commas(amount, 4);
    if currency == "CNY" {
        format!("¥{num}")
    } else {
        format!("{currency} {num}")
    }
}

pub fn format_qty(qty: i64) -> String {
    let dec = units_to_quantity(qty);
    let s = format!("{dec:.2}");
    s.trim_end_matches('0').trim_end_matches('.').to_string()
}

pub fn format_leverage(leverage: f64) -> String {
    format_leverage_num(leverage)
}

pub fn format_compact_price(price: f64) -> String {
    let s = format!("{price:.6}");
    s.trim_end_matches('0').trim_end_matches('.').to_string()
}

pub fn parse_leverage(raw: &str) -> Result<f64, String> {
    let token = raw.trim().to_uppercase();
    let Some(value) = token.strip_suffix('X') else {
        return Err("leverage must end with x".to_string());
    };
    if value.is_empty() {
        return Err("missing leverage value".to_string());
    }
    normalize_leverage(value)
}

pub fn parse_min_unit(raw: &str) -> Result<i64, String> {
    let token = raw.trim().to_lowercase();
    let Some(value) = token.strip_suffix('s') else {
        return Err("minimum unit must end with s".to_string());
    };
    if value.is_empty() {
        return Err("missing minimum unit".to_string());
    }
    if value.bytes().all(|b| b.is_ascii_digit()) && value.starts_with('0') {
        let converted = format!("0.{}", &value[1..]);
        quantity_to_units(&converted)
    } else {
        quantity_to_units(value)
    }
}

pub fn calculate_amount_qty(
    amount_cny: Decimal,
    price: Decimal,
    min_unit: i64,
    rate: f64,
) -> Result<i64, String> {
    let rate_dec =
        Decimal::from_str(&format!("{rate}")).map_err(|_| "invalid exchange rate".to_string())?;
    if rate_dec <= Decimal::ZERO {
        return Err("invalid exchange rate".to_string());
    }
    let unit_cost_cny = price
        .checked_mul(units_to_quantity(min_unit))
        .and_then(|v| v.checked_mul(rate_dec))
        .ok_or_else(|| "cost overflow".to_string())?;
    if unit_cost_cny <= Decimal::ZERO {
        return Err("invalid unit cost".to_string());
    }
    let lots = (amount_cny / unit_cost_cny).floor();
    let lots_i64 = lots
        .to_i64()
        .ok_or_else(|| "quantity is too large".to_string())?;
    lots_i64
        .checked_mul(min_unit)
        .ok_or_else(|| "quantity is too large".to_string())
}

#[derive(Debug, Clone, PartialEq)]
pub enum TradeQty {
    All,
    Units(i64),
}

#[derive(Debug, Clone)]
pub struct ParsedTradeArgs {
    pub symbol: String,
    pub price: f64,
    pub qty: TradeQty,
    pub leverage: Option<f64>,
}

#[derive(Debug, Clone)]
pub struct ParsedAmountTradeArgs {
    pub symbol: String,
    pub price: f64,
    pub amount: Decimal,
    pub qty: i64,
    pub min_unit: i64,
    pub leverage: Option<f64>,
}

pub fn parse_amount_trade_args(
    args: Option<&str>,
    rate: f64,
    calculate_qty: bool,
) -> Option<ParsedAmountTradeArgs> {
    let parts: Vec<&str> = args?.split_whitespace().collect();
    if parts.len() < 3 || parts.len() > 5 {
        return None;
    }

    let price = Decimal::from_str(parts[1]).ok()?;
    let amount = Decimal::from_str(parts[2]).ok()?;
    if price <= Decimal::ZERO || amount <= Decimal::ZERO {
        return None;
    }

    let mut leverage: Option<f64> = None;
    let mut min_unit: i64 = 1;
    let mut saw_min_unit = false;

    for opt in &parts[3..] {
        let lower_last = opt.chars().last()?.to_ascii_lowercase();
        if lower_last == 'x' && leverage.is_none() {
            leverage = Some(parse_leverage(opt).ok()?);
        } else if lower_last == 's' && !saw_min_unit {
            min_unit = parse_min_unit(opt).ok()?;
            saw_min_unit = true;
        } else {
            return None;
        }
    }

    let qty = if calculate_qty {
        let q = calculate_amount_qty(amount, price, min_unit, rate).ok()?;
        if q != 0 {
            validate_qty_units(q).ok()?;
        }
        q
    } else {
        0
    };

    let raw_symbol = parts[0].to_uppercase();
    let symbol = if is_qualified_symbol(&raw_symbol) {
        raw_symbol
    } else {
        normalize_symbol(&raw_symbol).ok()?
    };
    let parsed_price = price.to_f64()?;
    if !parsed_price.is_finite() || parsed_price <= 0.0 {
        return None;
    }

    Some(ParsedAmountTradeArgs {
        symbol,
        price: parsed_price,
        amount,
        qty,
        min_unit,
        leverage,
    })
}

pub fn parse_trade_args(args: Option<&str>) -> Option<ParsedTradeArgs> {
    let parts: Vec<&str> = args?.split_whitespace().collect();
    if parts.len() != 3 && parts.len() != 4 {
        return None;
    }

    let price: f64 = parts[1].parse().ok()?;
    if !price.is_finite() || price <= 0.0 {
        return None;
    }

    let qty = if parts[2].eq_ignore_ascii_case("ALL") {
        TradeQty::All
    } else {
        TradeQty::Units(quantity_to_units(parts[2]).ok()?)
    };

    let leverage = if parts.len() == 4 {
        Some(parse_leverage(parts[3]).ok()?)
    } else {
        None
    };

    let raw_symbol = parts[0].to_uppercase();
    let symbol = if is_qualified_symbol(&raw_symbol) {
        raw_symbol
    } else {
        normalize_symbol(&raw_symbol).ok()?
    };

    Some(ParsedTradeArgs {
        symbol,
        price,
        qty,
        leverage,
    })
}

pub fn parse_close_args(args: Option<&str>) -> Option<(String, f64)> {
    let parts: Vec<&str> = args?.split_whitespace().collect();
    if parts.len() != 2 {
        return None;
    }
    let synthetic = format!("{} {} ALL", parts[0], parts[1]);
    let parsed = parse_trade_args(Some(&synthetic))?;
    Some((parsed.symbol, parsed.price))
}

pub async fn quote_missing_text(quotes: &QuoteService, symbol: &str) -> String {
    let Some(alt) = quotes.get_japan_alternative(symbol).await else {
        return String::new();
    };
    if !alt.price.is_finite() || alt.price <= 0.0 {
        return String::new();
    }
    let currency = quote_currency(&alt);
    format!(
        "\n疑似日本股票：{} ({}) {:.2} {currency}，请改用 {}.JP 重试。",
        alt.name, alt.symbol, alt.price, alt.symbol
    )
}

pub async fn verify_confirm_owner(
    bot: &Bot,
    callback: &CallbackQuery,
    pending: Option<&PendingTrade>,
    require_pending: bool,
) -> bool {
    if Some(callback.from_user.id) != callback_owner(callback) {
        let _ = bot
            .answer_callback_query(&callback.id, Some("只有发起人可以操作此按钮"), true)
            .await;
        return false;
    }
    if !require_pending {
        return true;
    }
    let Some(data) = pending else {
        let _ = bot
            .answer_callback_query(&callback.id, Some("会话已过期，请重新发起"), true)
            .await;
        return false;
    };
    if callback.from_user.id != data.user_id {
        let _ = bot
            .answer_callback_query(&callback.id, Some("会话已过期，请重新发起"), true)
            .await;
        return false;
    }
    if let Some(expected_msg_id) = data.confirmation_message_id {
        let current_msg_id = callback.message.as_ref().map(|m| m.message_id);
        if current_msg_id != Some(expected_msg_id) {
            let _ = bot
                .answer_callback_query(
                    &callback.id,
                    Some("该确认已失效，请使用最新的确认按钮"),
                    true,
                )
                .await;
            return false;
        }
    }
    true
}

pub async fn needs_deviation_reconfirm(
    bot: &Bot,
    fsm: &FsmStorage,
    callback: &CallbackQuery,
    pending: &PendingTrade,
    action: &str,
    label: &str,
) -> bool {
    if pending.deviation_pct <= PRICE_DEVIATION_PCT || pending.deviation_ack {
        return false;
    }
    let chat_id = callback.message.as_ref().map(|m| m.chat.id).unwrap_or(0);
    let mut updated = pending.clone();
    updated.deviation_ack = true;
    fsm.set(chat_id, pending.user_id, updated);

    if let Some(msg) = &callback.message {
        let kb = confirm_keyboard(action, pending.user_id);
        let _ = bot
            .edit_message_text(
                msg.chat.id,
                msg.message_id,
                &format!(
                    "⚠️ 价格偏离提示\n\n\
                     {} 委托价 {:.4} 与现价 {:.4} 偏离 {:.1}%（超过 {:.0}%）\n\n\
                     确定要按此价格{label}吗？",
                    pending.symbol,
                    pending.price,
                    pending.market_price,
                    pending.deviation_pct,
                    PRICE_DEVIATION_PCT,
                ),
                Some(&kb),
            )
            .await;
    }
    let _ = bot.answer_callback_query(&callback.id, None, false).await;
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compact_min_units_and_amount_trade() {
        assert_eq!(parse_min_unit("100s").unwrap(), 10000);
        assert_eq!(parse_min_unit("1s").unwrap(), 100);
        assert_eq!(parse_min_unit("01s").unwrap(), 10);
        assert_eq!(parse_min_unit("001s").unwrap(), 1);

        let p = parse_amount_trade_args(Some("600000 28.9 10000 100s 5x"), 1.0, true).unwrap();
        assert_eq!(p.qty, 30000);
        assert_eq!(p.leverage, Some(5.0));

        let hk = parse_amount_trade_args(Some("01810 28.9 500"), 0.862692, true).unwrap();
        assert_eq!(hk.qty, 2005);
    }

    #[test]
    fn trade_arg_validation() {
        assert!(parse_trade_args(Some("00700 nan 100")).is_none());
        assert!(parse_trade_args(Some("00700 475.2 1 0.5x")).is_none());
        assert!(parse_trade_args(Some("00700 475.2 0.001")).is_none());
        let p = parse_trade_args(Some("JP7203 3312 1")).unwrap();
        assert_eq!(p.symbol, "JP7203");
        assert_eq!(p.qty, TradeQty::Units(100));
    }
}
