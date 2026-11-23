use crate::handlers::common::{
    format_leverage, format_money, format_qty, needs_deviation_reconfirm, verify_confirm_owner,
};
use crate::handlers::AppState;
use crate::models::{ModelError, Side, Trade};
use crate::telegram::{CallbackQuery, PendingTrade};

fn confirmed_trade_lines(trades: &[Trade]) -> String {
    trades
        .iter()
        .map(|trade| {
            let side_label = if trade.side == Side::Buy { "买入" } else { "卖出" };
            format!(
                "{} {side_label} {} 股 @ {} (ID: {})",
                format_leverage(trade.leverage),
                format_qty(trade.qty),
                format_money(trade.price, &trade.currency),
                trade.id,
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn insert_confirmed_trades(
    state: &AppState,
    data: &PendingTrade,
    side: Option<Side>,
) -> Result<Vec<Trade>, ModelError> {
    if data.close_all {
        return state.db.insert_close_trades(
            data.user_id,
            &data.username,
            &data.symbol,
            side,
            data.price,
            &data.currency,
            data.rate,
            data.requested_leverage,
            Some(&data.market),
        );
    }
    let Some(side) = side else {
        return Ok(Vec::new());
    };
    let effective_leverage = data.effective_leverage.or(data.requested_leverage);
    let trade = state.db.insert_trade(
        data.user_id,
        &data.username,
        &data.symbol,
        side,
        data.price,
        data.qty,
        &data.currency,
        data.rate,
        effective_leverage,
        Some(&data.market),
    )?;
    Ok(vec![trade])
}

async fn edit_callback_msg(state: &AppState, callback: &CallbackQuery, text: &str) {
    if let Some(msg) = &callback.message {
        let _ = state
            .bot
            .edit_message_text(msg.chat.id, msg.message_id, text, None)
            .await;
    }
}

pub async fn handle_callback(state: &AppState, callback: &CallbackQuery) {
    let data_str = callback.data.as_deref().unwrap_or("");
    let kind = if data_str.starts_with("buy_ok") {
        "buy"
    } else if data_str.starts_with("sell_ok") {
        "sell"
    } else if data_str.starts_with("close_ok") {
        "close"
    } else if data_str.starts_with("cancel") {
        "cancel"
    } else {
        return;
    };

    let user_lock = state.confirmation_lock(callback.from_user.id);
    let _guard = user_lock.lock().await;
    let chat_id = callback.message.as_ref().map(|m| m.chat.id).unwrap_or(0);

    if kind == "cancel" {
        if !verify_confirm_owner(&state.bot, callback, None, false).await {
            return;
        }
        state.fsm.clear(chat_id, callback.from_user.id);
        edit_callback_msg(state, callback, "已取消").await;
        let _ = state.bot.answer_callback_query(&callback.id, None, false).await;
        return;
    }

    let pending = state.fsm.get(chat_id, callback.from_user.id);
    if !verify_confirm_owner(&state.bot, callback, pending.as_ref(), true).await {
        return;
    }
    let data = pending.unwrap();

    let (label, side) = match kind {
        "buy" => ("买入", Some(Side::Buy)),
        "sell" => ("卖出", Some(Side::Sell)),
        _ => ("平仓", None),
    };

    if needs_deviation_reconfirm(&state.bot, &state.fsm, callback, &data, kind, label).await {
        return;
    }
    state.fsm.clear(chat_id, callback.from_user.id);

    let trades = match insert_confirmed_trades(state, &data, side) {
        Ok(t) => t,
        Err(ModelError::LeverageMismatch(exc)) => {
            edit_callback_msg(state, callback, &format!("仓位已变化，操作无效：{exc}")).await;
            let _ = state.bot.answer_callback_query(&callback.id, None, false).await;
            return;
        }
        Err(exc) => {
            edit_callback_msg(state, callback, &format!("操作失败：{exc}")).await;
            let _ = state.bot.answer_callback_query(&callback.id, None, false).await;
            return;
        }
    };

    if trades.is_empty() {
        let empty_text = match kind {
            "buy" => format!("仓位已变化，无法执行 ALL：{} 当前已不是空仓", data.symbol),
            "sell" => format!("仓位已变化，无法执行 ALL：{} 当前已不是多仓", data.symbol),
            _ => format!("{} 当前仓位已经为 0，无需平仓", data.symbol),
        };
        edit_callback_msg(state, callback, &empty_text).await;
        let _ = state.bot.answer_callback_query(&callback.id, None, false).await;
        return;
    }

    let trade = &trades[0];
    let quote = state
        .quotes
        .get_quote(&trade.symbol, Some(&trade.market))
        .await;
    let name = quote
        .filter(|q| q.price > 0.0)
        .map(|q| q.name)
        .unwrap_or_else(|| trade.symbol.clone());

    let text = match kind {
        "close" => format!(
            "已一次平仓：{name} ({})\n{}",
            trade.symbol,
            confirmed_trade_lines(&trades)
        ),
        "buy" if data.close_all => format!(
            "已一次平空：{name} ({})\n{}",
            trade.symbol,
            confirmed_trade_lines(&trades)
        ),
        "sell" if data.close_all => format!(
            "已一次平多：{name} ({})\n{}",
            trade.symbol,
            confirmed_trade_lines(&trades)
        ),
        _ => format!(
            "已记录{label}：{name} ({}) × {} @ {}  杠杆 {} (ID: {})",
            trade.symbol,
            format_qty(trade.qty),
            format_money(trade.price, &trade.currency),
            format_leverage(trade.leverage),
            trade.id,
        ),
    };

    edit_callback_msg(state, callback, &text).await;
    let _ = state.bot.answer_callback_query(&callback.id, None, false).await;
}
