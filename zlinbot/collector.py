"""
Круг сбора: источники из БД -> скрапер/RSS -> дедупликация -> БД. Здоровье и тревоги.

Правила, которые живут здесь:
- новый источник: сначала проверяем, читается ли он вообще; всё, что видно в этот момент,
  уходит в «seen» (база) — черновики только из появившегося позже;
- дедупликация: по post_id (первичный ключ), затем по хешу первых ~100 значащих символов;
  она общая для Facebook и RSS, поэтому одна новость из двух источников не пройдёт дважды;
- глобальный сбой ≠ мёртвый источник, и считается ОТДЕЛЬНО по типу источника: если бы
  «другие читаются» считалось по всем сразу, то живой RSS во время стены логина в FB
  отправил бы все группы FB в unavailable. При глобальном сбое типа — одна тревога и
  пауза для этого типа (до 2 ч), остальные источники работают дальше.

Тексты тревог — по-русски: их напрямую шлёт в личку бот (этап 4+), а пока печатает CLI.
"""
from __future__ import annotations

import logging
import random
import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from . import dedup
from .config import TZ
from .db import Database, Group
from .fb import extract
from .fb.scraper import ScrapeResult
from .rss import FetchResult

log = logging.getLogger(__name__)

UNAVAILABLE_AFTER = 3                # провалов подряд (в кругах, где другие источники того же типа читались)
RECHECK_UNAVAILABLE = 24 * 3600      # недоступный источник перепроверяем раз в сутки
STALE_AFTER = 3 * 24 * 3600          # запись старше этого при первом появлении — «всплыла старая»
ACTIVITY_EVERY = 12 * 3600           # «Dnes N» со страницы /about — 2 раза в сутки, это лишние запросы к FB
ROUND_INTERVAL = (15 * 60, 25 * 60)  # пауза между кругами, с джиттером
COOLDOWN_MAX = 2 * 3600              # потолок паузы для типа источника после глобального сбоя
OK_STATUSES = frozenset({"ok", "not_modified"})

STATUS_TEXT = {
    "ok": "читается",
    "not_modified": "без изменений",
    "no_feed": "лента не отдаётся (стена логина или новая вёрстка)",
    "no_articles": "лента есть, но постов в ней не видно (сменилась разметка постов)",
    "no_ids": "посты видны, но их ID не находятся (сменились ссылки)",
    "login_wall": "Facebook требует вход",
    "checkpoint": "Facebook требует проверку (checkpoint)",
    "error": "ошибка загрузки",
}
KIND_TEXT = {"fb": "Facebook", "rss": "RSS"}
_STRUCTURAL = frozenset({"no_articles", "no_ids"})
_BLOCKED = frozenset({"login_wall", "checkpoint"})
_URL_RE = re.compile(r"^https?://", re.I)


class Scraper(Protocol):
    async def fetch_group(self, slug: str, *, group_id: str | None = None) -> ScrapeResult: ...
    async def fetch_activity(self, slug: str) -> extract.Activity | None: ...


class Feeds(Protocol):
    async def fetch(self, url: str, *, etag: str | None = None,
                    last_modified: str | None = None) -> FetchResult: ...


@dataclass(frozen=True, slots=True)
class SourceResult:
    """Общий вид результата проверки источника — и для Facebook, и для RSS."""
    status: str
    items: Sequence[Any] = ()          # posts из extract / items из rss — общие поля контракта
    error: str | None = None
    debug_html: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)  # fb_id/name или etag/last_modified

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES


@dataclass
class AddResult:
    ok: bool
    group: Group | None = None
    reason: str | None = None  # почему не добавили — человеческим языком
    seen: int = 0              # сколько видимых записей ушло в базу


@dataclass
class GroupOutcome:
    group: Group
    result: SourceResult
    counts: Counter[str] = field(default_factory=Counter)  # new / duplicate / seen / known
    became_unavailable: bool = False
    recovered: bool = False


@dataclass
class RoundReport:
    started_at: float
    outcomes: list[GroupOutcome]
    global_failure: bool = False          # хотя бы у одного типа источников не прочиталось ничего
    failed_kinds: list[str] = field(default_factory=list)
    skipped_kinds: list[str] = field(default_factory=list)  # пропущены — идёт пауза после сбоя
    alerts: list[str] = field(default_factory=list)
    next_delay: float = 0.0

    @property
    def new_posts(self) -> int:
        return sum(o.counts["new"] for o in self.outcomes)


class Collector:
    def __init__(self, db: Database, scraper: Scraper | None = None, feeds: Feeds | None = None, *,
                 clock: Callable[[], float] = time.time, rng: random.Random | None = None) -> None:
        self.db = db
        self.scraper = scraper
        self.feeds = feeds
        self.clock = clock
        self.rng = rng or random.Random()
        self._cooldown: dict[str, float] = {}   # тип источника -> до какого времени его не трогаем
        self._fail_streak: dict[str, int] = {}  # тип источника -> глобальных сбоёв подряд

    # -- добавление источника ------------------------------------------------

    async def add_source(self, url: str) -> AddResult:
        url = url.strip()
        if extract.parse_group_slug(url):
            return await self._add_fb(url)
        if "facebook.com" in url.lower():
            return AddResult(False, reason="Это ссылка на Facebook, но не на группу (нужна "
                                           "facebook.com/groups/…). Страницы (Page) не поддерживаются.")
        if _URL_RE.match(url):
            return await self._add_rss(url)
        return AddResult(False, reason="Не похоже ни на ссылку группы Facebook, ни на адрес RSS-ленты.")

    async def _add_fb(self, url: str) -> AddResult:
        slug = extract.parse_group_slug(url)
        assert slug and self.scraper
        if existing := await self.db.find_group("fb", slug=slug):
            return AddResult(False, existing, f"Группа уже добавлена: «{existing.title}» (#{existing.id}).")

        r = _from_scrape(await self.scraper.fetch_group(slug))
        if not r.ok:
            why = STATUS_TEXT.get(r.status, r.status) + (f": {r.error}" if r.error else "")
            return AddResult(False, reason=f"Без входа группа не читается — {why}. Не добавляю.")
        fb_id = r.meta.get("fb_id")
        if fb_id and (existing := await self.db.find_group("fb", fb_id=fb_id)):
            return AddResult(False, existing, f"Группа уже добавлена под другой ссылкой: "
                                              f"«{existing.title}» (#{existing.id}).")
        group = await self.db.add_group(kind="fb", url=f"{extract.FB_ROOT}/groups/{slug}/", slug=slug,
                                        fb_id=fb_id, name=r.meta.get("name"), now=self.clock())
        return await self._baseline(group, r)

    async def _add_rss(self, url: str) -> AddResult:
        assert self.feeds
        if existing := await self.db.find_group("rss", slug=url):
            return AddResult(False, existing, f"Лента уже добавлена: «{existing.title}» (#{existing.id}).")
        r = _from_feed(await self.feeds.fetch(url))
        if not r.ok:
            return AddResult(False, reason=f"Лента не читается — {r.error or STATUS_TEXT.get(r.status)}. Не добавляю.")
        group = await self.db.add_group(kind="rss", url=url, slug=url, fb_id=None,
                                        name=r.meta.get("name"), now=self.clock(),
                                        etag=r.meta.get("etag"), last_modified=r.meta.get("last_modified"))
        return await self._baseline(group, r)

    async def _baseline(self, group: Group, r: SourceResult) -> AddResult:
        counts = await self._ingest(group, r.items, baseline=True)
        log.info("добавлен источник #%d %s (%s), в базу: %d", group.id, group.title, group.kind, counts["seen"])
        return AddResult(True, group, seen=counts["seen"])

    # -- круг ----------------------------------------------------------------

    async def run_round(self) -> RoundReport:
        now = self.clock()
        report = RoundReport(started_at=now, outcomes=[])
        for g in await self._due_groups(now, report):
            r = await self._fetch(g)
            outcome = GroupOutcome(g, r)
            if r.ok:
                outcome.counts = await self._ingest(g, r.items)
                await self._save_meta(g, r)
                if g.kind == "fb":
                    await self._maybe_activity(g)
            report.outcomes.append(outcome)
        await self._apply_health(report)
        report.next_delay = self.rng.uniform(*ROUND_INTERVAL)
        return report

    async def check_group(self, group_id: int) -> GroupOutcome | None:
        """«Проверить сейчас» для одного источника — в обход паузы. Провал вне круга не отличить
        от глобального, поэтому счётчик провалов тут не трогаем."""
        g = await self.db.get_group(group_id)
        if g is None:
            return None
        r = await self._fetch(g)
        outcome = GroupOutcome(g, r)
        now = int(self.clock())
        if r.ok:
            outcome.counts = await self._ingest(g, r.items)
            await self._save_meta(g, r)
            outcome.recovered = g.status == "unavailable"
            await self.db.update_group(g.id, last_status=r.status, last_checked_at=now, last_ok_at=now,
                                       fail_streak=0, **({"status": "active"} if outcome.recovered else {}))
        else:
            await self.db.update_group(g.id, last_status=r.status, last_checked_at=now)
        return outcome

    # -- внутреннее ----------------------------------------------------------

    async def _due_groups(self, now: float, report: RoundReport) -> list[Group]:
        due = []
        for g in await self.db.list_groups(["active", "unavailable"]):
            if self._cooldown.get(g.kind, 0) > now:
                if g.kind not in report.skipped_kinds:
                    report.skipped_kinds.append(g.kind)
                continue
            if g.status == "unavailable" and now - (g.last_checked_at or 0) < RECHECK_UNAVAILABLE:
                continue
            due.append(g)
        return due

    async def _fetch(self, g: Group) -> SourceResult:
        if g.kind == "fb":
            if not self.scraper:
                return SourceResult("error", error="скрапер Facebook не подключён")
            return _from_scrape(await self.scraper.fetch_group(g.slug, group_id=g.fb_id))
        if not self.feeds:
            return SourceResult("error", error="загрузчик RSS не подключён")
        return _from_feed(await self.feeds.fetch(g.slug, etag=g.etag, last_modified=g.last_modified))

    async def _save_meta(self, g: Group, r: SourceResult) -> None:
        fields = {k: v for k, v in r.meta.items() if k in ("etag", "last_modified") and v}
        if g.kind == "fb" and r.meta.get("fb_id") and not g.fb_id:
            fields["fb_id"] = r.meta["fb_id"]
        if fields:
            await self.db.update_group(g.id, **fields)

    async def _ingest(self, group: Group, items: Sequence[Any], *, baseline: bool = False) -> Counter[str]:
        counts: Counter[str] = Counter()
        now = self.clock()
        for p in items:
            if await self.db.known_post(p.post_id):
                counts["known"] += 1
                continue
            shared = getattr(p, "shared_text", None)
            h = dedup.post_hash(p.text, shared)
            dup_of = await self.db.find_by_hash(h) if h else None
            if baseline:
                status, note = "seen", "база при добавлении источника"
            elif p.created_at and p.created_at < group.added_at:
                status, note = "seen", "старая запись: появилась до добавления источника"
            elif p.created_at and now - p.created_at > STALE_AFTER:
                status, note = "seen", "старая запись всплыла в ленте"
            elif dup_of:
                status, note = "duplicate", None
            else:
                status, note = "new", None
            await self.db.insert_post(
                post_id=p.post_id, group_id=group.id, permalink=p.permalink, author=p.author, text=p.text,
                shared_text=shared, media=[{"kind": m.kind, "url": m.url} for m in p.media],
                created_at=p.created_at, status=status, text_hash=h,
                dup_of=dup_of if status == "duplicate" else None, note=note, now=now)
            counts[status] += 1
            if not baseline:  # одно событие на запись: collected (новая) / duplicate / old (всплыла старая)
                event = {"new": "collected", "duplicate": "duplicate"}.get(status, "old")
                await self.db.log(event, group_id=group.id, post_id=p.post_id, now=now)
        return counts

    async def _maybe_activity(self, g: Group) -> None:
        assert self.scraper
        now = self.clock()
        last = await self.db.last_activity_ts(g.id)
        if last is not None and now - last < ACTIVITY_EVERY:
            return
        act = await self.scraper.fetch_activity(g.slug)
        if act is not None:
            await self.db.add_activity(g.id, today=act.today, month=act.month, now=self.clock())

    async def _apply_health(self, report: RoundReport) -> None:
        now = int(self.clock())
        by_kind: dict[str, list[GroupOutcome]] = {}
        for o in report.outcomes:
            by_kind.setdefault(o.group.kind, []).append(o)

        for kind, outs in by_kind.items():
            has_active = any(o.group.status == "active" for o in outs)
            if has_active and not any(o.result.ok for o in outs):
                report.global_failure = True
                report.failed_kinds.append(kind)
                self._fail_streak[kind] = streak = self._fail_streak.get(kind, 0) + 1
                pause = min(ROUND_INTERVAL[1] * 2 ** streak, COOLDOWN_MAX)
                self._cooldown[kind] = now + pause
                for o in outs:  # источники не наказываем: проблема не в них
                    await self.db.update_group(o.group.id, last_status=o.result.status, last_checked_at=now)
                if streak == 1:
                    report.alerts.insert(0, _global_alert(kind, outs, pause))
                continue

            if self._fail_streak.pop(kind, 0):
                self._cooldown.pop(kind, None)
                report.alerts.append(f"✅ {KIND_TEXT.get(kind, kind)} снова читается.")
            await self._apply_group_health(outs, report, now)

    async def _apply_group_health(self, outs: list[GroupOutcome], report: RoundReport, now: int) -> None:
        for o in outs:
            g, st = o.group, o.result.status
            if o.result.ok:
                o.recovered = g.status == "unavailable"
                await self.db.update_group(g.id, last_status=st, last_checked_at=now, last_ok_at=now,
                                           fail_streak=0, **({"status": "active"} if o.recovered else {}))
                if o.recovered:
                    report.alerts.append(f"✅ «{g.title}» снова читается.")
                continue

            streak = g.fail_streak + 1
            fields: dict[str, object] = {"last_status": st, "last_checked_at": now, "fail_streak": streak}
            if g.status == "active" and streak >= UNAVAILABLE_AFTER:
                fields["status"] = "unavailable"
                o.became_unavailable = True
                report.alerts.append(
                    f"⚠️ «{g.title}» не читается {streak} круга подряд, хотя остальные источники "
                    f"({KIND_TEXT.get(g.kind, g.kind)}) читаются: {_why(o.result)}. "
                    f"Пометил как unavailable, проверю снова через сутки." + _debug_hint(o.result))
            elif g.status == "active" and st in _STRUCTURAL and g.last_status == "ok":
                report.alerts.append(f"⚠️ «{g.title}»: {STATUS_TEXT[st]}." + _debug_hint(o.result))
            await self.db.update_group(g.id, **fields)


# ---------------------------------------------------------------------------
# Приведение результатов источников к общему виду
# ---------------------------------------------------------------------------

def _from_scrape(r: ScrapeResult) -> SourceResult:
    meta = {"fb_id": r.feed.group_numeric_id, "name": r.feed.group_name} if r.feed else {}
    return SourceResult(r.status, r.feed.posts if r.feed else (), r.error, r.debug_html, meta)


def _from_feed(r: FetchResult) -> SourceResult:
    meta = {"name": r.feed.title if r.feed else None, "etag": r.etag, "last_modified": r.last_modified}
    return SourceResult(r.status, r.items, r.error, None, meta)


def _why(r: SourceResult) -> str:
    return STATUS_TEXT.get(r.status, r.status) + (f" ({r.error})" if r.error else "")


def _global_alert(kind: str, outs: list[GroupOutcome], pause: float) -> str:
    statuses = Counter(o.result.status for o in outs)
    summary = ", ".join(f"{STATUS_TEXT.get(s, s)} — {n}" for s, n in statuses.items())
    if kind == "rss":
        why = "Ни одна лента не ответила — похоже, пропал интернет или упали сайты-источники."
    elif set(statuses) <= _STRUCTURAL:
        why = ("Лента отдаётся, но посты или их ID не находятся ни в одной группе — "
               "похоже, Facebook сменил вёрстку. Чинить в zlinbot/fb/selectors.py.")
    elif set(statuses) & _BLOCKED:
        why = "Facebook требует вход или проверку для этого IP."
    elif set(statuses) == {"no_feed"}:
        why = "Лента не отдаётся нигде — похоже, стена логина для этого IP (или новая вёрстка)."
    else:
        why = "Ошибки разные — смотри логи."
    dump = next((o.result for o in outs if o.result.debug_html), None)
    return (f"🚨 {KIND_TEXT.get(kind, kind)}: не прочиталось ни одного источника ({summary}).\n{why}\n"
            f"Источники недоступными не помечаю — проблема, скорее всего, не в них. "
            f"Следующая попытка через ~{pause / 60:.0f} мин." + (_debug_hint(dump) if dump else ""))


def _debug_hint(r: SourceResult) -> str:
    return f"\nHTML страницы: {r.debug_html}" if r.debug_html else ""


def fmt_local(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M") if ts else "—"
