"""
Этап 4: запуск бота с черновиками и кнопками.

    .venv\\Scripts\\python scripts\\bot.py

Нужны в .env: BOT_TOKEN, ADMIN_ID, CHANNEL_ID (и GEMINI_API_KEY, чтобы работала «Переписать»).
Сбор и разбор пока запускаются отдельно (collect.py); всё вместе свяжется на этапе 7.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zlinbot import config  # noqa: E402
from zlinbot.bot.app import build_dispatcher, check_channel, make_bot, startup_report  # noqa: E402
from zlinbot.bot.handlers import send_draft  # noqa: E402
from zlinbot.bot.publisher import Publisher  # noqa: E402
from zlinbot.db import Database  # noqa: E402
from zlinbot.gemini import Gemini  # noqa: E402
from zlinbot.media import MediaStore  # noqa: E402
from zlinbot.pipeline import current_model  # noqa: E402

log = logging.getLogger("zlinbot.run")


async def main_async() -> None:
    cfg = config.load()
    missing = [name for name, value in (("BOT_TOKEN", cfg.bot_token), ("ADMIN_ID", cfg.admin_id),
                                        ("CHANNEL_ID", cfg.channel_id)) if not value]
    if missing:
        print("Не хватает в .env: " + ", ".join(missing) + ". Образец — .env.example.")
        return

    async with Database(cfg.db_path) as db, contextlib.AsyncExitStack() as stack:
        bot = make_bot(cfg.bot_token)
        stack.push_async_callback(bot.session.close)
        gemini = None
        if cfg.gemini_key:
            gemini = await stack.enter_async_context(
                Gemini(cfg.gemini_key, model=await current_model(db, cfg.gemini_model)))
        else:
            log.warning("нет GEMINI_API_KEY — кнопка «Переписать» работать не будет")

        store = await stack.enter_async_context(MediaStore(cfg.media_dir))
        info = await check_channel(bot, cfg.channel_id)
        publisher = Publisher(bot, db, cfg.channel_id, channel_username=info.username,
                              discussion_chat_id=info.discussion_chat_id, store=store)
        dp = build_dispatcher(db, publisher, gemini, admin_id=cfg.admin_id,
                              discussion_chat_id=info.discussion_chat_id, store=store)
        await bot.send_message(cfg.admin_id, startup_report(info))

        pending = await db.drafts(status="pending", limit=3)   # показать, что ждало, пока бот лежал
        for draft in pending:
            await send_draft(bot, db, cfg.admin_id, draft.id, store)

        log.info("бот запущен, владелец %s, канал %s", cfg.admin_id, info.title or cfg.channel_id)
        await dp.start_polling(bot, handle_signals=False)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
