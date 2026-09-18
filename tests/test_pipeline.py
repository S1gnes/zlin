"""Конвейер обработки: стоп-слова -> Gemini -> черновик."""
import time

import pytest

from zlinbot import filters
from zlinbot.db import Database
from zlinbot.gemini import (FALLBACK_MODELS, GeminiBadRequest, GeminiBlocked, GeminiError,
                            GeminiQuotaExhausted, GeminiRetryable, Verdict)
from zlinbot.pipeline import MAX_ATTEMPTS, MAX_PER_ROUND, Processor

T0 = 1_789_000_000
NEWS = "Uzavírka na třídě Tomáše Bati potrvá od 20. září do 30. října, objízdná trasa vede přes Kvítkovou."
KEEP = Verdict(False, "Uzavírka potrvá do 30. října.", "Перекрытие продлится до 30 октября.",
               "Перекриття триватиме до 30 жовтня.", "The closure lasts until 30 October.",
               "🚧", ("od 20. září", "třída Tomáše Bati"), model="gemini-2.5-flash")


class FakeGemini:
    """Отдаёт заготовленные вердикты или поднимает заготовленные исключения."""

    def __init__(self, *answers, model: str = "gemini-2.5-flash") -> None:
        self.answers = list(answers)
        self.model = model
        self.prompts: list[tuple[str, str, str]] = []

    async def summarize(self, text, *, source="", criteria=""):
        self.prompts.append((text, source, criteria))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
async def db(tmp_path):
    async with Database(tmp_path / "t.db") as database:
        await database.add_group(kind="rss", url="https://zlin.cz/feed/", slug="https://zlin.cz/feed/",
                                 fb_id=None, name="ZLIN.CZ", now=T0)
        yield database


async def add_post(db: Database, post_id: str, text: str, *, seen: int = T0, status: str = "new") -> None:
    await db.insert_post(post_id=post_id, group_id=1, permalink=f"https://zlin.cz/{post_id}", author="Redakce",
                         text=text, shared_text=None, media=[], created_at=seen, status=status,
                         text_hash=None, now=seen)


def clock():
    return T0


# -- отсев до Gemini -----------------------------------------------------------

async def test_stop_word_filters_before_gemini(db):
    await db.add_filter("Prodám")
    await add_post(db, "p1", "PRODÁM kolo Author, cena 4 500 Kč, Zlín")
    g = FakeGemini(KEEP)
    report = await Processor(db, g, clock=clock).run()
    assert [d.status for d in report.decisions] == ["filtered"]
    assert "prodam" in report.decisions[0].note
    assert g.prompts == []                                        # квота не потрачена
    assert (await db.get_post("p1")).status == "filtered"
    assert (await db.event_counts(0)).get("filtered") == 1


async def test_thin_text_is_skipped_without_gemini(db):
    await add_post(db, "p1", "Fotogalerie")
    g = FakeGemini(KEEP)
    report = await Processor(db, g, clock=clock).run()
    assert report.decisions[0].status == "skipped" and g.prompts == []


def test_stop_words_ignore_case_and_diacritics():
    assert filters.match("Prodáme dva lístky", ["prodam"]) == "prodam"
    assert filters.match("Hledám práci", ["prodam"]) is None
    assert filters.is_valid("ab") is False and filters.is_valid("kolo") is True


# -- решения Gemini ------------------------------------------------------------

async def test_keep_creates_draft(db):
    await add_post(db, "p1", NEWS)
    report = await Processor(db, FakeGemini(KEEP), clock=clock).run()
    decision = report.decisions[0]
    assert decision.status == "pending" and decision.draft_id == 1
    assert (await db.get_post("p1")).status == "pending"
    [draft] = await db.drafts()
    assert draft.summary == KEEP.post and draft.summary_ru == KEEP.post_ru
    assert draft.summary_ua == KEEP.post_ua and draft.summary_en == KEEP.post_en
    assert draft.emoji == "🚧"
    assert draft.facts == list(KEEP.facts) and draft.model == "gemini-2.5-flash"
    assert (await db.event_counts(0)).get("drafted") == 1


async def test_skip_verdict_marks_post(db):
    await add_post(db, "p1", NEWS)
    report = await Processor(db, FakeGemini(Verdict(True)), clock=clock).run()
    assert report.decisions[0].status == "skipped"
    assert (await db.get_post("p1")).note == "Gemini: не для канала"
    assert await db.drafts() == []


async def test_source_name_and_criteria_reach_the_model(db):
    await db.set_setting("relevance", "Только перекрытия дорог")
    await add_post(db, "p1", NEWS)
    g = FakeGemini(KEEP)
    await Processor(db, g, clock=clock).run()
    text, source, criteria = g.prompts[0]
    assert source == "ZLIN.CZ" and criteria == "Только перекрытия дорог" and text == NEWS


# -- сбои ----------------------------------------------------------------------

async def test_blocked_answer_marks_one_post_and_goes_on(db):
    await add_post(db, "p1", NEWS, seen=T0)
    await add_post(db, "p2", NEWS + " Druhá zpráva o dopravě ve městě.", seen=T0 + 10)
    g = FakeGemini(GeminiBlocked("ответ заблокирован фильтром: SAFETY"), KEEP)
    report = await Processor(db, g, clock=clock).run()
    assert [d.status for d in report.decisions] == ["failed", "pending"]
    assert (await db.get_post("p1")).status == "failed"
    assert (await db.event_counts(0)).get("failed") == 1


async def test_garbage_json_marks_post_failed(db):
    await add_post(db, "p1", NEWS)
    report = await Processor(db, FakeGemini(GeminiError("в ответе нет JSON")), clock=clock).run()
    assert report.decisions[0].status == "failed" and "нет JSON" in report.decisions[0].note


async def test_temporary_failure_keeps_the_post_in_the_queue(db):
    """Перегрузка модели и минутный лимит — не вина записи. Раньше после трёх ретраев
    запись помечалась «сломано» навсегда; теперь она ждёт следующего круга."""
    await add_post(db, "p1", NEWS, seen=T0)
    processor = Processor(db, FakeGemini(GeminiRetryable("503: high demand")), clock=clock)
    report = await processor.run()

    assert report.decisions == [] and report.paused_until is None
    post = await db.get_post("p1")
    assert post.status == "new" and post.attempts == 1

    processor.gemini = FakeGemini(KEEP)                            # модель ожила
    report = await processor.run()
    assert report.count("pending") == 1


async def test_endless_temporary_failures_eventually_bury_the_post(db):
    """Обратная сторона: одна «ядовитая» запись не должна затыкать очередь навсегда."""
    await add_post(db, "p1", NEWS, seen=T0)
    processor = Processor(db, FakeGemini(GeminiRetryable("503: high demand")), clock=clock)
    for _ in range(MAX_ATTEMPTS - 1):
        assert await processor.run() and (await db.get_post("p1")).status == "new"

    report = await processor.run()
    assert report.decisions[0].status == "failed"
    assert f"{MAX_ATTEMPTS} неудачных попыток" in report.decisions[0].note
    assert "🚨" in report.alerts[0]


async def test_daily_quota_switches_to_the_next_model(db):
    """У каждой модели свой суточный счётчик, поэтому ложиться до полуночи рано:
    сначала берём следующую модель списка."""
    await add_post(db, "p1", NEWS, seen=T0)
    gem = FakeGemini(GeminiQuotaExhausted("квота исчерпана", T0 + 3600), model=FALLBACK_MODELS[0])
    processor = Processor(db, gem, clock=clock)
    report = await processor.run()

    assert report.paused_until is None                             # не встали, а переехали
    assert gem.model == FALLBACK_MODELS[1]
    assert await db.get_setting("gemini_model") == FALLBACK_MODELS[1]   # переживёт перезапуск
    assert (await db.get_post("p1")).status == "new"               # запись ждёт в очереди
    assert "♻️" in report.alerts[0]


async def test_after_the_chain_ends_we_wait_and_come_back_to_the_first_model(db):
    await add_post(db, "p1", NEWS, seen=T0)
    reset = T0 + 3600
    gem = FakeGemini(GeminiQuotaExhausted("квота исчерпана", reset), model=FALLBACK_MODELS[0])
    processor = Processor(db, gem, clock=clock)
    for _ in range(len(FALLBACK_MODELS) - 1):                      # проходим список до конца
        await processor.run()
    assert gem.model == FALLBACK_MODELS[-1]

    report = await processor.run()                                 # запасных больше нет
    assert report.paused_until == reset and "⏸" in report.alerts[0]

    processor.clock = lambda: reset + 1                            # квоты обновились у всех
    processor.gemini = gem = FakeGemini(KEEP, model=FALLBACK_MODELS[-1])
    report = await processor.run()
    assert gem.model == FALLBACK_MODELS[0]                         # вернулись на основную
    assert await db.get_setting("gemini_model") == FALLBACK_MODELS[0]
    assert report.count("pending") == 1


async def test_daily_quota_stops_the_round_and_keeps_posts_queued(db):
    await add_post(db, "p1", NEWS, seen=T0)
    await add_post(db, "p2", NEWS + " Jiná zpráva.", seen=T0 + 10)
    reset = T0 + 3600
    processor = Processor(db, FakeGemini(GeminiQuotaExhausted("квота исчерпана", reset),
                                         model=FALLBACK_MODELS[-1]), clock=clock)
    report = await processor.run()
    assert report.decisions == [] and report.paused_until == reset
    assert "⏸" in report.alerts[0]
    assert (await db.get_post("p1")).status == "new"               # записи ждут в очереди

    report = await processor.run()                                  # до сброса не дёргаем модель совсем
    assert report.decisions == [] and report.alerts == []

    processor.clock = lambda: reset + 1                             # квота обновилась
    processor.gemini = FakeGemini(KEEP)
    report = await processor.run()
    assert "✅" in report.alerts[0] and report.count("pending") == 2


async def test_bad_request_stops_the_round(db):
    await add_post(db, "p1", NEWS)
    report = await Processor(db, FakeGemini(GeminiBadRequest("400: model not found")), clock=clock).run()
    assert report.decisions == [] and "Проверь ключ" in report.alerts[0]
    assert (await db.get_post("p1")).status == "new"


# -- очередь -------------------------------------------------------------------

async def test_no_more_than_eight_per_round_oldest_first(db):
    for i in range(12):
        await add_post(db, f"p{i:02d}", f"{NEWS} Zpráva číslo {i}.", seen=T0 + i)
    g = FakeGemini(KEEP)
    report = await Processor(db, g, clock=clock).run()
    assert len(report.decisions) == MAX_PER_ROUND == 8
    assert [d.post.post_id for d in report.decisions][:3] == ["p00", "p01", "p02"]
    assert (await db.post_status_counts())["new"] == 4


async def test_already_processed_posts_are_left_alone(db):
    await add_post(db, "p1", NEWS, status="published")
    report = await Processor(db, FakeGemini(KEEP), clock=clock).run()
    assert report.decisions == []
    assert (await db.get_post("p1")).status == "published"


async def test_without_key_processor_does_nothing(db):
    await add_post(db, "p1", NEWS)
    report = await Processor(db, None, clock=time.time).run()
    assert report.decisions == [] and (await db.get_post("p1")).status == "new"
