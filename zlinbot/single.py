"""
Замок одного экземпляра.

Два запущенных бота молча конфликтуют: Telegram отдаёт обновления только одному
(«Conflict: terminated by other getUpdates request»), и в итоге не отвечает ни один.
Ловится это плохо — бот вроде запущен, а команды не работают.

Замок файловый, с блокировкой ОС: если процесс убили, ядро снимет её само, и мёртвый
замок никого не заблокирует.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import IO

from .fb.pace import _try_lock, _unlock  # noqa: PLC2701 — одна и та же блокировка ОС

log = logging.getLogger(__name__)

DEFAULT_PATH = Path(tempfile.gettempdir()) / "zlinbot-instance.lock"


class SingleInstance:
    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = path
        # PID лежит рядом, а не в самом замке: заблокированный байт не прочитать
        # даже своему же процессу через другой дескриптор.
        self.pid_path = path.with_name(path.name + ".pid")
        self._fh: IO[bytes] | None = None

    def acquire(self) -> bool:
        """False — этот бот уже где-то запущен."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")  # noqa: SIM115 — держим открытым, пока живём
        if not _try_lock(fh):
            fh.close()
            return False
        self._fh = fh
        self.pid_path.write_text(str(os.getpid()), encoding="ascii")
        return True

    def release(self) -> None:
        if self._fh is not None:
            _unlock(self._fh)
            self._fh.close()
            self._fh = None
            self.pid_path.unlink(missing_ok=True)

    def owner_pid(self) -> int | None:
        """Чей это замок — чтобы сказать человеку, кого закрывать."""
        try:
            return int(self.pid_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    def __enter__(self) -> SingleInstance:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
