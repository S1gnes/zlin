"""Межпроцессный замок темпа: две «копии» не грузят FB одновременно и держат паузу."""
import asyncio
import time

from zlinbot.fb.pace import PaceLock


async def test_two_holders_never_overlap_and_keep_pause(tmp_path):
    # два экземпляра с отдельными файловыми дескрипторами — как два процесса
    path = tmp_path / "fb.lock"
    a, b = PaceLock(path, (0.3, 0.3), poll=0.02), PaceLock(path, (0.3, 0.3), poll=0.02)
    spans: list[tuple[float, float]] = []

    async def load(lock: PaceLock) -> None:
        async with lock:
            start = time.monotonic()
            await asyncio.sleep(0.15)  # «загрузка страницы»
            spans.append((start, time.monotonic()))

    await asyncio.gather(load(a), load(b))
    (s1, e1), (s2, e2) = sorted(spans)
    assert s2 >= e1 + 0.25  # второй начал только после первого + пауза


async def test_first_load_does_not_wait(tmp_path):
    t = time.monotonic()
    async with PaceLock(tmp_path / "fb.lock", (5, 5)):
        pass
    assert time.monotonic() - t < 1


async def test_pause_counts_from_last_load_of_anyone(tmp_path):
    path = tmp_path / "fb.lock"
    async with PaceLock(path, (0, 0)):
        pass
    t = time.monotonic()
    async with PaceLock(path, (0.4, 0.4)):  # новый экземпляр видит время чужой загрузки
        pass
    assert time.monotonic() - t >= 0.35
