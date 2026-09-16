"""
Скачивание медиа для черновика и подготовка к отправке.

Границы Bot API заложены сразу, потому что упираемся мы в них, а не в свою фантазию:
media group — максимум 10 файлов, фото до 10 МБ, видео до 50 МБ.

Качаем в момент создания черновика, а не публикации: ссылки Facebook подписаны и
протухают за несколько дней, а черновик столько может пролежать в очереди.

HLS/DASH (.m3u8/.mpd) не трогаем: склейка сегментов — это отдельный проект, а не
«перенести картинку». Такое видео пропускаем, ссылка на оригинал в посте остаётся.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

log = logging.getLogger(__name__)

PHOTO_LIMIT = 10 * 1024 * 1024        # Bot API: фото до 10 МБ
VIDEO_LIMIT = 50 * 1024 * 1024        # Bot API: видео до 50 МБ
MAX_GROUP = 10                        # Bot API: media group — не больше 10 файлов
TIMEOUT = 60.0
STREAM_MARKERS = (".m3u8", ".mpd")    # потоковые плейлисты, а не файлы
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
USER_AGENT = "zlinbot/0.1 (aggregator for a personal Telegram channel)"


@dataclass(frozen=True, slots=True)
class LocalMedia:
    kind: str          # photo / video
    path: Path
    size: int
    source_url: str


@dataclass(frozen=True, slots=True)
class DownloadReport:
    files: tuple[LocalMedia, ...] = ()
    skipped: tuple[str, ...] = ()     # причины пропуска — их видно в карточке черновика

    @property
    def ok(self) -> bool:
        return bool(self.files)


def limit_for(kind: str) -> int:
    return VIDEO_LIMIT if kind == "video" else PHOTO_LIMIT


class MediaStore:
    def __init__(self, root: Path, *, client: httpx.AsyncClient | None = None) -> None:
        self.root = Path(root)
        self._own = client is None
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                                   headers={"User-Agent": USER_AGENT})

    async def __aenter__(self) -> MediaStore:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own:
            await self._client.aclose()

    def dir_for(self, draft_id: int) -> Path:
        return self.root / f"draft_{draft_id}"

    def files(self, draft_id: int) -> list[Path]:
        folder = self.dir_for(draft_id)
        return sorted(folder.iterdir()) if folder.is_dir() else []

    def local(self, draft_id: int) -> list[LocalMedia]:
        """Что уже скачано для черновика. Вид файла восстанавливаем из имени: «01-photo-....jpg»."""
        out: list[LocalMedia] = []
        for path in self.files(draft_id):
            parts = path.name.split("-", 2)
            kind = parts[1] if len(parts) > 2 and parts[1] in ("photo", "video") else "photo"
            out.append(LocalMedia(kind, path, path.stat().st_size, ""))
        return out

    async def fetch(self, draft_id: int, media: list[dict[str, str]]) -> DownloadReport:
        files: list[LocalMedia] = []
        skipped: list[str] = []
        folder = self.dir_for(draft_id)
        for index, item in enumerate(media):
            if len(files) >= MAX_GROUP:
                skipped.append(f"сверх {MAX_GROUP} файлов media group: {len(media) - MAX_GROUP} шт.")
                break
            kind, url = item.get("kind", "photo"), item.get("url", "")
            if kind == "video" and any(marker in url.lower() for marker in STREAM_MARKERS):
                skipped.append("видео отдаётся потоком (HLS/DASH), а не файлом — осталась ссылка на оригинал")
                continue
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{index:02d}-{kind}-{_safe_name(url)}"
            try:
                size = await self._download(url, target, limit_for(kind))
            except _TooBig as e:
                skipped.append(f"{'видео' if kind == 'video' else 'фото'} больше лимита Bot API ({e})")
                target.unlink(missing_ok=True)
            except (httpx.HTTPError, OSError) as e:
                log.warning("медиа не скачалось: %s (%s)", url, e)
                skipped.append(f"не скачалось: {type(e).__name__}")
                target.unlink(missing_ok=True)
            else:
                files.append(LocalMedia(kind, target, size, url))
        return DownloadReport(tuple(files), tuple(skipped))

    async def _download(self, url: str, target: Path, limit: int) -> int:
        async with self._client.stream("GET", url) as response:
            response.raise_for_status()
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > limit:
                raise _TooBig(f"{declared / 1024 / 1024:.1f} МБ")
            size = 0
            with target.open("wb") as fh:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > limit:          # сервер мог не прислать Content-Length
                        raise _TooBig(f"больше {limit / 1024 / 1024:.0f} МБ")
                    fh.write(chunk)
        return size

    def clear(self, draft_id: int) -> int:
        """Решение по черновику принято — файлы больше не нужны."""
        folder = self.dir_for(draft_id)
        if not folder.is_dir():
            return 0
        removed = 0
        for path in folder.iterdir():
            path.unlink(missing_ok=True)
            removed += 1
        folder.rmdir()
        return removed

    def purge_older_than(self, seconds: float, *, now: float | None = None) -> int:
        """Подмести за черновиками, по которым решение так и не приняли."""
        if not self.root.is_dir():
            return 0
        deadline = (now or time.time()) - seconds
        removed = 0
        for folder in self.root.iterdir():
            if not folder.is_dir() or not folder.name.startswith("draft_"):
                continue
            if folder.stat().st_mtime >= deadline:
                continue
            try:
                removed += self.clear(int(folder.name.removeprefix("draft_")))
            except ValueError:
                log.warning("в кэше медиа лежит чужая папка: %s", folder)
        return removed


class _TooBig(Exception):
    pass


def _safe_name(url: str) -> str:
    name = unquote(Path(urlparse(url).path).name) or "file"
    return _SAFE_NAME_RE.sub("_", name)[-60:]
