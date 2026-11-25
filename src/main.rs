mod cache;
mod chartstyle;
mod config;
mod handlers;
mod http_client;
mod instrument;
mod instrument_search;
mod intraday;
mod kline;
mod maintenance;
mod models;
mod pnl;
mod quotes;
mod telegram;

use crate::chartstyle::ChartFont;
use crate::config::Config;
use crate::handlers::{dispatch_update, AppState};
use crate::http_client::build_http_client;
use crate::instrument_search::SearchService;
use crate::intraday::IntradayService;
use crate::kline::KlineService;
use crate::models::Db;
use crate::quotes::QuoteService;
use crate::telegram::{Bot, BotCommand};
use chrono::Local;
use chrono_tz::Tz;
use std::sync::Arc;
use std::time::Duration;

fn log_info(msg: &str) {
    eprintln!("{} [INFO] tgbot-stock: {msg}", Local::now().format("%Y-%m-%d %H:%M:%S"));
}

fn log_warn(msg: &str) {
    eprintln!("{} [WARN] tgbot-stock: {msg}", Local::now().format("%Y-%m-%d %H:%M:%S"));
}

fn bot_commands() -> Vec<BotCommand> {
    vec![
        BotCommand::new("quote", "查实时行情、分时和日K：/quote SYMBOL"),
        BotCommand::new("find", "按名称搜索股票代码：/find wacom"),
        BotCommand::new("buy", "买入：/buy SYMBOL PRICE QTY [Nx]"),
        BotCommand::new("buya", "按人民币预算买入：/buya SYMBOL PRICE AMOUNT"),
        BotCommand::new("sell", "卖出：/sell SYMBOL PRICE QTY [Nx]"),
        BotCommand::new("sella", "按人民币预算卖出：/sella SYMBOL PRICE AMOUNT"),
        BotCommand::new("buyn", "现价买入：/buyn SYMBOL QTY [Nx]"),
        BotCommand::new("selln", "现价卖出：/selln SYMBOL QTY [Nx]"),
        BotCommand::new("buyna", "现价按预算买入：/buyna SYMBOL AMOUNT [Nx] [Ns]"),
        BotCommand::new("sellna", "现价按预算卖出：/sellna SYMBOL AMOUNT [Nx] [Ns]"),
        BotCommand::new("close", "自动平仓：/close SYMBOL PRICE"),
        BotCommand::new("position", "查看我的持仓和浮盈"),
        BotCommand::new("pnl", "查看历史已实现收益和浮盈"),
        BotCommand::new("lb", "收益排行：/lb u 或 /lb r"),
        BotCommand::new("today", "今日市值变动比例排行"),
        BotCommand::new("trades", "查看我的交易记录（含ID）"),
        BotCommand::new("del", "按ID或代码+杠杆删除记录"),
        BotCommand::new("update", "规整旧数据库（可重复安全执行）"),
        BotCommand::new("help", "查看帮助"),
    ]
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let cfg = Config::from_env();
    if cfg.bot_token.trim().is_empty() {
        eprintln!("{} [CRITICAL] tgbot-stock: TG_BOT_TOKEN 环境变量未设置", Local::now().format("%Y-%m-%d %H:%M:%S"));
        std::process::exit(1);
    }

    let db = Db::open(&cfg.db_path).expect("failed to open SQLite database");
    db.init().expect("failed to initialize SQLite schema");
    log_info("数据库初始化完成");

    let display_tz: Tz = cfg
        .display_timezone
        .parse()
        .unwrap_or(chrono_tz::UTC);
    let font = ChartFont::load(cfg.chart_font.as_deref());
    let http = build_http_client();
    let bot = Bot::new(&cfg.bot_token, cfg.api_base.as_deref(), http.clone());

    let state = Arc::new(AppState::new(
        bot.clone(),
        db,
        QuoteService::new(http.clone()),
        IntradayService::new(http.clone()),
        KlineService::new(http.clone()),
        SearchService::new(http),
        font,
        display_tz,
    ));

    if let Err(e) = bot.set_my_commands(&bot_commands()).await {
        log_warn(&format!("注册命令菜单失败: {e}"));
    } else {
        log_info("命令菜单已注册");
    }

    match bot.get_me().await {
        Ok(me) => {
            log_info(&format!(
                "已连接 Telegram Bot: @{}",
                me.username.as_deref().unwrap_or("unknown")
            ));
            state.set_bot_username(me.username);
        }
        Err(e) => {
            log_warn(&format!("getMe 失败（继续轮询）: {e}"));
        }
    }

    log_info("Bot 启动中...");
    let mut offset: Option<i64> = None;
    let mut backoff_secs = 1u64;

    loop {
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {
                log_info("收到退出信号，正在停止...");
                break;
            }
            res = bot.get_updates(offset, 30) => {
                match res {
                    Ok(updates) => {
                        backoff_secs = 1;
                        for update in updates {
                            offset = Some(update.update_id + 1);
                            let st = Arc::clone(&state);
                            tokio::spawn(async move {
                                dispatch_update(st, update).await;
                            });
                        }
                    }
                    Err(e) => {
                        log_warn(&format!("getUpdates 失败，{backoff_secs} 秒后重试: {e}"));
                        tokio::time::sleep(Duration::from_secs(backoff_secs)).await;
                        backoff_secs = (backoff_secs * 2).min(5);
                    }
                }
            }
        }
    }
}
