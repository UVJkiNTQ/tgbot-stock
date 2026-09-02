import os

BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "trades.db")
QUOTE_CACHE_TTL = 3.0  # 行情缓存秒数
DISPLAY_TIMEZONE = os.getenv("BOT_TIMEZONE", "Asia/Shanghai")
