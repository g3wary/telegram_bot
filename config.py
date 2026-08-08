"""Конфиг — все секреты только из .env, в коде их не будет."""
import os
from dotenv import load_dotenv

load_dotenv()


def _int(name: str) -> int:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"❌ {name} не задан в .env")
    return int(v)


def _str(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"❌ {name} не задан в .env")
    return v


BOT_TOKEN = _str("BOT_TOKEN")
ADMIN_ID = _int("ADMIN_ID")
SBER_CARD = _str("SBER_CARD")
SBER_RECIPIENT = _str("SBER_RECIPIENT")
DB_URL = _str("DB_URL")
