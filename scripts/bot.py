"""
Запуск: бот, сбор и разбор в одном процессе.

    .venv\\Scripts\\python scripts\\bot.py     (или run.bat — он ещё и venv поднимет)

Нужны в .env (или в переменных окружения): BOT_TOKEN, ADMIN_ID, CHANNEL_ID, GEMINI_API_KEY.

Фоновые задачи живут под присмотром supervisor: упавшая не умирает молча, а пишет
владельцу и перезапускается. Второй экземпляр не запустится — Telegram отдаёт обновления
только одному, и молча перестают отвечать оба.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.types import BotCommand  # noqa: E402

from zlinbot import config, digest  # noqa: E402
from zlinbot.bot.app import build_dispatcher, check_channel, make_bot, startup_report  # noqa: E402
from zlinbot.bot.handlers import send_draft  # noqa: E402
from zlinbot.bot.publisher import Publisher  # noqa: E402
from zlinbot.collector import Collector  # noqa: E402
from zlinbot.db import Database  # noqa: E402
from zlinbot.fb.scraper import LazyFacebookScraper  # noqa: E402
from zlinbot.gemini import Gemini  # noqa: E402
from zlinbot.media import MediaStore  # noqa: E402
from zlinbot.pipeline import Processor, current_model  # noqa: E402
from zlinbot.rss import RssFetcher  # noqa: E402
from zlinbot.single import SingleInstance  # noqa: E402
from zlinbot.supervisor import Supervisor  # noqa: E402

log = logging.getLogger("zlinbot.run")

PROCESS_EVERY = 5 * 60       # как часто разбирать накопленное
COLLECT_EVERY = 20 * 60      # запасной интервал: обычно его задаёт сам круг сбора
MEDIA_TTL = 3 * 24 * 3600    # файлы черновиков без решения дольше этого — в утиль

COMMANDS = [
    BotCommand(command="pending", description="очередь черновиков"),
    BotCommand(command="groups", description="источники"),
    BotCommand(command="add", description="добавить источник"),
    BotCommand(command="filters", description="стоп-слова"),
    BotCommand(command="settings", description="медиа, модель, критерии"),
    BotCommand(command="stats", description="статистика"),
    BotCommand(command="models", description="модели Gemini"),
]


async def main_async() -> None:
    cfg = config.load()
    missing = [name for name, value in (("BOT_TOKEN", cfg.bot_token), ("ADMIN_ID", cfg.admin_id),
                                        ("CHANNEL_ID", cfg.channel_id)) if not value]
    if missing:
        print("Не хватает настроек: " + ", ".join(missing) + ". Образец — .env.example.")
        return

    lock = SingleInstance()
    if not lock.acquire():
        print(f"Бот уже запущен (процесс {lock.owner_pid() or 'неизвестен'}). "
              f"Два экземпляра мешают друг другу — этот не стартую.")
        return

    async with Database(cfg.db_path) as db, contextlib.AsyncExitStack() as stack:
        stack.callback(lock.release)
        bot = make_bot(cfg.bot_token)
        stack.push_async_callback(bot.session.close)
        gemini = None
        if cfg.gemini_key:
            gemini = await stack.enter_async_context(
                Gemini(cfg.gemini_key, model=await current_model(db, cfg.gemini_model)))
        else:
            log.warning("нет GEMINI_API_KEY — разбор и «Переписать» работать не будут")

        store = await stack.enter_async_context(MediaStore(cfg.media_dir))
        scraper = await stack.enter_async_context(
            LazyFacebookScraper(headless=cfg.headless, debug_dir=cfg.debug_dir))
        feeds = await stack.enter_async_context(RssFetcher())
        collector = Collector(db, scraper, feeds)
        processor = Processor(db, gemini, store=store)

        info = await check_channel(bot, cfg.channel_id)
        publisher = Publisher(bot, db, cfg.channel_id, channel_username=info.username,
                              discussion_chat_id=info.discussion_chat_id, store=store)
        dp = build_dispatcher(db, publisher, gemini, admin_id=cfg.admin_id,
                              discussion_chat_id=info.discussion_chat_id, store=store,
                              collector=collector)

        async def notify(text: str) -> None:
            await bot.send_message(cfg.admin_id, text)

        async def collect_round() -> float:
            report = await collector.run_round()
            for alert in report.alerts:
                await notify(alert)
            log.info("круг сбора: новых записей %d", report.new_posts)
            return report.next_delay

        async def process_round() -> float:
            report = await processor.run()
            for alert in report.alerts:
                await notify(alert)
            for decision in report.decisions:
                if decision.status == "pending" and decision.draft_id:
                    await send_draft(bot, db, cfg.admin_id, decision.draft_id, store)
            store.purge_older_than(MEDIA_TTL)
            if report.paused_until:                     # квота Gemini кончилась — ждём сброса
                return max(PROCESS_EVERY, report.paused_until - time.time())
            return PROCESS_EVERY

        async def daily_digest() -> float:
            await notify(await digest.build(db))
            return digest.seconds_until_digest()

        supervisor = Supervisor(notify=notify)
        supervisor.add("сбор", collect_round, interval=COLLECT_EVERY)
        supervisor.add("разбор", process_round, interval=PROCESS_EVERY, first_delay=60)
        supervisor.add("сводка", daily_digest, interval=24 * 3600,
                       first_delay=digest.seconds_until_digest())

        await bot.set_my_commands(COMMANDS)
        await notify(startup_report(info))
        for draft in await db.drafts(status="pending", limit=3):
            await send_draft(bot, db, cfg.admin_id, draft.id, store)

        background = asyncio.create_task(supervisor.run_forever(), name="supervisor")
        log.info("бот запущен, владелец %s, канал %s", cfg.admin_id, info.title or cfg.channel_id)
        try:
            await dp.start_polling(bot, handle_signals=False)
        finally:
            background.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
