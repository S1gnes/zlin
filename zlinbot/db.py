"""
SQLite через aiosqlite: схема, миграции и все запросы. Единственное место, где есть SQL.

Миграции — список скриптов, номер применённого хранится в PRAGMA user_version.
Схему меняем только добавлением нового элемента в MIGRATIONS, старые не трогаем.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any

import aiosqlite

from .config import TZ
from .textnorm import fold

# Статусы поста. ТЗ требует различать шесть; «seen», «duplicate» и «failed» — наши добавки.
POST_STATUSES = (
    "seen",       # увиден, но не обрабатывается: база при добавлении группы или всплыл старый пост
    "new",        # ещё не обработан
    "duplicate",  # тот же текст уже был (обычно — то же объявление в другой группе)
    "filtered",   # отсеян стоп-словами
    "skipped",    # Gemini сказал skip
    "pending",    # черновик ждёт решения
    "published",  # опубликован
    "rejected",   # отклонён
    "failed",     # обработка сломалась безвозвратно (см. note)
)
GROUP_STATUSES = ("active", "paused", "unavailable")

_V1 = f"""
CREATE TABLE groups (
    id              INTEGER PRIMARY KEY,
    kind            TEXT    NOT NULL DEFAULT 'fb' CHECK (kind IN ('fb', 'rss')),
    url             TEXT    NOT NULL,
    slug            TEXT    NOT NULL,          -- slug/ID из ссылки FB (или URL фида для RSS)
    fb_id           TEXT,                      -- канонический числовой ID группы FB
    name            TEXT,
    status          TEXT    NOT NULL DEFAULT 'active' CHECK (status IN {GROUP_STATUSES}),
    fail_streak     INTEGER NOT NULL DEFAULT 0, -- провалов подряд (только в кругах, где другие читались)
    last_status     TEXT,                      -- ok / no_feed / no_ids / ... последней проверки
    last_checked_at INTEGER,
    last_ok_at      INTEGER,
    added_at        INTEGER NOT NULL
);
CREATE UNIQUE INDEX groups_kind_slug ON groups(kind, slug COLLATE NOCASE);
CREATE UNIQUE INDEX groups_fb_id ON groups(fb_id) WHERE fb_id IS NOT NULL;

CREATE TABLE posts (
    post_id     TEXT    PRIMARY KEY,           -- "gid:pid" — ключ навсегда
    group_id    INTEGER REFERENCES groups(id) ON DELETE SET NULL,
    permalink   TEXT    NOT NULL,
    author      TEXT,
    text        TEXT    NOT NULL DEFAULT '',
    shared_text TEXT,
    media_json  TEXT    NOT NULL DEFAULT '[]', -- [{{"kind": "photo", "url": "..."}}]
    created_at  INTEGER,                       -- время поста по данным источника
    seen_at     INTEGER NOT NULL,              -- когда мы увидели его впервые
    text_hash   TEXT,                          -- dedup.post_hash; NULL — текст слишком короткий
    dup_of      TEXT,                          -- для duplicate: post_id оригинала
    status      TEXT    NOT NULL CHECK (status IN {POST_STATUSES}),
    status_at   INTEGER NOT NULL,
    note        TEXT                           -- причина статуса: стоп-слово, ошибка и т.п.
);
CREATE INDEX posts_status ON posts(status, seen_at);
CREATE INDEX posts_hash ON posts(text_hash) WHERE text_hash IS NOT NULL;
CREATE INDEX posts_group_created ON posts(group_id, created_at);

CREATE TABLE drafts (
    id             INTEGER PRIMARY KEY,        -- именно он идёт в callback_data (лимит 64 байта)
    post_id        TEXT    NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    summary        TEXT    NOT NULL,           -- пересказ по-чешски
    summary_ru     TEXT,                       -- перевод для комментария
    facts_json     TEXT    NOT NULL DEFAULT '[]',
    model          TEXT,
    status         TEXT    NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'publishing', 'published', 'rejected', 'superseded')),
    admin_msg_id   INTEGER,
    channel_msg_id INTEGER,
    created_at     INTEGER NOT NULL,
    decided_at     INTEGER
);
CREATE INDEX drafts_status ON drafts(status, created_at);

CREATE TABLE filters (
    id       INTEGER PRIMARY KEY,
    word     TEXT    NOT NULL,                 -- как ввёл пользователь
    norm     TEXT    NOT NULL UNIQUE,          -- textnorm.fold(word) — по нему и сравниваем
    added_at INTEGER NOT NULL
);

CREATE TABLE stats (                           -- журнал событий: статусы постов меняются, события — нет
    id       INTEGER PRIMARY KEY,
    ts       INTEGER NOT NULL,
    event    TEXT    NOT NULL,                 -- collected / duplicate / old / filtered / skipped / ...
    group_id INTEGER,                          -- без FK: статистика переживает удаление группы
    post_id  TEXT
);
CREATE INDEX stats_ts ON stats(ts, event);

CREATE TABLE activity (                        -- «Dnes N nových příspěvků» со страницы /about
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    ts       INTEGER NOT NULL,
    day      TEXT    NOT NULL,                 -- YYYY-MM-DD по пражскому времени
    today    INTEGER,
    month    INTEGER,
    PRIMARY KEY (group_id, ts)
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                        -- JSON
);
"""

# v2: условные запросы для RSS — не тянуть фид целиком, если он не менялся
_V2 = """
ALTER TABLE groups ADD COLUMN etag TEXT;
ALTER TABLE groups ADD COLUMN last_modified TEXT;
"""

MIGRATIONS: tuple[str, ...] = (_V1, _V2)

_GROUP_COLUMNS = frozenset({"url", "slug", "fb_id", "name", "status", "fail_streak", "last_status",
                            "last_checked_at", "last_ok_at", "etag", "last_modified"})


@dataclass(frozen=True, slots=True)
class Group:
    id: int
    kind: str
    url: str
    slug: str
    fb_id: str | None
    name: str | None
    status: str
    fail_streak: int
    last_status: str | None
    last_checked_at: int | None
    last_ok_at: int | None
    added_at: int
    etag: str | None = None          # RSS: условный запрос
    last_modified: str | None = None

    @property
    def title(self) -> str:
        return self.name or self.slug


@dataclass(frozen=True, slots=True)
class StoredPost:
    post_id: str
    group_id: int | None
    permalink: str
    author: str | None
    text: str
    shared_text: str | None
    media: list[dict[str, str]]
    created_at: int | None
    seen_at: int
    text_hash: str | None
    dup_of: str | None
    status: str
    status_at: int
    note: str | None


def day_bounds(day: date) -> tuple[int, int]:
    """Начало и конец суток по пражскому времени в unix-секундах."""
    start = datetime.combine(day, dtime.min, tzinfo=TZ)
    end = datetime.combine(date.fromordinal(day.toordinal() + 1), dtime.min, tzinfo=TZ)
    return int(start.timestamp()), int(end.timestamp())


def local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).date().isoformat()


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> Database:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._migrate()
        return self

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> Database:
        return await self.connect()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() не вызван")
        return self._conn

    async def _migrate(self) -> None:
        version = await self._scalar("PRAGMA user_version")
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            await self.conn.executescript(script)
            await self.conn.execute(f"PRAGMA user_version = {number}")
            await self.conn.commit()

    async def schema_version(self) -> int:
        return await self._scalar("PRAGMA user_version")

    # -- утилиты -------------------------------------------------------------

    async def _scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        async with self.conn.execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    async def _write(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        cur = await self.conn.execute(sql, tuple(params))
        await self.conn.commit()
        return cur

    # -- группы --------------------------------------------------------------

    async def add_group(self, *, kind: str, url: str, slug: str, fb_id: str | None, name: str | None,
                        now: float, etag: str | None = None, last_modified: str | None = None) -> Group:
        cur = await self._write(
            "INSERT INTO groups (kind, url, slug, fb_id, name, status, last_status, last_checked_at, "
            "last_ok_at, added_at, etag, last_modified) VALUES (?, ?, ?, ?, ?, 'active', 'ok', ?, ?, ?, ?, ?)",
            (kind, url, slug, fb_id, name, int(now), int(now), int(now), etag, last_modified))
        group = await self.get_group(cur.lastrowid)
        assert group is not None
        return group

    async def get_group(self, group_id: int) -> Group | None:
        rows = await self._rows("SELECT * FROM groups WHERE id = ?", (group_id,))
        return Group(**dict(rows[0])) if rows else None

    async def find_group(self, kind: str, *, slug: str | None = None, fb_id: str | None = None) -> Group | None:
        rows = await self._rows(
            "SELECT * FROM groups WHERE kind = ? AND ((? IS NOT NULL AND slug = ? COLLATE NOCASE) "
            "OR (? IS NOT NULL AND fb_id = ?)) LIMIT 1", (kind, slug, slug, fb_id, fb_id))
        return Group(**dict(rows[0])) if rows else None

    async def list_groups(self, statuses: Iterable[str] | None = None) -> list[Group]:
        if statuses is None:
            rows = await self._rows("SELECT * FROM groups ORDER BY id")
        else:
            st = tuple(statuses)
            rows = await self._rows(f"SELECT * FROM groups WHERE status IN ({','.join('?' * len(st))}) ORDER BY id", st)
        return [Group(**dict(r)) for r in rows]

    async def update_group(self, group_id: int, **fields: Any) -> None:
        bad = set(fields) - _GROUP_COLUMNS
        if bad:
            raise ValueError(f"нельзя обновить колонки groups: {bad}")
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            await self._write(f"UPDATE groups SET {sets} WHERE id = ?", (*fields.values(), group_id))

    async def delete_group(self, group_id: int) -> bool:
        """Посты группы остаются (group_id -> NULL): их ID и хеши продолжают защищать от дублей."""
        return (await self._write("DELETE FROM groups WHERE id = ?", (group_id,))).rowcount == 1

    # -- посты ---------------------------------------------------------------

    async def known_post(self, post_id: str) -> bool:
        return await self._scalar("SELECT 1 FROM posts WHERE post_id = ?", (post_id,)) is not None

    async def find_by_hash(self, text_hash: str) -> str | None:
        return await self._scalar(
            "SELECT post_id FROM posts WHERE text_hash = ? ORDER BY seen_at, post_id LIMIT 1", (text_hash,))

    async def insert_post(self, *, post_id: str, group_id: int | None, permalink: str, author: str | None,
                          text: str, shared_text: str | None, media: list[dict[str, str]],
                          created_at: int | None, status: str, text_hash: str | None,
                          dup_of: str | None = None, note: str | None = None, now: float) -> bool:
        """False — такой post_id уже есть (первый уровень дедупликации).
        Не «INSERT OR IGNORE»: тот молча глотает и нарушение CHECK (неверный статус) — только ON CONFLICT по ключу."""
        cur = await self._write(
            "INSERT INTO posts (post_id, group_id, permalink, author, text, shared_text, media_json, "
            "created_at, seen_at, text_hash, dup_of, status, status_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(post_id) DO NOTHING",
            (post_id, group_id, permalink, author, text, shared_text, json.dumps(media, ensure_ascii=False),
             created_at, int(now), text_hash, dup_of, status, int(now), note))
        return cur.rowcount == 1

    async def set_post_status(self, post_id: str, status: str, *, note: str | None = None,
                              expect: Iterable[str] | None = None, now: float | None = None) -> bool:
        """Сменить статус. expect — атомарная проверка текущего статуса (защита от двойного нажатия)."""
        params: list[Any] = [status, note, int(now or time.time()), post_id]
        sql = "UPDATE posts SET status = ?, note = ?, status_at = ? WHERE post_id = ?"
        if expect is not None:
            ex = tuple(expect)
            sql += f" AND status IN ({','.join('?' * len(ex))})"
            params += ex
        return (await self._write(sql, params)).rowcount == 1

    async def get_post(self, post_id: str) -> StoredPost | None:
        rows = await self._rows("SELECT * FROM posts WHERE post_id = ?", (post_id,))
        return _post(rows[0]) if rows else None

    async def posts(self, *, status: str | None = None, group_id: int | None = None,
                    limit: int = 20) -> list[StoredPost]:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if group_id is not None:
            where.append("group_id = ?")
            params.append(group_id)
        sql = "SELECT * FROM posts" + (f" WHERE {' AND '.join(where)}" if where else "")
        rows = await self._rows(sql + " ORDER BY seen_at DESC, post_id LIMIT ?", (*params, limit))
        return [_post(r) for r in rows]

    async def post_status_counts(self) -> dict[str, int]:
        rows = await self._rows("SELECT status, COUNT(*) FROM posts GROUP BY status")
        return {r[0]: r[1] for r in rows}

    # -- статистика ----------------------------------------------------------

    async def log(self, event: str, *, group_id: int | None = None, post_id: str | None = None,
                  now: float | None = None) -> None:
        await self._write("INSERT INTO stats (ts, event, group_id, post_id) VALUES (?, ?, ?, ?)",
                          (int(now or time.time()), event, group_id, post_id))

    async def event_counts(self, since: float) -> dict[str, int]:
        rows = await self._rows("SELECT event, COUNT(*) FROM stats WHERE ts >= ? GROUP BY event", (int(since),))
        return {r[0]: r[1] for r in rows}

    # -- активность групп (метрика покрытия) ---------------------------------

    async def add_activity(self, group_id: int, *, today: int | None, month: int | None, now: float) -> None:
        await self._write("INSERT INTO activity (group_id, ts, day, today, month) VALUES (?, ?, ?, ?, ?) "
                          "ON CONFLICT(group_id, ts) DO UPDATE SET today = excluded.today, month = excluded.month",
                          (group_id, int(now), local_day(now), today, month))

    async def last_activity_ts(self, group_id: int) -> int | None:
        return await self._scalar("SELECT MAX(ts) FROM activity WHERE group_id = ?", (group_id,))

    async def activity(self, group_id: int, since: float) -> list[tuple[int, int | None]]:
        rows = await self._rows("SELECT ts, today FROM activity WHERE group_id = ? AND ts >= ? ORDER BY ts",
                                (group_id, int(since)))
        return [(r[0], r[1]) for r in rows]

    async def posts_created_between(self, group_id: int, start: float, end: float) -> int:
        return await self._scalar(
            "SELECT COUNT(*) FROM posts WHERE group_id = ? AND created_at >= ? AND created_at < ?",
            (group_id, int(start), int(end)))

    # -- стоп-слова ----------------------------------------------------------

    async def add_filter(self, word: str, *, now: float | None = None) -> bool:
        """False — такое слово (без учёта регистра и диакритики) уже есть или пустое."""
        norm = fold(word).strip()
        if not norm:
            return False
        cur = await self._write("INSERT INTO filters (word, norm, added_at) VALUES (?, ?, ?) "
                                "ON CONFLICT(norm) DO NOTHING",
                                (word.strip(), norm, int(now or time.time())))
        return cur.rowcount == 1

    async def remove_filter(self, filter_id: int) -> bool:
        return (await self._write("DELETE FROM filters WHERE id = ?", (filter_id,))).rowcount == 1

    async def list_filters(self) -> list[tuple[int, str, str]]:
        rows = await self._rows("SELECT id, word, norm FROM filters ORDER BY norm")
        return [(r[0], r[1], r[2]) for r in rows]

    # -- настройки -----------------------------------------------------------

    async def get_setting(self, key: str, default: Any = None) -> Any:
        raw = await self._scalar("SELECT value FROM settings WHERE key = ?", (key,))
        return default if raw is None else json.loads(raw)

    async def set_setting(self, key: str, value: Any) -> None:
        await self._write("INSERT INTO settings (key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                          (key, json.dumps(value, ensure_ascii=False)))


def _post(row: aiosqlite.Row) -> StoredPost:
    d = dict(row)
    d["media"] = json.loads(d.pop("media_json") or "[]")
    return StoredPost(**d)
