"""
Этап 1: скрапер отдельным скриптом.

Разовый прогон — печатает найденные посты:
    .venv\\Scripts\\python scripts\\scrape.py https://www.facebook.com/groups/Zlin.Udalosti/

Сохранить HTML ленты и /about (для фикстур тестов):
    ... scrape.py URL --save-html tests\\fixtures

Замер покрытия — круги каждые 15–25 мин до Ctrl+C, лог в watch_out\\*.csv:
    ... scrape.py URL [URL ...] --watch
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import random
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zlinbot.fb import extract  # noqa: E402
from zlinbot.fb.scraper import FacebookScraper, ScrapeResult  # noqa: E402

ROUND_INTERVAL = (15 * 60, 25 * 60)  # между кругами, с джиттером
ACTIVITY_EVERY = 55 * 60             # «Dnes N» — не чаще раза в час на группу


def fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"


def print_result(r: ScrapeResult) -> None:
    f = r.feed
    head = f"[{r.status}] {r.slug}"
    if f:
        head += (f" — {f.group_name or '?'} (id {f.group_numeric_id or '?'}): статей {f.articles}, "
                 f"заглушек {f.skeletons}, без ID {f.without_id}, постов {len(f.posts)}")
    if r.see_more_clicks:
        head += f", раскрыто «Zobrazit víc»: {r.see_more_clicks}"
    if r.error:
        head += f" — {r.error}"
    print(head)
    if r.debug_html:
        print(f"    HTML сохранён для разбора: {r.debug_html}")
    for i, p in enumerate(f.posts if f else (), 1):
        media = ", ".join(f"{k} ×{n}" for k, n in Counter(m.kind for m in p.media).items()) or "без медиа"
        flag = " · ТЕКСТ ОБРЕЗАН" if p.truncated else ""
        print(f"  #{i} {p.post_id} · {p.author or '?'} · {fmt_ts(p.created_at)} "
              f"({p.created_label or '—'}) · {media}{flag}")
        print(f"     {p.permalink}")
        for line in (p.text or "(без своего текста)").splitlines():
            print(f"     {line}")
        if p.shared_text:
            print("     ↪ репост:")
            for line in p.shared_text.splitlines():
                print(f"       {line}")


async def run_once(fb: FacebookScraper, slugs: list[str], save_dir: Path | None) -> None:
    for slug in slugs:
        feed_path = about_path = None
        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)
            feed_path = save_dir / f"group_feed_{slug}.html"
            about_path = save_dir / f"group_about_{slug}.html"
        print_result(await fb.fetch_group(slug, save_html=feed_path))
        if save_dir:
            act = await fb.fetch_activity(slug, save_html=about_path)
            print(f"    активность: {act}")
            print(f"    сохранено: {feed_path}, {about_path}")


# ---------------------------------------------------------------------------
# Замер покрытия
# ---------------------------------------------------------------------------

@dataclass
class GroupWatch:
    baseline: set[str] = field(default_factory=set)    # посты, видимые в первом круге
    captured: dict[str, extract.Post] = field(default_factory=dict)  # новые после старта
    published: int = 0                                  # ≈ опубликовано в группе после старта
    act_day: date | None = None
    act_today: int | None = None
    act_checked: float = 0.0


class Watch:
    def __init__(self, out_dir: Path, slugs: list[str]) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        self.started = time.time()
        self.groups = {s: GroupWatch() for s in slugs}
        self.rounds = 0
        self._rounds_csv = _csv(out_dir / "rounds.csv", ["ts", "slug", "status", "articles", "skeletons",
                                                        "without_id", "visible_post_ids", "new_post_ids"])
        # каждый пост — один раз, при первом появлении; kind: base (первый круг) / new / old (всплыл старый)
        self._posts_csv = _csv(out_dir / "posts.csv", ["seen_at", "slug", "kind", "post_id", "created_at",
                                                      "lag_min", "created_label", "author", "media",
                                                      "truncated", "text"])
        self._act_csv = _csv(out_dir / "activity.csv", ["ts", "slug", "today", "month"])

    def on_feed(self, r: ScrapeResult) -> list[str]:
        g = self.groups[r.slug]
        visible = [p for p in (r.feed.posts if r.feed else ())]
        new: list[str] = []
        for p in visible:
            if p.post_id in g.baseline or p.post_id in g.captured:
                continue
            if self.rounds == 1:
                kind = "base"
                g.baseline.add(p.post_id)
            elif p.created_at is None or p.created_at >= self.started:
                kind = "new"
                g.captured[p.post_id] = p
                new.append(p.post_id)
            else:
                kind = "old"                  # старый пост всплыл в «Doporučené» — не новый
                g.baseline.add(p.post_id)
            lag = round((time.time() - p.created_at) / 60) if p.created_at else ""
            self._posts_csv.writerow([_iso(), r.slug, kind, p.post_id, fmt_ts(p.created_at), lag,
                                      p.created_label, p.author, len(p.media), p.truncated,
                                      (p.text or p.shared_text or "").replace("\n", " ")[:200]])
        f = r.feed
        self._rounds_csv.writerow([_iso(), r.slug, r.status, f and f.articles, f and f.skeletons,
                                   f and f.without_id, " ".join(p.post_id for p in visible), " ".join(new)])
        return new

    def activity_due(self, slug: str) -> bool:
        return time.time() - self.groups[slug].act_checked >= ACTIVITY_EVERY

    def on_activity(self, slug: str, act: extract.Activity | None) -> None:
        g = self.groups[slug]
        g.act_checked = time.time()
        self._act_csv.writerow([_iso(), slug, act and act.today, act and act.month])
        if not act or act.today is None:
            return
        today = date.today()
        if g.act_day is None:
            pass                                    # первая точка — база
        elif g.act_day == today:
            g.published += max(0, act.today - (g.act_today or 0))
        else:
            g.published += act.today                # новый день: счётчик сбросился в полночь
        g.act_day, g.act_today = today, act.today

    def summary(self, slug: str) -> str:
        g = self.groups[slug]
        got = len(g.captured)
        cov = f"{got / g.published:.0%}" if g.published else "—"
        return (f"{slug}: поймано новых {got} из ≈{g.published} опубликованных (покрытие {cov}); "
                f"«Dnes» сейчас: {g.act_today if g.act_today is not None else '?'}")


def _csv(path: Path, header: list[str]):
    new = not path.exists()
    fh = path.open("a", newline="", encoding="utf-8-sig", buffering=1)
    w = csv.writer(fh, delimiter=";")
    if new:
        w.writerow(header)
    return w


def _iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_name(s: str) -> str:
    return s.replace(":", "_")


async def run_watch(fb: FacebookScraper, slugs: list[str], out_dir: Path) -> None:
    w = Watch(out_dir, slugs)
    print(f"Замер покрытия: {', '.join(slugs)}. Лог: {out_dir.resolve()}. Остановить — Ctrl+C.")
    try:
        while True:
            w.rounds += 1
            print(f"\n— круг {w.rounds} ({datetime.now():%H:%M}) —")
            for slug in slugs:
                last_html = out_dir / "last_feed.html"
                r = await fb.fetch_group(slug, save_html=last_html)
                known = {pid for g in [w.groups[slug]] for pid in (*g.baseline, *g.captured)}
                new = w.on_feed(r)
                # HTML каждого впервые увиденного поста — корпус реальных фикстур для тестов
                for p in (r.feed.posts if r.feed else ()):
                    if p.post_id not in known and last_html.exists():
                        (out_dir / "html").mkdir(exist_ok=True)
                        shutil.copyfile(last_html, out_dir / "html" / f"{_safe_name(p.post_id)}.html")
                status = r.status if r.status == "ok" else f"!!! {r.status}"
                print(f"  [{status}] {slug}: видно {len(r.feed.posts) if r.feed else 0}, новых {len(new)}"
                      + (" (первый круг — это база)" if w.rounds == 1 else ""))
                if r.debug_html:
                    print(f"    HTML: {r.debug_html}")
                if w.activity_due(slug):
                    w.on_activity(slug, await fb.fetch_activity(slug))
                print("  " + w.summary(slug))
            pause = random.uniform(*ROUND_INTERVAL)
            print(f"  следующий круг через {pause / 60:.0f} мин")
            await asyncio.sleep(pause)
    finally:
        print("\nИтог замера:")
        for slug in slugs:
            print("  " + w.summary(slug))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # чешские буквы в консоли Windows
    ap = argparse.ArgumentParser(description="Этап 1: скрапер публичных групп FB без входа")
    ap.add_argument("urls", nargs="+", help="ссылки на группы (или slug)")
    ap.add_argument("--save-html", type=Path, metavar="DIR", help="сохранить HTML ленты и /about в DIR")
    ap.add_argument("--watch", action="store_true", help="замер покрытия: круги до Ctrl+C")
    ap.add_argument("--out", type=Path, default=Path("watch_out"), help="куда писать CSV замера")
    ap.add_argument("--headed", action="store_true", help="показать окно браузера (для отладки)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    slugs = []
    for u in args.urls:
        slug = extract.parse_group_slug(u) or (u if "/" not in u else None)
        if not slug:
            ap.error(f"это не ссылка на группу FB: {u}")
        slugs.append(slug)

    async def go() -> None:
        async with FacebookScraper(headless=not args.headed) as fb:
            if args.watch:
                await run_watch(fb, slugs, args.out)
            else:
                await run_once(fb, slugs, args.save_html)

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
