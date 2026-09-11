"""
Этап 2: БД и дедупликация — управление из командной строки, пока нет бота.

    collect.py add URL             проверить группу без входа и добавить (видимые посты -> «seen»)
    collect.py list                группы и их статусы
    collect.py run [--watch]       круг сбора по активным группам (или круги до Ctrl+C)
    collect.py check ID            проверить одну группу сейчас
    collect.py posts [--status S] [--limit N]
    collect.py stats               события за сутки и неделю, покрытие по дням
    collect.py pause|resume|delete ID
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zlinbot import config  # noqa: E402
from zlinbot.collector import STATUS_TEXT, Collector, RoundReport, fmt_local  # noqa: E402
from zlinbot.db import Database, day_bounds  # noqa: E402
from zlinbot.fb.scraper import FacebookScraper  # noqa: E402

EVENT_TEXT = {
    "collected": "новых постов", "duplicate": "дублей (тот же текст уже был)", "old": "всплыло старых",
    "filtered": "отсеяно стоп-словами", "skipped": "Gemini: skip", "drafted": "черновиков",
    "published": "опубликовано", "rejected": "отклонено",
}


def print_report(r: RoundReport) -> None:
    print(f"\n— круг {datetime.now():%H:%M} —")
    for o in r.outcomes:
        c = o.counts
        tail = (f"новых {c['new']}, дублей {c['duplicate']}, старых {c['seen']}, уже известных {c['known']}"
                if o.result.status == "ok" else STATUS_TEXT.get(o.result.status, o.result.status))
        print(f"  #{o.group.id} {o.group.title}: [{o.result.status}] {tail}")
    for a in r.alerts:
        print("  " + a.replace("\n", "\n  "))
    print(f"  следующий круг через {r.next_delay / 60:.0f} мин")


async def cmd_list(db: Database) -> None:
    groups = await db.list_groups()
    if not groups:
        print("Групп нет. Добавь: collect.py add <ссылка>")
    for g in groups:
        print(f"#{g.id} [{g.status}] {g.title}  ({g.url}, fb_id {g.fb_id or '?'})\n"
              f"    последняя проверка {fmt_local(g.last_checked_at)}: {g.last_status or '—'}, "
              f"последний успех {fmt_local(g.last_ok_at)}, провалов подряд {g.fail_streak}")


async def cmd_posts(db: Database, status: str | None, limit: int) -> None:
    for p in await db.posts(status=status, limit=limit):
        text = (p.text or p.shared_text or "").replace("\n", " ")
        dup = f" (дубль {p.dup_of})" if p.dup_of else ""
        print(f"[{p.status}]{dup} {p.post_id} · {p.author or '?'} · создан {fmt_local(p.created_at)} · "
              f"увиден {fmt_local(p.seen_at)} · медиа {len(p.media)}\n    {text[:160]}")
    print("\nвсего по статусам:", await db.post_status_counts())


async def cmd_stats(db: Database) -> None:
    now = time.time()
    for label, since in (("за сутки", now - 86400), ("за неделю", now - 7 * 86400)):
        counts = await db.event_counts(since)
        body = ", ".join(f"{EVENT_TEXT.get(k, k)}: {v}" for k, v in sorted(counts.items())) or "событий нет"
        print(f"{label}: {body}")
    print("\nпокрытие (поймано постов с датой этого дня / максимум «Dnes N» за день — оценка):")
    for g in await db.list_groups():
        for day in (date.today(), date.today() - timedelta(days=1)):
            start, end = day_bounds(day)
            got = await db.posts_created_between(g.id, start, end)
            samples = [today for ts, today in await db.activity(g.id, start) if today is not None and ts < end]
            peak = max(samples) if samples else None
            cov = f"{got / peak:.0%}" if peak else "—"
            print(f"  #{g.id} {g.title} {day:%d.%m}: поймано {got}, «Dnes» макс {peak if peak is not None else '?'}"
                  f" -> {cov}")


async def main_async(args: argparse.Namespace) -> None:
    cfg = config.load()
    async with Database(cfg.db_path) as db:
        if args.cmd == "list":
            return await cmd_list(db)
        if args.cmd == "posts":
            return await cmd_posts(db, args.status, args.limit)
        if args.cmd == "stats":
            return await cmd_stats(db)
        if args.cmd in ("pause", "resume"):
            await db.update_group(args.id, status="paused" if args.cmd == "pause" else "active", fail_streak=0)
            return await cmd_list(db)
        if args.cmd == "delete":
            print("удалена" if await db.delete_group(args.id) else "нет такой группы")
            return None

        async with FacebookScraper(headless=cfg.headless, debug_dir=cfg.debug_dir) as fb:
            collector = Collector(db, fb)
            if args.cmd == "add":
                res = await collector.add_group(args.url)
                if res.ok and res.group:
                    print(f"Добавлена #{res.group.id} «{res.group.title}» (fb_id {res.group.fb_id}). "
                          f"Видимых постов ушло в базу (не обрабатываются): {res.seen}.")
                else:
                    print(f"Не добавлена. {res.reason}")
            elif args.cmd == "check":
                o = await collector.check_group(args.id)
                if o is None:
                    print("нет такой группы")
                else:
                    print(f"#{o.group.id} {o.group.title}: [{o.result.status}] {dict(o.counts)}")
            elif args.cmd == "run":
                while True:
                    report = await collector.run_round()
                    print_report(report)
                    if not args.watch:
                        break
                    await asyncio.sleep(report.next_delay)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Этап 2: сбор постов в БД с дедупликацией")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("add", help="проверить и добавить группу").add_argument("url")
    sub.add_parser("list", help="группы и статусы")
    run = sub.add_parser("run", help="круг сбора")
    run.add_argument("--watch", action="store_true", help="круги каждые 15–25 мин до Ctrl+C")
    for name in ("check", "pause", "resume", "delete"):
        sub.add_parser(name).add_argument("id", type=int)
    posts = sub.add_parser("posts", help="последние посты из БД")
    posts.add_argument("--status")
    posts.add_argument("--limit", type=int, default=20)
    sub.add_parser("stats", help="статистика и покрытие")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
