use crate::handlers::common::{
    calculate_amount_qty, confirm_keyboard, format_compact_price, format_leverage, format_money,
    format_qty, format_with_commas, html_escape, parse_amount_trade_args, parse_close_args,
    parse_trade_args, price_deviation, quote_missing_text, ParsedTradeArgs, TradeQty,
    PRICE_DEVIATION_PCT,
};
use crate::handlers::AppState;
use crate::instrument_search::MAX_QUERY_LENGTH;
use crate::intraday;
use crate::kline;
use crate::models::{plan_trade, validate_qty_units, PositionEntry, Side, QTY_SCALE};
use crate::quotes::{market_currency, quote_currency, Quote};
use crate::telegram::{
    BufferedInputFile, CommandObject, InputMediaPhoto, Message, PendingTrade,
};
use rust_decimal::prelude::*;
use std::str::FromStr;

fn market_label(market: &str) -> &str {
    match market {
        "A" => "A股",
        "HK" => "港股",
        "US" => "美股",
        "FUND" => "基金",
        "BSE" => "北交所",
        "JP" => "日股",
        other => other,
    }
}

fn is_closed_by(entry: &PositionEntry, side: Side) -> bool {
    (side == Side::Buy && entry.qty < 0) || (side == Side::Sell && entry.qty > 0)
}

fn close_entries_text(entries: &[PositionEntry]) -> String {
    entries
        .iter()
        .map(|entry| {
            let action = if entry.qty > 0 { "卖出" } else { "买入" };
            format!(
                "{} {action}{}股",
                format_leverage(entry.leverage),
                format_qty(entry.qty.abs())
            )
        })
        .collect::<Vec<_>>()
        .join("、")
}

async fn invalidate_previous_confirmation(state: &AppState, msg: &Message, user_id: i64) {
    let Some(prev) = state.fsm.get(msg.chat.id, user_id) else {
        return;
    };
    let Some(prev_msg_id) = prev.confirmation_message_id else {
        return;
    };
    let _ = state
        .bot
        .edit_message_text(
            msg.chat.id,
            prev_msg_id,
            "该确认已失效：你已发起新的交易，请使用最新确认按钮。",
            None,
        )
        .await;
}

fn trade_usage(side: Side, at_market: bool) -> String {
    let command = if side == Side::Buy { "buy" } else { "sell" };
    if at_market {
        return format!(
            "用法：/{command}n SYMBOL QTY [Nx]\n\
             或：/{command}n SYMBOL ALL [Nx]\n\
             示例：/{command}n 600000 100 5x\n\
             使用 /quote 同源现价，确认后按确认单价格记录；杠杆规则与原命令相同"
        );
    }
    let example_price = if side == Side::Buy { "10.50" } else { "11.00" };
    let all_label = if side == Side::Buy { "平空仓" } else { "平多仓" };
    format!(
        "用法：/{command} SYMBOL PRICE QTY [Nx]\n\
         或：/{command} SYMBOL PRICE ALL [Nx]\n\
         示例：/{command} 600000 {example_price} 100 5x\n\
         {all_label}：/{command} 600000 {example_price} ALL\n\
         如填写杠杆必须以 x 结尾，例如 1x、2.5x、5x；\
         已有同向持仓时可省略，系统会沿用最近一笔交易的杠杆"
    )
}

fn amount_trade_usage(side: Side, at_market: bool) -> String {
    let command = match (side, at_market) {
        (Side::Buy, true) => "buyna",
        (Side::Sell, true) => "sellna",
        (Side::Buy, false) => "buya",
        (Side::Sell, false) => "sella",
    };
    let price_arg = if at_market { "" } else { "PRICE " };
    let example_price = if at_market { "" } else { "10.50 " };
    let action = if side == Side::Buy { "买入" } else { "卖出" };
    format!(
        "用法：/{command} SYMBOL {price_arg}AMOUNT [Nx] [Ns]\n\
         示例：/{command} 600000 {example_price}10000 5x 100s\n\
         按不超过人民币预算计算最大可{action}量（AMOUNT 为人民币）；\
         100s、1s、01s、001s 分别表示最小 100、1、0.1、0.01 股"
    )
}

fn with_placeholder_price(args: Option<&str>) -> Option<String> {
    let mut parts: Vec<&str> = args?.split_whitespace().collect();
    if parts.is_empty() {
        return None;
    }
    parts.insert(1, "1");
    Some(parts.join(" "))
}

pub async fn cmd_quote(state: &AppState, msg: &Message, cmd: &CommandObject) {
    let Some(raw_args) = cmd.args.as_deref() else {
        let _ = state
            .bot
            .reply_text(msg, "用法：/quote SYMBOL\n示例：/quote 600000", None)
            .await;
        return;
    };
    let symbol = raw_args.trim().to_uppercase();
    let quote = state.quotes.get_quote(&symbol, None).await;
    let Some(quote) = quote.filter(|q| q.price != 0.0) else {
        let alt = quote_missing_text(&state.quotes, &symbol).await;
        let _ = state
            .bot
            .reply_text(msg, &format!("未找到 {symbol} 的行情数据{alt}"), None)
            .await;
        return;
    };

    let change = quote.price - quote.prev_close;
    let change_pct = if quote.prev_close != 0.0 {
        change / quote.prev_close * 100.0
    } else {
        0.0
    };
    let sign = if change >= 0.0 { "+" } else { "" };
    let mlabel = market_label(&quote.market);
    let currency = quote_currency(&quote);

    if quote.market == "FUND" {
        let _ = state
            .bot
            .reply_text(
                msg,
                &format!(
                    "{} ({}) [基金]\n估算净值：{:.4}  单位净值：{:.4}  {currency}",
                    quote.name, quote.symbol, quote.price, quote.prev_close
                ),
                None,
            )
            .await;
        return;
    }

    let caption = format!(
        "{} ({}) [{mlabel}]\n\
         现价：{:.4}  {sign}{change:.4} ({sign}{change_pct:.2}%)  {currency}\n\
         今开：{:.4}  昨收：{:.4}\n\
         最高：{:.4}  最低：{:.4}",
        quote.name, quote.symbol, quote.price, quote.open, quote.prev_close, quote.high, quote.low
    );

    let (intraday_data, daily_data) = tokio::join!(
        state.intraday.get_intraday(&quote),
        state.kline.get_daily(&quote),
    );

    let mut charts: Vec<(&str, Vec<u8>)> = Vec::new();
    if let Some(data) = intraday_data {
        if let Some(png) = intraday::render_chart(&data, &state.font) {
            charts.push(("intraday", png));
        }
    }
    if let Some(series) = daily_data {
        if let Some(png) = kline::render_chart(&series, Some(quote.prev_close), &state.font) {
            charts.push(("daily", png));
        }
    }

    if charts.len() == 1 {
        let (kind, png) = charts.remove(0);
        if state
            .bot
            .reply_photo(
                msg,
                BufferedInputFile {
                    data: png,
                    filename: format!("{}-{kind}.png", quote.symbol),
                },
                Some(&caption),
            )
            .await
            .is_ok()
        {
            return;
        }
    } else if !charts.is_empty() {
        let media: Vec<InputMediaPhoto> = charts
            .into_iter()
            .enumerate()
            .map(|(idx, (kind, png))| InputMediaPhoto {
                media: BufferedInputFile {
                    data: png,
                    filename: format!("{}-{kind}.png", quote.symbol),
                },
                caption: if idx == 0 {
                    Some(caption.clone())
                } else {
                    None
                },
            })
            .collect();
        if state.bot.reply_media_group(msg, media).await.is_ok() {
            return;
        }
    }

    let _ = state.bot.reply_text(msg, &caption, None).await;
}

pub async fn cmd_find(state: &AppState, msg: &Message, cmd: &CommandObject) {
    let query = cmd.args.as_deref().unwrap_or("").trim();
    if query.is_empty() {
        let _ = state
            .bot
            .reply_text(msg, "用法：/find 关键词\n示例：/find wacom 或 /find 腾讯", None)
            .await;
        return;
    }
    if query.chars().count() > MAX_QUERY_LENGTH {
        let _ = state
            .bot
            .reply_text(
                msg,
                &format!("关键词过长，请控制在 {MAX_QUERY_LENGTH} 个字符内"),
                None,
            )
            .await;
        return;
    }

    let results = match state.search.search(query, 5).await {
        Ok(r) => r,
        Err(_) => {
            let _ = state
                .bot
                .reply_text(msg, "搜索服务暂时不可用，请稍后再试，或直接输入代码查询。", None)
                .await;
            return;
        }
    };

    if results.is_empty() {
        let _ = state
            .bot
            .reply_text(
                msg,
                &format!(
                    "未找到“{}”相关股票。\n可尝试英文名或拼音，或直接输入代码，例如 /quote 7203.JP",
                    html_escape(query)
                ),
                None,
            )
            .await;
        return;
    }

    let mut lines = vec![format!("找到 {} 个结果：", results.len())];
    for (idx, r) in results.iter().enumerate() {
        let ml = market_label(&r.market);
        let ex = if r.exchange.is_empty() {
            String::new()
        } else {
            format!("/{}", html_escape(&r.exchange))
        };
        lines.push(format!(
            "{}. {} — {} [{ml}{ex}]",
            idx + 1,
            html_escape(&r.display_symbol()),
            html_escape(&r.name),
        ));
    }
    lines.push(String::new());
    lines.push(format!("使用：/quote {}", html_escape(&results[0].display_symbol())));
    let _ = state.bot.reply_text(msg, &lines.join("\n"), None).await;
}

pub async fn cmd_trade(
    state: &AppState,
    msg: &Message,
    cmd: &CommandObject,
    side: Side,
    at_market: bool,
) {
    let synthetic = if at_market {
        with_placeholder_price(cmd.args.as_deref())
    } else {
        cmd.args.clone()
    };
    let Some(mut parsed) = parse_trade_args(synthetic.as_deref()) else {
        let _ = state
            .bot
            .reply_text(msg, &trade_usage(side, at_market), None)
            .await;
        return;
    };

    if at_market {
        let quote = state.quotes.get_quote(&parsed.symbol, None).await;
        let Some(quote) = quote.filter(|q| q.price.is_finite() && q.price > 0.0) else {
            let action = if side == Side::Buy { "买入" } else { "卖出" };
            let alt = quote_missing_text(&state.quotes, &parsed.symbol).await;
            let _ = state
                .bot
                .reply_text(
                    msg,
                    &format!("未找到 {} 的行情数据，无法记录{action}{alt}", parsed.symbol),
                    None,
                )
                .await;
            return;
        };
        parsed.price = quote.price;
        cmd_trade_parsed(state, msg, side, parsed, Some(quote), None, None).await;
        return;
    }

    cmd_trade_parsed(state, msg, side, parsed, None, None, None).await;
}

async fn cmd_trade_parsed(
    state: &AppState,
    msg: &Message,
    side: Side,
    parsed: ParsedTradeArgs,
    pre_quote: Option<Quote>,
    pre_rate: Option<f64>,
    amount_cny: Option<Decimal>,
) {
    let Some(user) = &msg.from_user else {
        return;
    };
    let action = if side == Side::Buy { "买入" } else { "卖出" };
    let quote = match pre_quote {
        Some(q) => Some(q),
        None => state.quotes.get_quote(&parsed.symbol, None).await,
    };
    let Some(quote) = quote.filter(|q| q.price > 0.0) else {
        let alt = quote_missing_text(&state.quotes, &parsed.symbol).await;
        let _ = state
            .bot
            .reply_text(
                msg,
                &format!("未找到 {} 的行情数据，无法记录{action}{alt}", parsed.symbol),
                None,
            )
            .await;
        return;
    };

    let name = quote.name.clone();
    let symbol = quote.symbol.clone();
    let market_price = quote.price;
    let deviation_pct = price_deviation(parsed.price, market_price);

    let entries = state
        .db
        .get_position_entries(user.id, Some(&symbol), Some(&quote.market))
        .unwrap_or_default();

    let (is_all, qty, effective_leverage, recent_leverage, position_line, new_entry_notice) =
        match parsed.qty {
            TradeQty::All => {
                let target_entries: Vec<PositionEntry> = entries
                    .into_iter()
                    .filter(|e| {
                        is_closed_by(e, side)
                            && parsed
                                .leverage
                                .is_none_or(|rl| (e.leverage - rl).abs() < 1e-9)
                    })
                    .collect();
                if target_entries.is_empty() {
                    let pos_label = if side == Side::Buy { "空仓" } else { "多仓" };
                    let lev_label = parsed
                        .leverage
                        .map(|l| format!("{} ", format_leverage(l)))
                        .unwrap_or_default();
                    let _ = state
                        .bot
                        .reply_text(
                            msg,
                            &format!(
                                "ALL 仅用于平{lev_label}{pos_label}：{name} ({symbol}) 当前没有对应持仓"
                            ),
                            None,
                        )
                        .await;
                    return;
                }
                let total_qty: i64 = target_entries.iter().map(|e| e.qty.abs()).sum();
                let pl = format!(
                    "\n将一次关闭 {} 个匹配条目：{}",
                    target_entries.len(),
                    close_entries_text(&target_entries)
                );
                (true, total_qty, None, None, pl, String::new())
            }
            TradeQty::Units(req_qty) => {
                let recent = if parsed.leverage.is_none() && !entries.is_empty() {
                    state
                        .db
                        .get_latest_trade_leverage(user.id, &symbol, Some(&quote.market))
                        .ok()
                        .flatten()
                } else {
                    None
                };
                let trade_plan = match plan_trade(&entries, side, parsed.leverage, recent) {
                    Ok(p) => p,
                    Err(exc) => {
                        let _ = state
                            .bot
                            .reply_text(msg, &format!("操作无效：{exc}"), None)
                            .await;
                        return;
                    }
                };
                let delta = if side == Side::Buy { req_qty } else { -req_qty };
                let pl = format!(
                    "\n{} 条目：{} 股 → {} 股",
                    format_leverage(trade_plan.leverage),
                    format_qty(trade_plan.current_qty),
                    format_qty(trade_plan.current_qty + delta)
                );
                let notice = if trade_plan.new_entry {
                    format!(
                        "\n⚠️ 当前已有其他杠杆持仓；本次将建立新的 {} 独立持仓条目",
                        format_leverage(trade_plan.leverage)
                    )
                } else {
                    String::new()
                };
                (
                    false,
                    req_qty,
                    Some(trade_plan.leverage),
                    recent,
                    pl,
                    notice,
                )
            }
        };

    let currency = quote_currency(&quote);
    let rate = match pre_rate {
        Some(r) => r,
        None => match state.quotes.get_rate(currency).await {
            Ok(r) => r,
            Err(_) => {
                let _ = state
                    .bot
                    .reply_text(
                        msg,
                        &format!("暂时无法获取 {currency}/CNY 汇率，请稍后重试"),
                        None,
                    )
                    .await;
                return;
            }
        },
    };

    let total_orig = parsed.price * qty as f64 / QTY_SCALE as f64;
    let total_cny = total_orig * rate;
    let username = user.display_name();

    invalidate_previous_confirmation(state, msg, user.id).await;
    let action_key = if side == Side::Buy { "buy" } else { "sell" };
    state.fsm.set(
        msg.chat.id,
        user.id,
        PendingTrade {
            user_id: user.id,
            username,
            symbol: symbol.clone(),
            market: quote.market.clone(),
            price: parsed.price,
            qty,
            currency: currency.to_string(),
            rate,
            market_price,
            deviation_pct,
            deviation_ack: false,
            close_all: is_all,
            requested_leverage: parsed.leverage,
            effective_leverage,
            confirmation_message_id: None,
        },
    );

    let leverage_line = match effective_leverage {
        Some(eff) => {
            let suffix = if !is_all && parsed.leverage.is_none() && recent_leverage.is_some() {
                "（未指定，沿用最近一笔交易）"
            } else {
                ""
            };
            format!("\n杠杆 {}{suffix}", format_leverage(eff))
        }
        None => String::new(),
    };
    let rate_line = if currency != "CNY" {
        format!(
            "\n汇率 {currency}/CNY: {rate:.4}  (≈¥{})",
            format_with_commas(total_cny, 2)
        )
    } else {
        String::new()
    };
    let budget_line = match amount_cny {
        Some(amt) => format!(
            "\n预算（人民币）：¥{}",
            format_with_commas(amt.to_f64().unwrap_or(0.0), 2)
        ),
        None => String::new(),
    };
    let warn_line = if deviation_pct > PRICE_DEVIATION_PCT {
        format!("\n⚠️ 委托价偏离现价({market_price:.4}) {deviation_pct:.1}%")
    } else {
        String::new()
    };

    let kb = confirm_keyboard(action_key, user.id);
    if let Ok(sent) = state
        .bot
        .reply_text(
            msg,
            &format!(
                "确认{action}：{name} ({symbol})\n\
                 {}股 @ {} = {}{leverage_line}{position_line}{new_entry_notice}{rate_line}{budget_line}{warn_line}",
                format_qty(qty),
                format_money(parsed.price, currency),
                format_money(total_orig, currency),
            ),
            Some(&kb),
        )
        .await
    {
        state
            .fsm
            .set_confirmation_message_id(msg.chat.id, user.id, Some(sent.message_id));
    }
}

pub async fn cmd_amount_trade(
    state: &AppState,
    msg: &Message,
    cmd: &CommandObject,
    side: Side,
    at_market: bool,
) {
    let synthetic = if at_market {
        with_placeholder_price(cmd.args.as_deref())
    } else {
        cmd.args.clone()
    };
    let Some(parsed) = parse_amount_trade_args(synthetic.as_deref(), 1.0, !at_market) else {
        let _ = state
            .bot
            .reply_text(msg, &amount_trade_usage(side, at_market), None)
            .await;
        return;
    };

    let action = if side == Side::Buy { "买入" } else { "卖出" };
    if !at_market && parsed.qty == 0 && market_currency(&parsed.symbol, None) == "CNY" {
        let _ = state
            .bot
            .reply_text(
                msg,
                &format!(
                    "人民币预算 ¥{} 不足：按现价 {}、最小单位 {} 股，无法{action} {}",
                    format_with_commas(parsed.amount.to_f64().unwrap_or(0.0), 2),
                    format_compact_price(parsed.price),
                    format_qty(parsed.min_unit),
                    parsed.symbol,
                ),
                None,
            )
            .await;
        return;
    }

    let quote = state.quotes.get_quote(&parsed.symbol, None).await;
    let Some(quote) = quote.filter(|q| q.price.is_finite() && q.price > 0.0) else {
        let alt = quote_missing_text(&state.quotes, &parsed.symbol).await;
        let _ = state
            .bot
            .reply_text(
                msg,
                &format!("未找到 {} 的行情数据，无法记录{action}{alt}", parsed.symbol),
                None,
            )
            .await;
        return;
    };

    let price = if at_market { quote.price } else { parsed.price };
    let currency = quote_currency(&quote);
    let rate = match state.quotes.get_rate(currency).await {
        Ok(r) => r,
        Err(_) => {
            let _ = state
                .bot
                .reply_text(
                    msg,
                    &format!("暂时无法获取 {currency}/CNY 汇率，请稍后重试"),
                    None,
                )
                .await;
            return;
        }
    };

    let price_dec = match Decimal::from_str(&format!("{price}")) {
        Ok(d) => d,
        Err(_) => return,
    };
    let qty = match calculate_amount_qty(parsed.amount, price_dec, parsed.min_unit, rate) {
        Ok(q) => q,
        Err(_) => {
            let _ = state
                .bot
                .reply_text(msg, "计算数量过大，请减少人民币预算", None)
                .await;
            return;
        }
    };
    if qty == 0 {
        let _ = state
            .bot
            .reply_text(
                msg,
                &format!(
                    "人民币预算 ¥{} 不足：按现价 {} {currency}、最小单位 {} 股，无法{action} {}",
                    format_with_commas(parsed.amount.to_f64().unwrap_or(0.0), 2),
                    format_compact_price(price),
                    format_qty(parsed.min_unit),
                    parsed.symbol,
                ),
                None,
            )
            .await;
        return;
    }
    if validate_qty_units(qty).is_err() {
        let _ = state
            .bot
            .reply_text(msg, "计算数量过大，请减少人民币预算", None)
            .await;
        return;
    }

    cmd_trade_parsed(
        state,
        msg,
        side,
        ParsedTradeArgs {
            symbol: parsed.symbol,
            price,
            qty: TradeQty::Units(qty),
            leverage: parsed.leverage,
        },
        Some(quote),
        Some(rate),
        Some(parsed.amount),
    )
    .await;
}

pub async fn cmd_close(state: &AppState, msg: &Message, cmd: &CommandObject) {
    let Some(user) = &msg.from_user else {
        return;
    };
    let Some((raw_symbol, price)) = parse_close_args(cmd.args.as_deref()) else {
        let _ = state
            .bot
            .reply_text(msg, "用法：/close SYMBOL PRICE\n示例：/close 600000 11.00", None)
            .await;
        return;
    };

    let quote = state.quotes.get_quote(&raw_symbol, None).await;
    let Some(quote) = quote.filter(|q| q.price > 0.0) else {
        let alt = quote_missing_text(&state.quotes, &raw_symbol).await;
        let _ = state
            .bot
            .reply_text(msg, &format!("未找到 {raw_symbol} 的行情数据，无法平仓{alt}"), None)
            .await;
        return;
    };

    let name = quote.name.clone();
    let symbol = quote.symbol.clone();
    let entries = state
        .db
        .get_position_entries(user.id, Some(&symbol), Some(&quote.market))
        .unwrap_or_default();
    if entries.is_empty() {
        let _ = state
            .bot
            .reply_text(msg, &format!("{name} ({symbol}) 当前没有持仓，无需平仓"), None)
            .await;
        return;
    }

    let qty: i64 = entries.iter().map(|e| e.qty.abs()).sum();
    let currency = quote_currency(&quote);
    let rate = match state.quotes.get_rate(currency).await {
        Ok(r) => r,
        Err(_) => {
            let _ = state
                .bot
                .reply_text(
                    msg,
                    &format!("暂时无法获取 {currency}/CNY 汇率，请稍后重试"),
                    None,
                )
                .await;
            return;
        }
    };

    let market_price = quote.price;
    let deviation_pct = price_deviation(price, market_price);
    let total_orig = price * qty as f64 / QTY_SCALE as f64;
    let total_cny = total_orig * rate;

    invalidate_previous_confirmation(state, msg, user.id).await;
    state.fsm.set(
        msg.chat.id,
        user.id,
        PendingTrade {
            user_id: user.id,
            username: user.display_name(),
            symbol: symbol.clone(),
            market: quote.market.clone(),
            price,
            qty,
            currency: currency.to_string(),
            rate,
            market_price,
            deviation_pct,
            deviation_ack: false,
            close_all: true,
            requested_leverage: None,
            effective_leverage: None,
            confirmation_message_id: None,
        },
    );

    let rate_line = if currency != "CNY" {
        format!(
            "\n汇率 {currency}/CNY: {rate:.4}  (≈¥{})",
            format_with_commas(total_cny, 2)
        )
    } else {
        String::new()
    };
    let warn_line = if deviation_pct > PRICE_DEVIATION_PCT {
        format!("\n⚠️ 委托价偏离现价({market_price:.4}) {deviation_pct:.1}%")
    } else {
        String::new()
    };

    let kb = confirm_keyboard("close", user.id);
    if let Ok(sent) = state
        .bot
        .reply_text(
            msg,
            &format!(
                "确认一次平仓：{name} ({symbol})\n\
                 将关闭 {} 个条目：{}\n\
                 合计 {} 股 @ {} = {}{rate_line}{warn_line}",
                entries.len(),
                close_entries_text(&entries),
                format_qty(qty),
                format_money(price, currency),
                format_money(total_orig, currency),
            ),
            Some(&kb),
        )
        .await
    {
        state
            .fsm
            .set_confirmation_message_id(msg.chat.id, user.id, Some(sent.message_id));
    }
}
