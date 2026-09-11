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

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Europe/Prague")  # на Windows требует пакет tzdata


@dataclass(frozen=True, slots=True)
class Config:
    db_path: Path
    debug_dir: Path
    headless: bool


def load() -> Config:
    load_dotenv(ROOT / ".env")
    return Config(
        db_path=Path(os.getenv("DB_PATH") or ROOT / "data" / "zlinbot.db"),
        debug_dir=Path(os.getenv("DEBUG_DIR") or ROOT / "debug"),
        headless=os.getenv("HEADLESS", "1") != "0",
    )
