"""
Присмотр за фоновыми задачами: сбор, разбор, ежедневная сводка.

Две вещи, ради которых это отдельный модуль:

1. Упавшая задача не умирает молча. Исключение логируется, уходит владельцу в личку
   и задача перезапускается с нарастающей паузой — иначе канал просто затихает, а
   почему, выясняется через сутки.

2. Расписание считается по настенным часам, а не «поспать N секунд». Ноутбук засыпает,
   и таймер во сне не идёт: проснувшись, круг должен пойти сразу, а не через остаток
   паузы. Поэтому ждём короткими отрезками и каждый раз сверяемся с часами. Заодно так
   виден сам факт сна — о нём стоит сказать, потому что всё это время канал молчал.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

TICK = 30.0                      # шаг ожидания: чем меньше, тем быстрее замечаем пробуждение
SLEEP_GAP = 10 * 60              # разрыв в часах больше этого — машина спала или её усыпили
RESTART_DELAYS = (30.0, 120.0, 600.0)   # пауза после падения, дальше по последнему значению


@dataclass
class Task:
    name: str
    run: Callable[[], Awaitable[float | None]]   # одна итерация; вернуть — когда повторить
    interval: float                               # если итерация ничего не вернула
    next_at: float = 0.0
    failures: int = 0
    runs: int = 0


@dataclass
class Supervisor:
    notify: Callable[[str], Awaitable[None]]
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    tick: float = TICK
    tasks: list[Task] = field(default_factory=list)
    _last_tick: float | None = None

    def add(self, name: str, run: Callable[[], Awaitable[float | None]], interval: float,
            *, first_delay: float = 0.0) -> None:
        self.tasks.append(Task(name, run, interval, next_at=self.clock() + first_delay))

    async def run_forever(self) -> None:
        while True:
            await self._step()
            await self.sleep(self.tick)

    async def _step(self) -> None:
        now = self.clock()
        await self._notice_sleep(now)
        for task in self.tasks:
            if now < task.next_at:
                continue
            task.next_at = await self._run_task(task, now)

    async def _run_task(self, task: Task, now: float) -> float:
        try:
            asked = await task.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — ради этого модуль и нужен
            task.failures += 1
            delay = RESTART_DELAYS[min(task.failures, len(RESTART_DELAYS)) - 1]
            log.exception("фоновая задача «%s» упала", task.name)
            await self._say(f"🚨 Фоновая задача «{task.name}» упала: {type(e).__name__}: {e}\n"
                            f"Это {task.failures}-й сбой подряд, перезапускаю через "
                            f"{delay / 60:.0f} мин. Подробности в логе.")
            return self.clock() + delay
        if task.failures:
            await self._say(f"✅ Задача «{task.name}» снова работает (после {task.failures} сбоев).")
        task.failures = 0
        task.runs += 1
        return self.clock() + (asked if asked is not None else task.interval)

    async def _notice_sleep(self, now: float) -> None:
        """Между тиками прошло куда больше, чем мы спали, — машина отключалась."""
        previous, self._last_tick = self._last_tick, now
        if previous is None:
            return
        gap = now - previous
        if gap > SLEEP_GAP:
            log.warning("похоже, машина спала %.0f мин", gap / 60)
            await self._say(f"😴 Машина не работала около {gap / 60:.0f} мин — всё это время я ничего "
                            f"не собирал и не публиковал. Догоняю сейчас.")

    async def _say(self, text: str) -> None:
        try:
            await self.notify(text)
        except Exception:  # noqa: BLE001 — не смогли написать в личку, это не повод падать
            log.exception("не удалось отправить сообщение владельцу")
