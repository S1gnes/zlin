"""
Настройки процесса из .env. Здесь только то, что задаётся один раз при установке
(пути, позже — токены). Всё, что меняется в работе, живёт в таблице settings и правится из бота.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from . import gemini

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Europe/Prague")  # на Windows требует пакет tzdata


@dataclass(frozen=True, slots=True)
class Config:
    db_path: Path
    debug_dir: Path
    headless: bool
    gemini_key: str | None       # ключ живёт только в .env и в git не попадает
    gemini_model: str            # значение по умолчанию; рабочее — в таблице settings
    bot_token: str | None
    admin_id: int | None         # единственный, кому бот отвечает
    channel_id: str | None       # @имя или -100...


def load() -> Config:
    load_dotenv(ROOT / ".env")
    return Config(
        db_path=Path(os.getenv("DB_PATH") or ROOT / "data" / "zlinbot.db"),
        debug_dir=Path(os.getenv("DEBUG_DIR") or ROOT / "debug"),
        headless=os.getenv("HEADLESS", "1") != "0",
        gemini_key=(os.getenv("GEMINI_API_KEY") or "").strip() or None,
        gemini_model=(os.getenv("GEMINI_MODEL") or "").strip() or gemini.DEFAULT_MODEL,
        bot_token=(os.getenv("BOT_TOKEN") or "").strip() or None,
        admin_id=_int(os.getenv("ADMIN_ID")),
        channel_id=(os.getenv("CHANNEL_ID") or "").strip() or None,
    )


def _int(raw: str | None) -> int | None:
    try:
        return int((raw or "").strip())
    except ValueError:
        return None
