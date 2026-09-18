"""
Обработка накопленных записей: стоп-слова -> Gemini -> черновик.

Порядок важен: сначала бесплатный отсев по стоп-словам, и только потом Gemini —
у бесплатного тарифа считается каждый вызов.

Отдельно разведены четыре вида сбоя модели, потому что реагировать на них надо по-разному:
- суточная квота: ретраи бессмысленны; берём следующую модель (у неё свой счётчик),
  а когда список кончится — встаём до сброса и говорим в личку;
- 400 (модель снята, ключ не принят): круг останавливаем, само не пройдёт;
- временный сбой (перегрузка, минутный лимит, сеть): запись остаётся в очереди, круг
  прерываем до следующего раза — виновата модель, а не запись;
- фильтр безопасности или мусор вместо JSON: это про одну запись, помечаем её и идём дальше.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import filters
from .db import Database, StoredPost
from .media import DownloadReport, MediaStore
from .gemini import (DEFAULT_CRITERIA, DEFAULT_MODEL, GeminiBadRequest, GeminiBlocked, GeminiError,
                     GeminiQuotaExhausted, GeminiRetryable, Verdict, next_model)
from .textnorm import significant

log = logging.getLogger(__name__)

MAX_PER_ROUND = 8   # из ТЗ: не более 8 записей за круг
MIN_TEXT = 20       # значащих символов; короче — пересказывать нечего
MAX_ATTEMPTS = 10   # столько кругов запись ждёт живую модель, потом всё-таки в «сломано»


@dataclass
class Decision:
    post: StoredPost
    status: str                     # filtered / skipped / pending / failed
    note: str | None = None
    draft_id: int | None = None
    verdict: Verdict | None = None
    media: DownloadReport | None = None


@dataclass
class ProcessReport:
    decisions: list[Decision] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    paused_until: float | None = None   # квота Gemini кончилась — до этого времени не дёргаем

    def count(self, status: str) -> int:
        return sum(1 for d in self.decisions if d.status == status)


class Summarizer:
    """Минимум, который нужен конвейеру от Gemini (в тестах подменяется)."""

    model: str

    async def summarize(self, text: str, *, source: str = "", criteria: str = DEFAULT_CRITERIA,
                        extra: str = "") -> Verdict: ...


class Processor:
    def __init__(self, db: Database, gemini: Summarizer | None, *, store: MediaStore | None = None,
                 clock: Callable[[], float] = time.time, limit: int = MAX_PER_ROUND) -> None:
        self.db = db
        self.gemini = gemini
        self.store = store
        self.clock = clock
        self.limit = limit
        self._paused_until: float | None = None  # пауза до сброса суточной квоты
        self._preferred_model: str | None = None  # модель до перехода на запасную

    async def run(self) -> ProcessReport:
        report = ProcessReport(paused_until=self._paused_until)
        now = self.clock()
        if self._paused_until and now < self._paused_until:
            return report
        if self._paused_until:
            report.alerts.append("✅ Суточная квота Gemini обновилась, продолжаю разбор.")
            self._paused_until = report.paused_until = None
            await self._restore_model(report)
        if self.gemini is None:
            return report

        stop_words = [norm for _, _, norm in await self.db.list_filters()]
        criteria = await self.db.get_setting("relevance", DEFAULT_CRITERIA)
        posts = await self.db.posts(status="new", limit=self.limit, oldest_first=True)
        for post in posts:  # с самых старых: хвост очереди не должен застаиваться
            decision = await self._process(post, stop_words, criteria, report)
            if decision is None:
                break
            report.decisions.append(decision)
        return report

    async def _process(self, post: StoredPost, stop_words: list[str], criteria: str,
                       report: ProcessReport) -> Decision | None:
        """None — круг надо прервать (квота или неверные настройки)."""
        text = "\n".join(part for part in (post.text, post.shared_text) if part)
        if word := filters.match(text, stop_words):
            return await self._decide(post, "filtered", f"стоп-слово «{word}»")
        if len(significant(text)) < MIN_TEXT:
            return await self._decide(post, "skipped", "нечего пересказывать: пустой или почти пустой текст")

        source = await self._source_name(post)
        try:
            verdict = await self.gemini.summarize(text, source=source, criteria=criteria)
        except GeminiQuotaExhausted as e:
            return await self._quota_exhausted(e, report)
        except GeminiBadRequest as e:
            report.alerts.append(f"🚨 Gemini не принимает запрос: {e}\nПроверь ключ и название модели "
                                 f"(актуальный список — команда models). Разбор остановлен.")
            return None
        except GeminiBlocked as e:
            return await self._decide(post, "failed", f"модель не дала ответ: {e}")
        except GeminiRetryable as e:
            return await self._postpone(post, e, report)
        except GeminiError as e:
            return await self._decide(post, "failed", f"ошибка разбора ответа: {e}")

        if verdict.skip or not verdict.post:
            note = "Gemini: не для канала" if verdict.skip else "Gemini: пустой пересказ"
            return await self._decide(post, "skipped", note, verdict=verdict)

        draft_id = await self.db.add_draft(post_id=post.post_id, summary=verdict.post,
                                           summary_ru=verdict.post_ru or None,
                                           summary_ua=verdict.post_ua or None,
                                           summary_en=verdict.post_en or None, facts=verdict.facts,
                                           model=verdict.model, now=self.clock())
        decision = await self._decide(post, "pending", None, verdict=verdict)
        decision.draft_id = draft_id
        # медиа качаем сейчас, а не при публикации: ссылки Facebook протухают за дни,
        # а черновик может столько ждать решения
        if self.store and post.media:
            decision.media = await self.store.fetch(draft_id, post.media)
        await self.db.log("drafted", post_id=post.post_id, now=self.clock())
        return decision

    async def _quota_exhausted(self, error: GeminiQuotaExhausted, report: ProcessReport) -> None:
        """Суточная квота модели кончилась. Счётчик у каждой модели свой, поэтому сначала
        берём следующую из списка и только когда он кончится — ждём полуночи."""
        current = getattr(self.gemini, "model", "")
        following = next_model(current)
        if self.gemini is not None and following and following != current:
            self._preferred_model = self._preferred_model or current
            self.gemini.model = following
            await self.db.set_setting("gemini_model", following)
            log.warning("квота модели %s исчерпана, перехожу на %s", current, following)
            report.alerts.append(f"♻️ {error}\nПерехожу на <b>{following}</b> — у неё отдельная "
                                 f"суточная квота. Вернусь на {self._preferred_model} после сброса.")
            return None
        self._paused_until = report.paused_until = error.reset_at
        report.alerts.append(
            f"⏸ {error} Запасные модели тоже исчерпаны. Разбор встал до сброса квоты; "
            f"собирать записи продолжаю, они подождут в очереди.")
        return None

    async def _restore_model(self, report: ProcessReport) -> None:
        """Квоты обновились — возвращаемся на модель, с которой начинали."""
        if self.gemini is None or not self._preferred_model:
            return
        self.gemini.model = self._preferred_model
        await self.db.set_setting("gemini_model", self._preferred_model)
        report.alerts.append(f"↩️ Вернулся на модель {self._preferred_model}.")
        self._preferred_model = None

    async def _postpone(self, post: StoredPost, error: GeminiRetryable,
                        report: ProcessReport) -> Decision | None:
        """Временный сбой модели: запись не виновата, она остаётся в очереди и ждёт следующего
        круга. Но вечно ждать нельзя — иначе одна «ядовитая» запись затыкает очередь целиком."""
        attempts = await self.db.bump_attempts(post.post_id, now=self.clock())
        if attempts >= MAX_ATTEMPTS:
            report.alerts.append(f"🚨 Запись не разобралась с {attempts} попыток, помечаю сломанной:\n"
                                 f"{post.permalink}\n{error}")
            return await self._decide(post, "failed", f"{attempts} неудачных попыток: {error}")
        log.info("временный сбой модели (попытка %d из %d), запись остаётся в очереди: %s",
                 attempts, MAX_ATTEMPTS, error)
        return None

    async def _decide(self, post: StoredPost, status: str, note: str | None, *,
                      verdict: Verdict | None = None) -> Decision:
        await self.db.set_post_status(post.post_id, status, note=note, expect=["new"], now=self.clock())
        if status in ("filtered", "skipped", "failed"):
            await self.db.log(status, group_id=post.group_id, post_id=post.post_id, now=self.clock())
        return Decision(post, status, note, verdict=verdict)

    async def _source_name(self, post: StoredPost) -> str:
        if post.group_id is None:
            return ""
        group = await self.db.get_group(post.group_id)
        return group.title if group else ""


async def rewrite_draft(db: Database, gemini: Summarizer, draft_id: int, *, instruction: str = "",
                        store: MediaStore | None = None,
                        clock: Callable[[], float] = time.time) -> int | None:
    """Переписать черновик: новая версия — новая строка, старая помечается superseded.
    None — модель на этот раз решила, что запись каналу не подходит."""
    draft = await db.get_draft(draft_id)
    post = await db.get_post(draft.post_id) if draft else None
    if draft is None or post is None or draft.status != "pending":
        return None
    group = await db.get_group(post.group_id) if post.group_id else None
    text = "\n".join(part for part in (post.text, post.shared_text) if part)
    criteria = await db.get_setting("relevance", DEFAULT_CRITERIA)
    verdict = await gemini.summarize(text, source=group.title if group else "", criteria=criteria,
                                     extra=instruction)
    if verdict.skip or not verdict.post:
        return None
    new_id = await db.add_draft(post_id=post.post_id, summary=verdict.post,
                                summary_ru=verdict.post_ru or None,
                                summary_ua=verdict.post_ua or None,
                                summary_en=verdict.post_en or None, facts=verdict.facts,
                                model=verdict.model, now=clock())
    await db.set_draft_status(draft_id, "superseded", expect=["pending"], now=clock())
    if store and post.media:          # файлы лежат под старым id черновика — переносим на новый
        await store.fetch(new_id, post.media)
        store.clear(draft_id)
    return new_id


async def current_model(db: Database, default: str = DEFAULT_MODEL) -> str:
    """Модель из настроек (правится из бота), иначе значение по умолчанию из .env."""
    return await db.get_setting("gemini_model", default)
