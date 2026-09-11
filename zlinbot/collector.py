"""
Круг сбора: группы из БД -> скрапер -> дедупликация -> БД. Здоровье групп и тревоги.

Правила, которые живут здесь:
- новая группа: при добавлении проверяем доступность без логина; всё, что видно в этот
  момент, уходит в «seen» (база) — черновики только из появившегося позже;
- дедупликация: по post_id (первичный ключ), затем по хешу первых ~100 значащих символов;
- глобальный сбой ≠ недоступная группа: если за круг не прочиталась ни одна группа,
  это FB закрыл доступ нам — группы не трогаем, шлём одну тревогу и увеличиваем паузу;
  unavailable ставим, только если группа проваливается N кругов подряд, а другие читаются.

Тексты тревог — по-русски: их напрямую шлёт в личку бот (этап 4+), а пока печатает CLI.
"""
from __future__ import annotations

import logging
import random
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from . import dedup
from .config import TZ
from .db import Database, Group
from .fb import extract
from .fb.scraper import ScrapeResult

log = logging.getLogger(__name__)

UNAVAILABLE_AFTER = 3                # провалов подряд (в кругах, где другие группы читались)
RECHECK_UNAVAILABLE = 24 * 3600      # недоступную группу перепроверяем раз в сутки
STALE_AFTER = 3 * 24 * 3600          # пост старше этого при первом появлении — «всплыл старый»
ACTIVITY_EVERY = 3600                # счётчик «Dnes N» — не чаще раза в час на группу
ROUND_INTERVAL = (15 * 60, 25 * 60)  # пауза между кругами, с джиттером
BACKOFF_MAX = 2 * 3600               # потолок паузы при глобальном сбое

STATUS_TEXT = {
    "ok": "читается",
    "no_feed": "лента не отдаётся (стена логина или новая вёрстка)",
    "no_articles": "лента есть, но постов в ней не видно (сменилась разметка постов)",
    "no_ids": "посты видны, но их ID не находятся (сменились ссылки)",
    "login_wall": "Facebook требует вход",
    "checkpoint": "Facebook требует проверку (checkpoint)",
    "error": "ошибка загрузки",
}
_STRUCTURAL = frozenset({"no_articles", "no_ids"})
_BLOCKED = frozenset({"login_wall", "checkpoint"})


class Scraper(Protocol):
    async def fetch_group(self, slug: str, *, group_id: str | None = None) -> ScrapeResult: ...
    async def fetch_activity(self, slug: str) -> extract.Activity | None: ...


@dataclass
class AddResult:
    ok: bool
    group: Group | None = None
    reason: str | None = None  # почему не добавили — человеческим языком
    seen: int = 0              # сколько видимых постов ушло в базу


@dataclass
class GroupOutcome:
    group: Group
    result: ScrapeResult
    counts: Counter[str] = field(default_factory=Counter)  # new / duplicate / seen / known
    became_unavailable: bool = False
    recovered: bool = False


@dataclass
class RoundReport:
    started_at: float
    outcomes: list[GroupOutcome]
    global_failure: bool = False
    alerts: list[str] = field(default_factory=list)
    next_delay: float = 0.0

    @property
    def new_posts(self) -> int:
        return sum(o.counts["new"] for o in self.outcomes)


class Collector:
    def __init__(self, db: Database, scraper: Scraper, *, clock: Callable[[], float] = time.time,
                 rng: random.Random | None = None) -> None:
        self.db = db
        self.scraper = scraper
        self.clock = clock
        self.rng = rng or random.Random()
        self._global_streak = 0  # кругов подряд без единой прочитанной группы

    # -- добавление группы ---------------------------------------------------

    async def add_group(self, url: str) -> AddResult:
        slug = extract.parse_group_slug(url)
        if not slug:
            return AddResult(False, reason="Это не ссылка на группу Facebook (нужна facebook.com/groups/…). "
                                           "Страницы (Page) не поддерживаются.")
        if existing := await self.db.find_group("fb", slug=slug):
            return AddResult(False, existing, f"Группа уже добавлена: «{existing.title}» (#{existing.id}).")

        r = await self.scraper.fetch_group(slug)
        if r.status != "ok" or r.feed is None:
            why = STATUS_TEXT.get(r.status, r.status) + (f": {r.error}" if r.error else "")
            return AddResult(False, reason=f"Без входа группа не читается — {why}. Не добавляю.")
        fb_id = r.feed.group_numeric_id
        if fb_id and (existing := await self.db.find_group("fb", fb_id=fb_id)):
            return AddResult(False, existing, f"Группа уже добавлена под другой ссылкой: «{existing.title}» "
                                              f"(#{existing.id}).")

        group = await self.db.add_group(kind="fb", url=f"{extract.FB_ROOT}/groups/{slug}/", slug=slug,
                                        fb_id=fb_id, name=r.feed.group_name, now=self.clock())
        counts = await self._ingest(group, r, baseline=True)
        log.info("добавлена группа #%d %s, в базу: %d", group.id, group.title, counts["seen"])
        return AddResult(True, group, seen=counts["seen"])

    # -- круг ----------------------------------------------------------------

    async def run_round(self) -> RoundReport:
        now = self.clock()
        report = RoundReport(started_at=now, outcomes=[])
        groups = [g for g in await self.db.list_groups(["active", "unavailable"])
                  if g.status == "active" or now - (g.last_checked_at or 0) >= RECHECK_UNAVAILABLE]
        for g in groups:
            r = await self.scraper.fetch_group(g.slug, group_id=g.fb_id)
            outcome = GroupOutcome(g, r)
            if r.status == "ok":
                outcome.counts = await self._ingest(g, r)
                await self._maybe_activity(g)
            report.outcomes.append(outcome)

        attempted_active = [o for o in report.outcomes if o.group.status == "active"]
        any_ok = any(o.result.status == "ok" for o in report.outcomes)
        report.global_failure = bool(attempted_active) and not any_ok
        await self._apply_health(report)
        report.next_delay = self._next_delay()
        if report.global_failure and self._global_streak == 1:
            report.alerts.insert(0, self._global_alert(report))
        return report

    async def check_group(self, group_id: int) -> GroupOutcome | None:
        """«Проверить сейчас» для одной группы. Провал одной группы вне круга не отличить
        от глобального, поэтому счётчик провалов тут не трогаем."""
        g = await self.db.get_group(group_id)
        if g is None:
            return None
        r = await self.scraper.fetch_group(g.slug, group_id=g.fb_id)
        outcome = GroupOutcome(g, r)
        now = int(self.clock())
        if r.status == "ok":
            outcome.counts = await self._ingest(g, r)
            outcome.recovered = g.status == "unavailable"
            await self.db.update_group(g.id, last_status="ok", last_checked_at=now, last_ok_at=now,
                                       fail_streak=0, **({"status": "active"} if outcome.recovered else {}))
        else:
            await self.db.update_group(g.id, last_status=r.status, last_checked_at=now)
        return outcome

    def _next_delay(self) -> float:
        base = self.rng.uniform(*ROUND_INTERVAL)
        return min(base * 2 ** self._global_streak, BACKOFF_MAX) if self._global_streak else base

    # -- внутреннее ----------------------------------------------------------

    async def _ingest(self, group: Group, r: ScrapeResult, *, baseline: bool = False) -> Counter[str]:
        counts: Counter[str] = Counter()
        now = self.clock()
        for p in (r.feed.posts if r.feed else ()):
            if await self.db.known_post(p.post_id):
                counts["known"] += 1
                continue
            h = dedup.post_hash(p.text, p.shared_text)
            dup_of = await self.db.find_by_hash(h) if h else None
            if baseline:
                status, note = "seen", "база при добавлении группы"
            elif p.created_at and p.created_at < group.added_at:
                status, note = "seen", "старый пост: создан до добавления группы"
            elif p.created_at and now - p.created_at > STALE_AFTER:
                status, note = "seen", "старый пост всплыл в ленте"
            elif dup_of:
                status, note = "duplicate", None
            else:
                status, note = "new", None
            await self.db.insert_post(
                post_id=p.post_id, group_id=group.id, permalink=p.permalink, author=p.author, text=p.text,
                shared_text=p.shared_text, media=[{"kind": m.kind, "url": m.url} for m in p.media],
                created_at=p.created_at, status=status, text_hash=h,
                dup_of=dup_of if status == "duplicate" else None, note=note, now=now)
            counts[status] += 1
            if not baseline:  # одно событие на пост: collected (новый) / duplicate / old (всплыл старый)
                event = {"new": "collected", "duplicate": "duplicate"}.get(status, "old")
                await self.db.log(event, group_id=group.id, post_id=p.post_id, now=now)
        if r.feed and r.feed.group_numeric_id and not group.fb_id:
            await self.db.update_group(group.id, fb_id=r.feed.group_numeric_id)
        return counts

    async def _maybe_activity(self, g: Group) -> None:
        now = self.clock()
        last = await self.db.last_activity_ts(g.id)
        if last is not None and now - last < ACTIVITY_EVERY:
            return
        act = await self.scraper.fetch_activity(g.slug)
        if act is not None:
            await self.db.add_activity(g.id, today=act.today, month=act.month, now=self.clock())

    async def _apply_health(self, report: RoundReport) -> None:
        now = int(self.clock())
        if report.global_failure:
            self._global_streak += 1
            for o in report.outcomes:  # группы не наказываем: проблема не в них
                await self.db.update_group(o.group.id, last_status=o.result.status, last_checked_at=now)
            return

        if self._global_streak:
            report.alerts.append(f"✅ Facebook снова читается (после {self._global_streak} неудачных кругов подряд).")
        self._global_streak = 0
        for o in report.outcomes:
            g, st = o.group, o.result.status
            if st == "ok":
                o.recovered = g.status == "unavailable"
                await self.db.update_group(g.id, last_status="ok", last_checked_at=now, last_ok_at=now,
                                           fail_streak=0, **({"status": "active"} if o.recovered else {}))
                if o.recovered:
                    report.alerts.append(f"✅ «{g.title}» снова читается без входа.")
                continue

            streak = g.fail_streak + 1
            fields: dict[str, object] = {"last_status": st, "last_checked_at": now, "fail_streak": streak}
            if g.status == "active" and streak >= UNAVAILABLE_AFTER:
                fields["status"] = "unavailable"
                o.became_unavailable = True
                report.alerts.append(
                    f"⚠️ «{g.title}» не читается {streak} круга подряд, хотя остальные группы читаются: "
                    f"{STATUS_TEXT.get(st, st)}. Пометил как unavailable, проверю снова через сутки."
                    + _debug_hint(o.result))
            elif g.status == "active" and st in _STRUCTURAL and g.last_status == "ok":
                report.alerts.append(f"⚠️ «{g.title}»: {STATUS_TEXT[st]}." + _debug_hint(o.result))
            await self.db.update_group(g.id, **fields)

    def _global_alert(self, report: RoundReport) -> str:
        statuses = Counter(o.result.status for o in report.outcomes)
        summary = ", ".join(f"{STATUS_TEXT.get(s, s)} — {n}" for s, n in statuses.items())
        if set(statuses) <= _STRUCTURAL:
            why = ("Лента отдаётся, но посты или их ID не находятся ни в одной группе — "
                   "похоже, Facebook сменил вёрстку. Чинить в zlinbot/fb/selectors.py.")
        elif set(statuses) & _BLOCKED:
            why = "Facebook требует вход или проверку для этого IP."
        elif set(statuses) == {"no_feed"}:
            why = "Лента не отдаётся нигде — похоже, стена логина для этого IP (или новая вёрстка)."
        else:
            why = "Ошибки разные — смотри логи."
        dump = next((o.result for o in report.outcomes if o.result.debug_html), None)
        return (f"🚨 Ни одна группа не прочиталась ({summary}).\n{why}\n"
                f"Группы недоступными не помечаю — проблема, скорее всего, не в них. "
                f"Следующая попытка через ~{report.next_delay / 60:.0f} мин." + (_debug_hint(dump) if dump else ""))


def _debug_hint(r: ScrapeResult) -> str:
    return f"\nHTML страницы: {r.debug_html}" if r.debug_html else ""


def fmt_local(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M") if ts else "—"
