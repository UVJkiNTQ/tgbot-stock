use std::env;
use std::path::PathBuf;

pub const QUOTE_CACHE_TTL_SECS: f64 = 3.0;

#[derive(Debug, Clone)]
pub struct Config {
    pub bot_token: String,
    pub api_base: Option<String>,
    pub db_path: PathBuf,
    pub display_timezone: String,
    pub chart_font: Option<String>,
}

impl Config {
    pub fn from_env() -> Self {
        let bot_token = env::var("TG_BOT_TOKEN").unwrap_or_default();
        let api_base = env::var("TG_API_BASE")
            .ok()
            .filter(|s| !s.trim().is_empty());
        let db_path = env::var("BOT_DB_PATH")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("data").join("trades.db"));
        let display_timezone =
            env::var("BOT_TIMEZONE").unwrap_or_else(|_| "Asia/Shanghai".to_string());
        let chart_font = env::var("BOT_CHART_FONT")
            .ok()
            .filter(|s| !s.trim().is_empty());
        Self {
            bot_token,
            api_base,
            db_path,
            display_timezone,
            chart_font,
        }
    }
}
