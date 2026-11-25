pub mod callbacks;
pub mod common;
pub mod reports;
pub mod system;
pub mod trading;

use crate::chartstyle::ChartFont;
use crate::instrument_search::SearchService;
use crate::intraday::IntradayService;
use crate::kline::KlineService;
use crate::maintenance::MaintenanceGate;
use crate::models::{Db, Side};
use crate::pnl::PnlService;
use crate::quotes::QuoteService;
use crate::telegram::{parse_command, Bot, FsmStorage, Update};
use chrono_tz::Tz;
use std::collections::HashMap;
use std::sync::{Arc, Mutex, Weak};

pub struct AppState {
    pub bot: Bot,
    pub bot_username: Mutex<Option<String>>,
    pub db: Db,
    pub quotes: QuoteService,
    pub intraday: IntradayService,
    pub kline: KlineService,
    pub search: SearchService,
    pub pnl: PnlService,
    pub font: ChartFont,
    pub fsm: FsmStorage,
    pub maintenance: MaintenanceGate,
    pub display_tz: Tz,
    confirm_locks: Mutex<HashMap<i64, Weak<tokio::sync::Mutex<()>>>>,
}

impl AppState {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        bot: Bot,
        db: Db,
        quotes: QuoteService,
        intraday: IntradayService,
        kline: KlineService,
        search: SearchService,
        font: ChartFont,
        display_tz: Tz,
    ) -> Self {
        Self {
            bot,
            bot_username: Mutex::new(None),
            db,
            quotes,
            intraday,
            kline,
            search,
            pnl: PnlService::new(),
            font,
            fsm: FsmStorage::new(),
            maintenance: MaintenanceGate::new(),
            display_tz,
            confirm_locks: Mutex::new(HashMap::new()),
        }
    }

    pub fn set_bot_username(&self, username: Option<String>) {
        *self.bot_username.lock().unwrap() = username;
    }

    pub fn confirmation_lock(&self, user_id: i64) -> Arc<tokio::sync::Mutex<()>> {
        let mut map = self.confirm_locks.lock().unwrap();
        if let Some(existing) = map.get(&user_id).and_then(Weak::upgrade) {
            return existing;
        }
        let lock = Arc::new(tokio::sync::Mutex::new(()));
        map.insert(user_id, Arc::downgrade(&lock));
        if map.len() > 256 {
            map.retain(|_, v| v.strong_count() > 0);
        }
        lock
    }
}

pub async fn dispatch_update(state: Arc<AppState>, update: Update) {
    if let Some(msg) = update.message {
        let bot_username = state.bot_username.lock().unwrap().clone();
        let Some(cmd) = parse_command(msg.text.as_deref(), bot_username.as_deref()) else {
            return;
        };

        if state.maintenance.is_active()
            && !MaintenanceGate::is_update_command(msg.text.as_deref())
        {
            let _ = state
                .bot
                .reply_text(&msg, "数据库维护中，请暂时不要操作，完成后会通知", None)
                .await;
            return;
        }

        match cmd.command.as_str() {
            "start" | "help" => system::cmd_help(&state, &msg).await,
            "update" => system::cmd_update(&state, &msg).await,
            "quote" => trading::cmd_quote(&state, &msg, &cmd).await,
            "find" => trading::cmd_find(&state, &msg, &cmd).await,
            "buy" => trading::cmd_trade(&state, &msg, &cmd, Side::Buy, false).await,
            "sell" => trading::cmd_trade(&state, &msg, &cmd, Side::Sell, false).await,
            "buyn" => trading::cmd_trade(&state, &msg, &cmd, Side::Buy, true).await,
            "selln" => trading::cmd_trade(&state, &msg, &cmd, Side::Sell, true).await,
            "buya" => trading::cmd_amount_trade(&state, &msg, &cmd, Side::Buy, false).await,
            "sella" => trading::cmd_amount_trade(&state, &msg, &cmd, Side::Sell, false).await,
            "buyna" => trading::cmd_amount_trade(&state, &msg, &cmd, Side::Buy, true).await,
            "sellna" => trading::cmd_amount_trade(&state, &msg, &cmd, Side::Sell, true).await,
            "close" => trading::cmd_close(&state, &msg, &cmd).await,
            "position" => reports::cmd_position(&state, &msg).await,
            "pnl" => reports::cmd_pnl(&state, &msg).await,
            "lb" | "leaderboard" => reports::cmd_leaderboard(&state, &msg, &cmd).await,
            "today" => reports::cmd_today(&state, &msg).await,
            "trades" => reports::cmd_trades(&state, &msg).await,
            "del" => reports::cmd_delete(&state, &msg, &cmd).await,
            _ => {}
        }
    } else if let Some(cb) = update.callback_query {
        if state.maintenance.is_active() {
            let _ = state
                .bot
                .answer_callback_query(&cb.id, Some("数据库维护中，请等待完成通知"), true)
                .await;
            return;
        }
        callbacks::handle_callback(&state, &cb).await;
    }
}
