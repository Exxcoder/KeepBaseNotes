import os
import re
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "8624420019:AAEXcEVnp9ib2-oZDfDMnlK-QNppjCUCa6c").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "kukbar_bot").strip().lstrip("@")

raw_webapp_url = os.getenv("BASE_WEBAPP_URL", os.getenv("WEBAPP_BASE_URL", "")).strip()
match = re.search(r"https?://[^\s\)\]]+", raw_webapp_url)
if match:
    BASE_WEBAPP_URL = match.group(0).rstrip("/")
else:
    BASE_WEBAPP_URL = raw_webapp_url.rstrip("/")

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

def get_db_path() -> str:
    custom_path = os.getenv("DB_PATH")
    if custom_path:
        return custom_path
    if os.path.exists("/data") and os.path.isdir("/data"):
        return "/data/bot.db"
    return "./bot.db"

DB_PATH = get_db_path()
DEFAULT_TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
MORNING_HOUR = int(os.getenv("MORNING_HOUR", "6"))
EVENING_HOUR = int(os.getenv("EVENING_HOUR", "21"))
