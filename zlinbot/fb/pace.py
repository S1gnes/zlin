"""
Вежливый темп на уровне машины, а не процесса.

asyncio.Lock в скрапере защищает только внутри одного процесса: если рядом с ботом
запустить scripts/scrape.py или две команды подряд, запросы к FB пошли бы параллельно
или без паузы. Поэтому «одна загрузка за раз» и пауза между загрузками держатся на
файле-замке с блокировкой ОС (снимается сама, если процесс упал), а время последней
загрузки любого процесса хранится рядом.
"""
from __future__ import annotations

import asyncio
import logging
import random
import sys
import tempfile
import time
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)

DEFAULT_LOCK = Path(tempfile.gettempdir()) / "zlinbot-fb.lock"

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fh: IO[bytes]) -> bool:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fh: IO[bytes]) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class PaceLock:
    """async with PaceLock(...): — войти, когда никто не грузит FB и пауза от прошлой загрузки выдержана."""

    def __init__(self, path: Path = DEFAULT_LOCK, pause: tuple[float, float] = (25.0, 70.0),
                 poll: float = 0.5) -> None:
        self.path = path
        self.stamp = path.with_name(path.name + ".last")
        self.pause = pause
        self.poll = poll
        self._fh: IO[bytes] | None = None

    async def __aenter__(self) -> PaceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")  # noqa: SIM115 — закрываем в __aexit__
        waited_for_other = False
        while not _try_lock(fh):
            if not waited_for_other:
                log.info("FB сейчас грузит другой процесс — жду своей очереди")
                waited_for_other = True
            await asyncio.sleep(self.poll)
        self._fh = fh
        wait = random.uniform(*self.pause) - (time.time() - self._last())
        if wait > 0:
            log.info("пауза %.0f с перед следующей загрузкой FB", wait)
            await asyncio.sleep(wait)
        return self

    async def __aexit__(self, *exc: object) -> None:
        try:
            self.stamp.write_text(f"{time.time():.3f}", encoding="ascii")
        finally:
            assert self._fh is not None
            _unlock(self._fh)
            self._fh.close()
            self._fh = None

    def _last(self) -> float:
        try:
            return float(self.stamp.read_text(encoding="ascii"))
        except (OSError, ValueError):
            return 0.0
