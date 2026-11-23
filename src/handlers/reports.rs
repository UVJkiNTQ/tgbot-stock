use crate::handlers::common::{
    format_leverage, format_money, format_qty, format_with_commas, parse_leverage,
};
use crate::handlers::AppState;
use crate::models::{Side, Trade};
use crate::pnl::UserPnl;
use crate::telegram::{CommandObject, Message};
use chrono::{DateTime, NaiveDate, NaiveDateTime};
use chrono_tz::Tz;
use std::collections::HashSet;

pub fn format_trade_time(trade_ts: &str, tz: Tz) -> String {
    if let Ok(dt) = DateTime::parse_from_rfc3339(trade_ts) {
        return dt
            .with_timezone(&tz)
            .format("%Y-%m-%d %H:%M:%S")
            .to_string();
    }
    if let Ok(ndt) = NaiveDateTime::parse_from_str(trade_ts, "%Y-%m-%dT%H:%M:%S%.f")
        .or_else(|_| NaiveDateTime::parse_from_str(trade_ts, "%Y-%m-%d %H:%M:%S"))
    {
        return ndt
            .and_utc()
            .with_timezone(&tz)
            .format("%Y-%m-%d %H:%M:%S")
            .to_string();
    }
    if let Ok(nd) = NaiveDate::parse_from_str(trade_ts, "%Y-%m-%d") {
        if let Some(ndt) = nd.and_hms_opt(0, 0, 0) {
            return ndt
                .and_utc()
                .with_timezone(&tz)
                .format("%Y-%m-%d %H:%M:%S")
                .to_string();
        }
    }
    trade_ts.chars().take(10).collect()
}

fn instrument_label(name: &str, symbol: &str) -> String {
    if !name.is_empty() && name != symbol {
        format!("{name}({symbol})")
    } else {
        symbol.to_string()
    }
}

pub fn position_lines(result: &UserPnl) -> Vec<String> {
    let mut lines = vec![format!("{} 持仓汇总：\n", result.username)];
    for pos in &result.positions {
        let direction = if pos.qty < 0 { "  [空]" } else { "" };
        if !pos.quote_available {
            lines.push(format!(
                "{} × {}{direction}  [{}] [{}]\n  开仓均价 {:.4}\n  ⚠️ 行情不可用，暂不计入市值、浮盈和汇总",
                instrument_label(&pos.name, &pos.symbol),
                format_qty(pos.qty),
                pos.currency,
                format_leverage(pos.leverage),
                pos.avg_cost,
            ));
            continue;
        }

        let sign = if pos.unrealized_pnl >= 0.0 { "+" } else { "" };
        let sign_cny = if pos.unrealized_pnl_cny >= 0.0 { "+" } else { "" };
        let entry_label = if pos.qty < 0 { "开仓均价" } else { "成本" };
        let cur_price = pos.current_price.unwrap_or(0.0);

        if pos.is_foreign() {
            lines.push(format!(
                "{} × {}{direction}  [{}] [{}]\n  {entry_label} {:.4}  现价 {:.4}\n  汇率 {:.4}  ¥{entry_label} {:.4}  ¥现价 {:.4}\n  浮盈 {sign}{:.2} {} (≈¥{sign_cny}{:.2}, {sign}{:.2}%)\n  保证金 ¥{}",
                instrument_label(&pos.name, &pos.symbol),
                format_qty(pos.qty),
                pos.currency,
                format_leverage(pos.leverage),
                pos.avg_cost,
                cur_price,
                pos.rate.unwrap_or(1.0),
                pos.avg_cost_cny,
                pos.current_price_cny.unwrap_or(0.0),
                pos.unrealized_pnl,
                pos.currency,
                pos.unrealized_pnl_cny,
                pos.unrealized_pnl_pct,
                format_with_commas(pos.margin_cny, 2),
            ));
        } else {
            lines.push(format!(
                "{} × {}{direction}  [{}]\n  {entry_label} {:.4}  现价 {:.4}\n  浮盈 {sign}{:.2} ({sign}{:.2}%)\n  保证金 ¥{}",
                instrument_label(&pos.name, &pos.symbol),
                format_qty(pos.qty),
                format_leverage(pos.leverage),
                pos.avg_cost,
                cur_price,
                pos.unrealized_pnl,
                pos.unrealized_pnl_pct,
                format_with_commas(pos.margin_cny, 2),
            ));
        }
    }

    let total_sign = if result.total_unrealized_pnl_cny >= 0.0 {
        "+"
    } else {
        ""
    };
    let total_pct = if result.total_margin_cny != 0.0 {
        result.total_unrealized_pnl_cny / result.total_margin_cny * 100.0
    } else {
        0.0
    };
    let value_label = if result.positions.iter().any(|p| p.qty < 0) {
        "净市值（空头按负值）："
    } else {
        "总市值："
    };
    lines.push(format!(
        "\n{value_label}¥{}\n总名义成本：¥{}\n总保证金：¥{}\n总浮盈：{total_sign}¥{} ({total_sign}{total_pct:.2}%)",
        format_with_commas(result.total_market_value_cny, 2),
        format_with_commas(result.total_cost_cny, 2),
        format_with_commas(result.total_margin_cny, 2),
        format_with_commas(result.total_unrealized_pnl_cny, 2),
    ));

    let unavailable_count = result
        .positions
        .iter()
        .filter(|p| !p.quote_available)
        .count();
    if unavailable_count > 0 {
        lines.push(format!(
            "\n⚠️ {unavailable_count} 个持仓行情不可用，市值、浮盈和汇总暂未计入"
        ));
    }
    lines
}

pub async fn cmd_position(state: &AppState, msg: &Message) {
    let Some(user) = &msg.from_user else {
        return;
    };
    let result = match state
        .pnl
        .compute_user_pnl(&state.db, &state.quotes, user.id, true)
        .await
    {
        Ok(r) => r,
        Err(_) => {
            let _ = state
                .bot
                .reply_text(msg, "暂时无法获取外币汇率，请稍后重试", None)
                .await;
            return;
        }
    };
    if result.positions.is_empty() {
        let _ = state.bot.reply_text(msg, "你当前没有持仓", None).await;
        return;
    }
    let _ = state
        .bot
        .reply_text(msg, &position_lines(&result).join("\n"), None)
        .await;
}

pub async fn cmd_pnl(state: &AppState, msg: &Message) {
    let Some(user) = &msg.from_user else {
        return;
    };
    let mut result = match state
        .pnl
        .compute_user_pnl(&state.db, &state.quotes, user.id, true)
        .await
    {
        Ok(r) => r,
        Err(_) => {
            let _ = state
                .bot
                .reply_text(msg, "暂时无法获取外币汇率，请稍后重试", None)
                .await;
            return;
        }
    };
    if result.positions.is_empty() && result.realized.is_empty() {
        let _ = state
            .bot
            .reply_text(msg, "你还没有交易记录，无法计算损益", None)
            .await;
        return;
    }

    if !result.realized.is_empty() {
        state
            .pnl
            .populate_realized_names(&mut result, &state.quotes)
            .await;
    }

    let mut lines = vec![format!("{} 损益汇总：\n", result.username), "已实现收益：".to_string()];
    if !result.realized.is_empty() {
        for item in &result.realized {
            let sign = if item.realized_pnl_cny >= 0.0 { "+" } else { "" };
            let sign_orig = if item.realized_pnl >= 0.0 { "+" } else { "" };
            let return_pct = if item.closed_margin_cny != 0.0 {
                item.realized_pnl_cny / item.closed_margin_cny * 100.0
            } else {
                0.0
            };
            let detail = if item.is_foreign() {
                format!(
                    "{sign_orig}{:.2} {}  (≈¥{sign}{:.2}, {sign}{return_pct:.2}%)",
                    item.realized_pnl, item.currency, item.realized_pnl_cny
                )
            } else {
                format!("{sign}¥{:.2} ({sign}{return_pct:.2}%)", item.realized_pnl_cny)
            };
            lines.push(format!(
                "{} [{}]  {detail}",
                instrument_label(&item.name, &item.symbol),
                format_leverage(item.leverage),
            ));
        }
    } else {
        lines.push("暂无已平仓交易".to_string());
    }

    lines.push("\n浮动盈亏：".to_string());
    if !result.positions.is_empty() {
        for pos in &result.positions {
            let direction = if pos.qty < 0 { " [空]" } else { "" };
            let leverage = format!(" [{}]", format_leverage(pos.leverage));
            if !pos.quote_available {
                lines.push(format!(
                    "{}{direction}{leverage}  ⚠️ 行情不可用，暂不计入浮盈",
                    instrument_label(&pos.name, &pos.symbol)
                ));
                continue;
            }
            let sign = if pos.unrealized_pnl_cny >= 0.0 { "+" } else { "" };
            let sign_orig = if pos.unrealized_pnl >= 0.0 { "+" } else { "" };
            if pos.is_foreign() {
                lines.push(format!(
                    "{}{direction}{leverage}  {sign_orig}{:.2} {}  (≈¥{sign}{:.2}, {sign_orig}{:.2}%)",
                    instrument_label(&pos.name, &pos.symbol),
                    pos.unrealized_pnl,
                    pos.currency,
                    pos.unrealized_pnl_cny,
                    pos.unrealized_pnl_pct,
                ));
            } else {
                lines.push(format!(
                    "{}{direction}{leverage}  {sign_orig}{:.2} ({sign_orig}{:.2}%)",
                    instrument_label(&pos.name, &pos.symbol),
                    pos.unrealized_pnl,
                    pos.unrealized_pnl_pct,
                ));
            }
        }
    } else {
        lines.push("暂无未平仓持仓".to_string());
    }

    let realized_sign = if result.total_realized_pnl_cny >= 0.0 { "+" } else { "" };
    let unrealized_sign = if result.total_unrealized_pnl_cny >= 0.0 { "+" } else { "" };
    let total_pnl = result.total_pnl_cny();
    let total_sign = if total_pnl >= 0.0 { "+" } else { "" };
    let total_margin = result.total_pnl_margin_cny();
    let total_pct = if total_margin != 0.0 {
        total_pnl / total_margin * 100.0
    } else {
        0.0
    };
    lines.push(format!(
        "\n已实现合计：{realized_sign}¥{}\n浮动合计：{unrealized_sign}¥{}\n历史总收益：{total_sign}¥{} ({total_sign}{total_pct:.2}%)",
        format_with_commas(result.total_realized_pnl_cny, 2),
        format_with_commas(result.total_unrealized_pnl_cny, 2),
        format_with_commas(total_pnl, 2),
    ));

    let _ = state.bot.reply_text(msg, &lines.join("\n"), None).await;
}

pub async fn cmd_leaderboard(state: &AppState, msg: &Message, cmd: &CommandObject) {
    let mode = cmd
        .args
        .as_deref()
        .unwrap_or("u")
        .trim()
        .to_lowercase();
    if mode != "u" && mode != "r" {
        let _ = state
            .bot
            .reply_text(
                msg,
                "用法：/lb [u|r]\nu = 历史总收益（包括当前浮盈）\nr = 已实现收益（不计当前持仓盈亏）",
                None,
            )
            .await;
        return;
    }

    let include_unrealized = mode == "u";
    let board = match state
        .pnl
        .compute_leaderboard(&state.db, &state.quotes, include_unrealized)
        .await
    {
        Ok(b) => b,
        Err(_) => {
            let _ = state
                .bot
                .reply_text(msg, "暂时无法获取外币汇率，请稍后重试", None)
                .await;
            return;
        }
    };

    if board.is_empty() {
        let empty = if include_unrealized {
            "还没有人有交易记录，开搞吧！"
        } else {
            "还没有人产生已实现收益"
        };
        let _ = state.bot.reply_text(msg, empty, None).await;
        return;
    }

    let title = if include_unrealized {
        "历史总收益排行榜（含浮盈，CNY口径）：\n"
    } else {
        "已实现收益排行榜（不含浮盈，CNY口径）：\n"
    };
    let mut lines = vec![title.to_string()];
    for (idx, user_pnl) in board.iter().enumerate() {
        let (amount, basis) = if include_unrealized {
            (user_pnl.total_pnl_cny(), user_pnl.total_pnl_margin_cny())
        } else {
            (user_pnl.total_realized_pnl_cny, user_pnl.total_closed_margin_cny)
        };
        let ret = amount / basis * 100.0;
        let sign = if ret >= 0.0 { "+" } else { "" };
        let amount_sign = if amount >= 0.0 { "+" } else { "" };
        let medal = match idx {
            0 => "🥇".to_string(),
            1 => "🥈".to_string(),
            2 => "🥉".to_string(),
            n => format!("{}.", n + 1),
        };
        lines.push(format!(
            "{medal} {}  {sign}{ret:.2}%  ({amount_sign}¥{})",
            user_pnl.username,
            format_with_commas(amount, 2),
        ));
    }

    let _ = state.bot.reply_text(msg, &lines.join("\n"), None).await;
}

pub fn trade_lines(trades: &[Trade], limit: usize, tz: Tz) -> String {
    let start = trades.len().saturating_sub(limit);
    let mut lines = Vec::new();
    for trade in &trades[start..] {
        let side_label = if trade.side == Side::Buy { "买入" } else { "卖出" };
        let date = format_trade_time(&trade.trade_ts, tz);
        lines.push(format!(
            "#{}  {date}  {side_label} {} × {} @ {}  {}",
            trade.id,
            trade.symbol,
            format_qty(trade.qty),
            format_money(trade.price, &trade.currency),
            format_leverage(trade.leverage),
        ));
    }
    lines.join("\n")
}

pub async fn cmd_trades(state: &AppState, msg: &Message) {
    let Some(user) = &msg.from_user else {
        return;
    };
    let trades = state.db.get_trades(user.id, None, None).unwrap_or_default();
    if trades.is_empty() {
        let _ = state.bot.reply_text(msg, "你还没有任何交易记录", None).await;
        return;
    }
    let shown = trades.len().min(20);
    let body = trade_lines(&trades, 20, state.display_tz);
    let _ = state
        .bot
        .reply_text(
            msg,
            &format!("你的交易记录（最近{shown}条）：\n\n{body}\n\n使用 /del ID 删除某条记录"),
            None,
        )
        .await;
}

pub async fn cmd_delete(state: &AppState, msg: &Message, cmd: &CommandObject) {
    let Some(user) = &msg.from_user else {
        return;
    };
    let Some(args) = cmd.args.as_deref() else {
        let trades = state.db.get_trades(user.id, None, None).unwrap_or_default();
        if trades.is_empty() {
            let _ = state.bot.reply_text(msg, "你还没有任何交易记录", None).await;
            return;
        }
        let body = trade_lines(&trades, 20, state.display_tz);
        let _ = state
            .bot
            .reply_text(
                msg,
                &format!(
                    "用法：\n/del ID [ID ...] — 删除一条或多条记录\n\
                     /del SYMBOL Nx — 删除指定代码和杠杆的全部记录\n\n\
                     你的交易记录：\n{body}"
                ),
                None,
            )
            .await;
        return;
    };

    let parts: Vec<&str> = args.split_whitespace().collect();
    let id_tokens: Vec<&str> = parts
        .iter()
        .map(|p| p.strip_prefix('#').unwrap_or(p))
        .collect();

    if !id_tokens.is_empty()
        && id_tokens
            .iter()
            .all(|t| !t.is_empty() && t.bytes().all(|b| b.is_ascii_digit()))
    {
        let mut trade_ids = Vec::new();
        let mut seen = HashSet::new();
        for tok in id_tokens {
            let Ok(id) = tok.parse::<i64>() else {
                let _ = state.bot.reply_text(msg, "ID 必须为正整数", None).await;
                return;
            };
            if id <= 0 {
                let _ = state.bot.reply_text(msg, "ID 必须为正整数", None).await;
                return;
            }
            if seen.insert(id) {
                trade_ids.push(id);
            }
        }

        if trade_ids.len() == 1 {
            let trade_id = trade_ids[0];
            let Some(trade) = state.db.get_trade_by_id(trade_id).ok().flatten() else {
                let _ = state
                    .bot
                    .reply_text(msg, &format!("未找到记录 #{trade_id}"), None)
                    .await;
                return;
            };
            if trade.user_id != user.id {
                let _ = state
                    .bot
                    .reply_text(msg, "你只能删除自己的交易记录", None)
                    .await;
                return;
            }
            let deleted = state.db.delete_trade(trade_id, user.id).unwrap_or(false);
            let reply = if deleted {
                format!("已删除交易记录 #{trade_id}")
            } else {
                format!("未找到记录 #{trade_id}")
            };
            let _ = state.bot.reply_text(msg, &reply, None).await;
            return;
        }

        let deleted_ids = state
            .db
            .delete_trades(&trade_ids, user.id)
            .unwrap_or_default();
        let deleted_set: HashSet<i64> = deleted_ids.iter().copied().collect();
        let missing_ids: Vec<i64> = trade_ids
            .into_iter()
            .filter(|id| !deleted_set.contains(id))
            .collect();

        let mut lines = Vec::new();
        if !deleted_ids.is_empty() {
            let ids = deleted_ids
                .iter()
                .map(|id| format!("#{id}"))
                .collect::<Vec<_>>()
                .join(" ");
            lines.push(format!("已删除交易记录：{ids}"));
        }
        if !missing_ids.is_empty() {
            let ids = missing_ids
                .iter()
                .map(|id| format!("#{id}"))
                .collect::<Vec<_>>()
                .join(" ");
            lines.push(format!("未找到或无权删除：{ids}"));
        }
        let _ = state.bot.reply_text(msg, &lines.join("\n"), None).await;
        return;
    }

    if parts.len() != 2 {
        let _ = state
            .bot
            .reply_text(msg, "用法：/del ID [ID ...] 或 /del SYMBOL Nx", None)
            .await;
        return;
    }

    let (symbol, raw_lev) = (parts[0], parts[1]);
    let Ok(leverage) = parse_leverage(raw_lev) else {
        let _ = state
            .bot
            .reply_text(msg, "代码或杠杆格式无效；杠杆示例：1x、2.5x、5x", None)
            .await;
        return;
    };
    let deleted_count = match state
        .db
        .delete_trades_by_symbol_leverage(user.id, symbol, leverage, None)
    {
        Ok(c) => c,
        Err(_) => {
            let _ = state
                .bot
                .reply_text(msg, "代码或杠杆格式无效；杠杆示例：1x、2.5x、5x", None)
                .await;
            return;
        }
    };

    let reply = if deleted_count > 0 {
        format!(
            "已删除 {} [{}] 的全部 {deleted_count} 条交易记录",
            symbol.to_uppercase(),
            format_leverage(leverage),
        )
    } else {
        format!(
            "未找到 {} [{}] 的交易记录",
            symbol.to_uppercase(),
            format_leverage(leverage),
        )
    };
    let _ = state.bot.reply_text(msg, &reply, None).await;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn utc_converts_to_beijing_time() {
        let tz: Tz = "Asia/Shanghai".parse().unwrap();
        assert_eq!(
            format_trade_time("2026-09-02T20:00:00+00:00", tz),
            "2026-09-03 04:00:00"
        );
    }
}
