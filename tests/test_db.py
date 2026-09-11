"""Схема и запросы БД."""
import sqlite3

import pytest

from zlinbot.db import MIGRATIONS, POST_STATUSES, Database

NOW = 1_789_000_000


@pytest.fixture
async def db(tmp_path):
    async with Database(tmp_path / "t.db") as database:
        yield database


def post_kwargs(post_id="555:1", **over):
    base = dict(post_id=post_id, group_id=None, permalink=f"https://www.facebook.com/groups/g/posts/{post_id}/",
                author="A", text="t", shared_text=None, media=[{"kind": "photo", "url": "https://x/1.jpg"}],
                created_at=NOW - 60, status="new", text_hash=None, now=NOW)
    base.update(over)
    return base


async def test_migrations_applied_once(tmp_path):
    async with Database(tmp_path / "t.db") as db:
        assert await db.schema_version() == len(MIGRATIONS)
    async with Database(tmp_path / "t.db") as db:  # повторное открытие не падает на CREATE TABLE
        assert await db.schema_version() == len(MIGRATIONS)


async def test_group_uniqueness(db):
    await db.add_group(kind="fb", url="u", slug="Zlin.Udalosti", fb_id="1028", name="G", now=NOW)
    with pytest.raises(sqlite3.IntegrityError):  # тот же slug в другом регистре
        await db.add_group(kind="fb", url="u", slug="zlin.udalosti", fb_id=None, name=None, now=NOW)
    with pytest.raises(sqlite3.IntegrityError):  # та же группа под числовой ссылкой
        await db.add_group(kind="fb", url="u", slug="1028", fb_id="1028", name=None, now=NOW)
    assert (await db.find_group("fb", slug="ZLIN.UDALOSTI")).fb_id == "1028"
    assert (await db.find_group("fb", fb_id="1028")).slug == "Zlin.Udalosti"
    assert await db.find_group("fb", slug="jina") is None


async def test_post_id_is_first_level_dedup(db):
    assert await db.insert_post(**post_kwargs())
    assert not await db.insert_post(**post_kwargs(text="другой текст"))
    stored = await db.get_post("555:1")
    assert stored.text == "t" and stored.media == [{"kind": "photo", "url": "https://x/1.jpg"}]


async def test_status_constraint(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.insert_post(**post_kwargs(status="whatever"))
    assert set(POST_STATUSES) >= {"new", "filtered", "skipped", "pending", "published", "rejected"}


async def test_status_transition_is_atomic(db):
    await db.insert_post(**post_kwargs(status="pending"))
    assert await db.set_post_status("555:1", "published", expect=["pending"])
    assert not await db.set_post_status("555:1", "published", expect=["pending"])  # второе нажатие
    assert (await db.get_post("555:1")).status == "published"


async def test_find_by_hash_returns_oldest(db):
    await db.insert_post(**post_kwargs("555:2", text_hash="h", now=NOW + 10))
    await db.insert_post(**post_kwargs("555:1", text_hash="h", now=NOW))
    assert await db.find_by_hash("h") == "555:1"
    assert await db.find_by_hash("nope") is None


async def test_delete_group_keeps_posts(db):
    g = await db.add_group(kind="fb", url="u", slug="s", fb_id="9", name=None, now=NOW)
    await db.insert_post(**post_kwargs(group_id=g.id))
    assert await db.delete_group(g.id)
    assert (await db.get_post("555:1")).group_id is None  # ID поста всё ещё защищает от повтора


async def test_filters_fold_diacritics_and_case(db):
    assert await db.add_filter("Prodám")
    assert not await db.add_filter("prodam")
    assert not await db.add_filter("  ")
    [(fid, word, norm)] = await db.list_filters()
    assert (word, norm) == ("Prodám", "prodam")
    assert await db.remove_filter(fid)


async def test_settings_roundtrip(db):
    assert await db.get_setting("media_mode", "copy") == "copy"
    await db.set_setting("media_mode", "link")
    await db.set_setting("media_mode", "copy")
    assert await db.get_setting("media_mode") == "copy"


async def test_stats_events(db):
    await db.log("collected", now=NOW)
    await db.log("collected", now=NOW)
    await db.log("duplicate", now=NOW - 90_000)
    assert await db.event_counts(NOW - 86_400) == {"collected": 2}
