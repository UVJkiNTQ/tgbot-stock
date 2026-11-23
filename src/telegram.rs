use crate::cache::TtlCache;
use reqwest::{multipart, Client};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::sync::Mutex;
use std::time::Duration;

const API_BASE: &str = "https://api.telegram.org";
const FSM_TTL_SECS: u64 = 3600;
const FSM_MAX_ENTRIES: usize = 512;

#[derive(Debug, Clone)]
pub struct TelegramApiError {
    pub message: String,
}

impl std::fmt::Display for TelegramApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct BotCommand {
    pub command: String,
    pub description: String,
}

impl BotCommand {
    pub fn new(command: &str, description: &str) -> Self {
        Self {
            command: command.to_string(),
            description: description.to_string(),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct InlineKeyboardButton {
    pub text: String,
    pub callback_data: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct InlineKeyboardMarkup {
    pub inline_keyboard: Vec<Vec<InlineKeyboardButton>>,
}

#[derive(Debug, Clone)]
pub struct BufferedInputFile {
    pub data: Vec<u8>,
    pub filename: String,
}

#[derive(Debug, Clone)]
pub struct InputMediaPhoto {
    pub media: BufferedInputFile,
    pub caption: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct User {
    pub id: i64,
    #[serde(default)]
    pub first_name: String,
    #[serde(default)]
    pub last_name: Option<String>,
    #[serde(default)]
    pub username: Option<String>,
}

impl User {
    pub fn full_name(&self) -> String {
        match &self.last_name {
            Some(last) if !last.is_empty() => format!("{} {last}", self.first_name),
            _ => self.first_name.clone(),
        }
    }

    pub fn display_name(&self) -> String {
        self.username
            .clone()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| self.full_name())
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct Chat {
    pub id: i64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Message {
    pub message_id: i64,
    pub chat: Chat,
    #[serde(rename = "from", default)]
    pub from_user: Option<User>,
    #[serde(default)]
    pub text: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct CallbackQuery {
    pub id: String,
    #[serde(rename = "from")]
    pub from_user: User,
    #[serde(default)]
    pub data: Option<String>,
    #[serde(default)]
    pub message: Option<Message>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Update {
    pub update_id: i64,
    #[serde(default)]
    pub message: Option<Message>,
    #[serde(default)]
    pub callback_query: Option<CallbackQuery>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandObject {
    pub command: String,
    pub args: Option<String>,
}

pub fn parse_command(text: Option<&str>, bot_username: Option<&str>) -> Option<CommandObject> {
    let text = text?;
    if !text.starts_with('/') {
        return None;
    }
    let mut split = text.splitn(2, |c: char| c.is_whitespace());
    let head = &split.next()?[1..];
    if head.is_empty() {
        return None;
    }
    let (cmd_name, mention) = match head.split_once('@') {
        Some((c, m)) => (c, Some(m)),
        None => (head, None),
    };
    if cmd_name.is_empty() {
        return None;
    }
    if let (Some(m), Some(bot_u)) = (mention, bot_username) {
        if !m.eq_ignore_ascii_case(bot_u) {
            return None;
        }
    }
    let args = split
        .next()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string);
    Some(CommandObject {
        command: cmd_name.to_lowercase(),
        args,
    })
}

#[derive(Debug, Clone)]
pub struct PendingTrade {
    pub user_id: i64,
    pub username: String,
    pub symbol: String,
    pub market: String,
    pub price: f64,
    pub qty: i64,
    pub currency: String,
    pub rate: f64,
    pub market_price: f64,
    pub deviation_pct: f64,
    pub deviation_ack: bool,
    pub close_all: bool,
    pub requested_leverage: Option<f64>,
    pub effective_leverage: Option<f64>,
    pub confirmation_message_id: Option<i64>,
}

pub struct FsmStorage {
    data: Mutex<TtlCache<(i64, i64), PendingTrade>>,
}

impl Default for FsmStorage {
    fn default() -> Self {
        Self::new()
    }
}

impl FsmStorage {
    pub fn new() -> Self {
        Self {
            data: Mutex::new(TtlCache::new(
                Duration::from_secs(FSM_TTL_SECS),
                FSM_MAX_ENTRIES,
            )),
        }
    }

    pub fn get(&self, chat_id: i64, user_id: i64) -> Option<PendingTrade> {
        self.data.lock().unwrap().get(&(chat_id, user_id))
    }

    pub fn set(&self, chat_id: i64, user_id: i64, pending: PendingTrade) {
        self.data.lock().unwrap().insert((chat_id, user_id), pending);
    }

    pub fn set_confirmation_message_id(&self, chat_id: i64, user_id: i64, msg_id: Option<i64>) {
        let mut cache = self.data.lock().unwrap();
        if let Some(mut existing) = cache.get(&(chat_id, user_id)) {
            existing.confirmation_message_id = msg_id;
            cache.insert((chat_id, user_id), existing);
        }
    }

    pub fn clear(&self, chat_id: i64, user_id: i64) {
        // Overwrite by removing via a fresh get/drop isn't needed if we expose remove;
        // let's keep a dedicated remove method on FsmStorage via clear-by-filter or TtlCache.
        let mut cache = self.data.lock().unwrap();
        // Insert-free removal using a helper we'll keep simple:
        let _ = cache.get(&(chat_id, user_id));
        // Actually let's add remove() to TtlCache so clear is O(1)!
        cache.remove(&(chat_id, user_id));
    }
}

#[derive(Clone)]
pub struct Bot {
    http: Client,
    base_url: String,
    parse_mode: Option<&'static str>,
}

impl Bot {
    pub fn new(token: &str, api_base: Option<&str>, http: Client) -> Self {
        let base = api_base.unwrap_or(API_BASE).trim_end_matches('/');
        Self {
            http,
            base_url: format!("{base}/bot{token}"),
            parse_mode: Some("HTML"),
        }
    }

    async fn post_json(&self, method: &str, body: Value, timeout_secs: u64) -> Result<Value, TelegramApiError> {
        let url = format!("{}/{method}", self.base_url);
        let resp = self
            .http
            .post(&url)
            .json(&body)
            .timeout(Duration::from_secs(timeout_secs))
            .send()
            .await
            .map_err(|e| TelegramApiError {
                message: format!("{method} HTTP error: {}", e.without_url()),
            })?;

        let payload: Value = resp.json().await.map_err(|_| TelegramApiError {
            message: format!("{method} returned invalid JSON"),
        })?;

        if payload.get("ok").and_then(Value::as_bool) != Some(true) {
            return Err(TelegramApiError {
                message: format!(
                    "{method} failed: {}",
                    payload
                        .get("description")
                        .and_then(Value::as_str)
                        .unwrap_or("unknown Telegram API error")
                ),
            });
        }
        Ok(payload.get("result").cloned().unwrap_or(Value::Null))
    }

    async fn post_multipart(
        &self,
        method: &str,
        form: multipart::Form,
        timeout_secs: u64,
    ) -> Result<Value, TelegramApiError> {
        let url = format!("{}/{method}", self.base_url);
        let resp = self
            .http
            .post(&url)
            .multipart(form)
            .timeout(Duration::from_secs(timeout_secs))
            .send()
            .await
            .map_err(|e| TelegramApiError {
                message: format!("{method} HTTP error: {}", e.without_url()),
            })?;

        let payload: Value = resp.json().await.map_err(|_| TelegramApiError {
            message: format!("{method} returned invalid JSON"),
        })?;

        if payload.get("ok").and_then(Value::as_bool) != Some(true) {
            return Err(TelegramApiError {
                message: format!(
                    "{method} failed: {}",
                    payload
                        .get("description")
                        .and_then(Value::as_str)
                        .unwrap_or("unknown Telegram API error")
                ),
            });
        }
        Ok(payload.get("result").cloned().unwrap_or(Value::Null))
    }

    pub async fn get_me(&self) -> Result<User, TelegramApiError> {
        let res = self.post_json("getMe", json!({}), 20).await?;
        serde_json::from_value(res).map_err(|e| TelegramApiError {
            message: format!("getMe decode error: {e}"),
        })
    }

    pub async fn set_my_commands(&self, commands: &[BotCommand]) -> Result<bool, TelegramApiError> {
        let res = self
            .post_json("setMyCommands", json!({ "commands": commands }), 20)
            .await?;
        Ok(res.as_bool().unwrap_or(false))
    }

    pub async fn get_updates(
        &self,
        offset: Option<i64>,
        timeout: u64,
    ) -> Result<Vec<Update>, TelegramApiError> {
        let mut body = json!({
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"]
        });
        if let Some(off) = offset {
            body["offset"] = json!(off);
        }
        let res = self.post_json("getUpdates", body, timeout + 15).await?;
        serde_json::from_value(res).map_err(|e| TelegramApiError {
            message: format!("getUpdates decode error: {e}"),
        })
    }

    pub async fn reply_text(
        &self,
        msg: &Message,
        text: &str,
        reply_markup: Option<&InlineKeyboardMarkup>,
    ) -> Result<Message, TelegramApiError> {
        self.send_message(msg.chat.id, text, Some(msg.message_id), reply_markup)
            .await
    }

    pub async fn send_message(
        &self,
        chat_id: i64,
        text: &str,
        reply_to_message_id: Option<i64>,
        reply_markup: Option<&InlineKeyboardMarkup>,
    ) -> Result<Message, TelegramApiError> {
        let mut body = json!({
            "chat_id": chat_id,
            "text": text,
        });
        if let Some(pm) = self.parse_mode {
            body["parse_mode"] = json!(pm);
        }
        if let Some(rid) = reply_to_message_id {
            body["reply_parameters"] = json!({
                "message_id": rid,
                "allow_sending_without_reply": true,
            });
        }
        if let Some(kb) = reply_markup {
            body["reply_markup"] = serde_json::to_value(kb).unwrap_or(Value::Null);
        }
        let res = self.post_json("sendMessage", body, 20).await?;
        serde_json::from_value(res).map_err(|e| TelegramApiError {
            message: format!("sendMessage decode error: {e}"),
        })
    }

    pub async fn reply_photo(
        &self,
        msg: &Message,
        photo: BufferedInputFile,
        caption: Option<&str>,
    ) -> Result<Message, TelegramApiError> {
        let mut form = multipart::Form::new().text("chat_id", msg.chat.id.to_string());
        if let Some(cap) = caption {
            form = form.text("caption", cap.to_string());
        }
        if let Some(pm) = self.parse_mode {
            form = form.text("parse_mode", pm.to_string());
        }
        form = form.text(
            "reply_parameters",
            json!({
                "message_id": msg.message_id,
                "allow_sending_without_reply": true,
            })
            .to_string(),
        );
        let part = multipart::Part::bytes(photo.data)
            .file_name(photo.filename)
            .mime_str("image/png")
            .unwrap();
        form = form.part("photo", part);

        let res = self.post_multipart("sendPhoto", form, 30).await?;
        serde_json::from_value(res).map_err(|e| TelegramApiError {
            message: format!("sendPhoto decode error: {e}"),
        })
    }

    pub async fn reply_media_group(
        &self,
        msg: &Message,
        media: Vec<InputMediaPhoto>,
    ) -> Result<Vec<Message>, TelegramApiError> {
        let mut spec = Vec::with_capacity(media.len());
        let mut form = multipart::Form::new().text("chat_id", msg.chat.id.to_string());

        for (index, item) in media.into_iter().enumerate() {
            let attach = format!("file{index}");
            let mut entry = json!({
                "type": "photo",
                "media": format!("attach://{attach}"),
            });
            if let Some(cap) = item.caption {
                entry["caption"] = json!(cap);
                if let Some(pm) = self.parse_mode {
                    entry["parse_mode"] = json!(pm);
                }
            }
            spec.push(entry);
            let part = multipart::Part::bytes(item.media.data)
                .file_name(item.media.filename)
                .mime_str("image/png")
                .unwrap();
            form = form.part(attach, part);
        }

        form = form
            .text("media", Value::Array(spec).to_string())
            .text(
                "reply_parameters",
                json!({
                    "message_id": msg.message_id,
                    "allow_sending_without_reply": true,
                })
                .to_string(),
            );

        let res = self.post_multipart("sendMediaGroup", form, 30).await?;
        serde_json::from_value(res).map_err(|e| TelegramApiError {
            message: format!("sendMediaGroup decode error: {e}"),
        })
    }

    pub async fn edit_message_text(
        &self,
        chat_id: i64,
        message_id: i64,
        text: &str,
        reply_markup: Option<&InlineKeyboardMarkup>,
    ) -> Result<(), TelegramApiError> {
        let mut body = json!({
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        });
        if let Some(pm) = self.parse_mode {
            body["parse_mode"] = json!(pm);
        }
        if let Some(kb) = reply_markup {
            body["reply_markup"] = serde_json::to_value(kb).unwrap_or(Value::Null);
        }
        self.post_json("editMessageText", body, 20).await?;
        Ok(())
    }

    pub async fn answer_callback_query(
        &self,
        callback_query_id: &str,
        text: Option<&str>,
        show_alert: bool,
    ) -> Result<(), TelegramApiError> {
        let mut body = json!({ "callback_query_id": callback_query_id });
        if let Some(t) = text {
            body["text"] = json!(t);
        }
        if show_alert {
            body["show_alert"] = json!(true);
        }
        self.post_json("answerCallbackQuery", body, 20).await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn command_parsing() {
        assert_eq!(
            parse_command(Some("/buy 600000 10.5 100"), None),
            Some(CommandObject {
                command: "buy".into(),
                args: Some("600000 10.5 100".into()),
            })
        );
        assert_eq!(
            parse_command(Some("/position@StockBot"), Some("stockbot")),
            Some(CommandObject {
                command: "position".into(),
                args: None,
            })
        );
        assert_eq!(
            parse_command(Some("/position@OtherBot"), Some("stockbot")),
            None
        );
        assert_eq!(parse_command(Some("hello"), None), None);
    }
}
