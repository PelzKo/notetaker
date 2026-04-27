import os

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])  # Your personal chat ID

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", 3306))
DB_NAME = os.environ.get("DB_NAME", "todobot")
DB_USER = os.environ.get("DB_USER", "todobot")
DB_PASSWORD = os.environ["DB_PASSWORD"]

SUMMARY_HOUR = int(os.environ.get("SUMMARY_HOUR", 8))   # 08:00 local time
SUMMARY_MINUTE = int(os.environ.get("SUMMARY_MINUTE", 0))

NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

CATEGORIES = ["Work", "Home", "MCM", "YFU", "Personal", "Other", "Unknown"]
