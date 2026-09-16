"""Присмотр за фоновыми задачами, замок одного экземпляра и ежедневная сводка."""
from datetime import datetime

import pytest

from zlinbot import digest
from zlinbot.config import TZ
from zlinbot.db import Database
from zlinbot.single import SingleInstance
from zlinbot.supervisor import RESTART_DELAYS, SLEEP_GAP, Supervisor

T0 = 1_789_000_000


class Clock:
    def __init__(self) -> None:
        self.t = float(T0)

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def env():
    clock, said = Clock(), []

    async def notify(text: str) -> None:
        said.append(text)

    async def sleep(seconds: float) -> None:      # «спим», двигая часы
        clock.t += seconds

    return clock, said, Supervisor(notify=notify, clock=clock, sleep=sleep, tick=30.0)


# -- расписание ----------------------------------------------------------------

async def test_task_runs_on_schedule_not_every_tick(env):
    clock, said, sup = env
    runs = []

    async def task():
        runs.append(clock.t)

    sup.add("сбор", task, interval=600)
    for _ in range(41):                            # 41 тик по 30 с — ровно до 20-й минуты
        await sup._step()
        clock.t += 30
    assert len(runs) == 3                          # старт, +10 мин, +20 мин
    assert said == []


async def test_task_can_ask_for_its_own_next_delay(env):
    clock, said, sup = env
    delays = iter([60.0, 900.0])

    async def task():
        return next(delays)

    sup.add("сбор", task, interval=99999)
    await sup._step()
    clock.t += 60
    await sup._step()                              # пришло время по запрошенной паузе
    assert sup.tasks[0].runs == 2
    clock.t += 60
    await sup._step()                              # вторая пауза больше — не запускаемся
    assert sup.tasks[0].runs == 2


# -- падения -------------------------------------------------------------------

async def test_crash_is_reported_and_task_restarts(env):
    clock, said, sup = env
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("база недоступна")

    sup.add("разбор", flaky, interval=60)
    await sup._step()
    assert "🚨" in said[0] and "разбор" in said[0] and "база недоступна" in said[0]

    clock.t += RESTART_DELAYS[0]                   # пауза после первого падения
    await sup._step()
    assert len(calls) == 2 and len(said) == 2      # снова упало, снова сказали

    clock.t += RESTART_DELAYS[1]
    await sup._step()
    assert len(calls) == 3
    assert "✅" in said[-1] and "снова работает" in said[-1]


async def test_restart_delay_grows(env):
    clock, said, sup = env

    async def always_fails():
        raise RuntimeError("нет сети")

    sup.add("сбор", always_fails, interval=60)
    seen = []
    for expected in RESTART_DELAYS + (RESTART_DELAYS[-1],):
        await sup._step()
        seen.append(sup.tasks[0].next_at - clock.t)
        clock.t += expected
    assert seen == [*RESTART_DELAYS, RESTART_DELAYS[-1]]


async def test_notify_failure_does_not_kill_the_supervisor(env):
    clock, said, sup = env

    async def broken_notify(text: str) -> None:
        raise ConnectionError("телеграм недоступен")

    sup.notify = broken_notify

    async def task():
        raise RuntimeError("что-то")

    sup.add("сбор", task, interval=60)
    await sup._step()                              # не должно выбросить исключение наружу
    assert sup.tasks[0].failures == 1


# -- сон машины ----------------------------------------------------------------

async def test_long_gap_between_ticks_is_reported_as_sleep(env):
    clock, said, sup = env

    async def task():
        return None

    sup.add("сбор", task, interval=600)
    await sup._step()
    clock.t += SLEEP_GAP + 60                      # ноутбук закрыли и открыли через 11 минут
    await sup._step()
    assert "😴" in said[0] and "не работала" in said[0]
    assert sup.tasks[0].runs == 2                  # проснувшись, круг пошёл сразу


# -- замок одного экземпляра ---------------------------------------------------

def test_second_instance_is_refused(tmp_path):
    path = tmp_path / "instance.lock"
    first, second = SingleInstance(path), SingleInstance(path)
    assert first.acquire() is True
    assert second.acquire() is False
    assert first.owner_pid() is not None
    first.release()
    assert second.acquire() is True                 # освободили — можно запускаться
    second.release()


# -- ежедневная сводка ---------------------------------------------------------

def test_digest_time_is_the_next_local_morning():
    evening = datetime(2026, 9, 16, 22, 0, tzinfo=TZ).timestamp()
    assert digest.seconds_until_digest(evening, hour=9) == pytest.approx(11 * 3600)
    early = datetime(2026, 9, 16, 7, 30, tzinfo=TZ).timestamp()
    assert digest.seconds_until_digest(early, hour=9) == pytest.approx(1.5 * 3600)


async def test_digest_reports_numbers_and_silent_sources(tmp_path):
    async with Database(tmp_path / "t.db") as db:
        await db.add_group(kind="rss", url="https://zlin.cz/feed/", slug="https://zlin.cz/feed/",
                           fb_id=None, name="ZLIN.CZ", now=T0)
        await db.add_group(kind="fb", url="https://www.facebook.com/groups/x/", slug="x",
                           fb_id="9", name="Události", now=T0)
        await db.update_group(2, status="unavailable", last_status="login_wall",
                              last_ok_at=T0 - 5 * 86400)
        for event in ("collected", "collected", "published"):
            await db.log(event, now=T0)

        text = await digest.build(db, now=T0 + 60)
    assert "собрано: 2" in text and "опубликовано: 1" in text
    assert "🟢 ZLIN.CZ" in text
    assert "🔴 Události" in text and "Facebook требует вход" in text and "молчит 5 сут." in text


async def test_digest_says_plainly_when_nothing_happened(tmp_path):
    async with Database(tmp_path / "t.db") as db:
        text = await digest.build(db, now=T0)
    assert "ничего не происходило" in text
