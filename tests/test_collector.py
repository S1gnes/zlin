"""Круг сбора: база нового источника, дедупликация, здоровье — с поддельными источниками."""
import random

import pytest

from zlinbot import collector as col
from zlinbot.collector import Collector
from zlinbot.db import Database
from zlinbot.fb.extract import Activity, FeedResult, Media, Post
from zlinbot.fb.scraper import ScrapeResult
from zlinbot.rss import FeedItem, FetchResult, ParsedFeed

T0 = 1_789_000_000
AD = "Prodám kolo Author 26 palců, výborný stav, cena 4 500 Kč, Zlín-Jižní Svahy"


def post(pid: str, text: str = "", *, gid: str = "555", shared: str | None = None,
         created: int | None = None) -> Post:
    return Post(post_id=f"{gid}:{pid}", group_id=gid, pid=pid,
                permalink=f"https://www.facebook.com/groups/{gid}/posts/{pid}/",
                text=text or f"Unikátní text {pid} " * 3, shared_text=shared, author="A", group_name="G",
                media=(), created_at=created, created_label=None, truncated=False)


def ok(slug: str, *posts: Post, fb_id: str = "555", name: str = "Skupina") -> ScrapeResult:
    return ScrapeResult(slug, "ok", FeedResult(True, name, fb_id, len(posts), 0, 0, tuple(posts)))


def fail(slug: str, status: str = "no_feed") -> ScrapeResult:
    feed = None if status in ("login_wall", "error") else FeedResult(status != "no_feed", None, None, 0, 0, 0, ())
    return ScrapeResult(slug, status, feed)


def item(n: str, text: str = "", *, created: int | None = None, media: tuple = ()) -> FeedItem:
    return FeedItem(post_id=f"rss:{n}", permalink=f"https://zlin.cz/zpravy/{n}/",
                    text=text or f"Zpráva {n} s dostatečně dlouhým textem pro hash {n}", title=f"Zpráva {n}",
                    author="Redakce", media=media, created_at=created)


def feed_ok(url: str, *items: FeedItem, name: str = "ZLIN.CZ", etag: str | None = 'W/"1"') -> FetchResult:
    return FetchResult(url, "ok", ParsedFeed(name, items), etag=etag, last_modified=None)


class FakeScraper:
    """Очередь результатов на источник; когда она пуста — повторяется последний выданный."""

    def __init__(self) -> None:
        self.results: dict[str, list] = {}
        self.last: dict[str, object] = {}
        self.activity: dict[str, Activity] = {}
        self.calls: list[str] = []

    def queue(self, key: str, *results) -> None:
        self.results.setdefault(key, []).extend(results)

    def _take(self, key: str):
        self.calls.append(key)
        if self.results.get(key):
            self.last[key] = self.results[key].pop(0)
        return self.last[key]

    async def fetch_group(self, slug, *, group_id=None):
        return self._take(slug)

    async def fetch_activity(self, slug):
        self.calls.append(f"about:{slug}")
        return self.activity.get(slug)


class FakeFeeds(FakeScraper):
    def __init__(self) -> None:
        super().__init__()
        self.conditional: list[tuple[str, str | None]] = []

    async def fetch(self, url, *, etag=None, last_modified=None):
        self.conditional.append((url, etag))
        return self._take(url)


class Clock:
    def __init__(self) -> None:
        self.t = float(T0)

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def env(tmp_path):
    async with Database(tmp_path / "t.db") as db:
        fs, feeds, clock = FakeScraper(), FakeFeeds(), Clock()
        yield db, fs, feeds, clock, Collector(db, fs, feeds, clock=clock, rng=random.Random(1))


async def add_fb(c: Collector, fs: FakeScraper, slug: str, fb_id: str, *visible: Post):
    fs.queue(slug, ok(slug, *visible, fb_id=fb_id, name=slug.upper()))
    res = await c.add_source(f"https://www.facebook.com/groups/{slug}/")
    assert res.ok, res.reason
    return res.group


async def add_rss(c: Collector, feeds: FakeFeeds, url: str, *visible: FeedItem):
    feeds.queue(url, feed_ok(url, *visible))
    res = await c.add_source(url)
    assert res.ok, res.reason
    return res.group


# -- добавление ----------------------------------------------------------------

async def test_add_fb_group_baselines_visible_posts(env):
    db, fs, feeds, clock, c = env
    g = await add_fb(c, fs, "zlin.a", "555", post("1", created=T0 - 60))
    assert (g.kind, g.fb_id, g.name, g.status) == ("fb", "555", "ZLIN.A", "active")
    assert (await db.get_post("555:1")).status == "seen"
    assert await db.event_counts(0) == {}  # база не считается «собранным»


async def test_add_rss_feed(env):
    db, fs, feeds, clock, c = env
    g = await add_rss(c, feeds, "https://zlin.cz/feed/", item("a", created=T0 - 60), item("b"))
    assert (g.kind, g.name, g.etag, g.status) == ("rss", "ZLIN.CZ", 'W/"1"', "active")
    assert [p.status for p in await db.posts(limit=10)] == ["seen", "seen"]
    assert (await db.get_post("rss:a")).permalink == "https://zlin.cz/zpravy/a/"


async def test_add_rejects_unreadable_sources(env):
    db, fs, feeds, clock, c = env
    fs.queue("zavrena", fail("zavrena", "no_feed"))
    assert "не читается" in (await c.add_source("https://www.facebook.com/groups/zavrena/")).reason
    feeds.queue("https://nic.cz/feed", FetchResult("https://nic.cz/feed", "error", error="HTTP 404"))
    assert "HTTP 404" in (await c.add_source("https://nic.cz/feed")).reason
    assert "не на группу" in (await c.add_source("https://www.facebook.com/zpravodajstvi.zlin.cz")).reason
    assert "Не похоже" in (await c.add_source("зляйн")).reason
    assert await db.list_groups() == []


async def test_add_rejects_duplicates(env):
    db, fs, feeds, clock, c = env
    await add_fb(c, fs, "zlin.a", "555")
    assert "уже добавлена" in (await c.add_source("https://m.facebook.com/groups/ZLIN.A/?ref=x")).reason
    fs.queue("555", ok("555", fb_id="555"))  # та же группа по числовой ссылке — узнаём по fb_id
    assert "под другой ссылкой" in (await c.add_source("https://www.facebook.com/groups/555/")).reason
    await add_rss(c, feeds, "https://zlin.cz/feed/", item("a"))
    assert "уже добавлена" in (await c.add_source("https://zlin.cz/feed/")).reason
    assert len(await db.list_groups()) == 2


# -- дедупликация --------------------------------------------------------------

async def test_new_known_and_cross_group_duplicate(env):
    db, fs, feeds, clock, c = env
    await add_fb(c, fs, "zlin.a", "555")
    await add_fb(c, fs, "zlin.b", "777")
    clock.t += 600
    fs.queue("zlin.a", ok("zlin.a", post("10", AD, created=int(clock.t) - 60)))
    fs.queue("zlin.b", ok("zlin.b", post("20", AD.upper() + " 👍", gid="777", created=int(clock.t) - 30), fb_id="777"))
    report = await c.run_round()
    assert report.new_posts == 1
    assert (await db.get_post("555:10")).status == "new"
    dup = await db.get_post("777:20")
    assert (dup.status, dup.dup_of) == ("duplicate", "555:10")

    clock.t += 1200  # тот же пост снова в ленте — первый уровень, ничего нового
    report = await c.run_round()
    assert report.new_posts == 0 and report.outcomes[0].counts["known"] == 1
    assert await db.event_counts(0) == {"collected": 1, "duplicate": 1}


async def test_duplicate_between_facebook_and_rss(env):
    db, fs, feeds, clock, c = env
    await add_fb(c, fs, "zlin.a", "555")
    url = "https://zlin.cz/feed/"
    await add_rss(c, feeds, url, item("old"))
    # общий префикс должен быть длиннее 100 значащих символов — хеш считается именно по ним
    news = ("Výměna trolejových sloupů omezí provoz v Podvesné až do konce října, hlásí dopravní podnik. "
            "Objízdná trasa povede přes Kvítkovou, autobusy linky 123 pojedou po náhradní trase.")
    clock.t += 600
    fs.queue("zlin.a", ok("zlin.a", post("10", news, created=int(clock.t))))
    feeds.queue(url, feed_ok(url, item("new", news + " (zdroj: DSZO)", created=int(clock.t))))
    await c.run_round()
    assert (await db.get_post("555:10")).status == "new"
    rss_post = await db.get_post("rss:new")
    assert (rss_post.status, rss_post.dup_of) == ("duplicate", "555:10")


async def test_short_texts_are_not_hash_duplicates(env):
    db, fs, feeds, clock, c = env
    await add_fb(c, fs, "zlin.a", "555")
    clock.t += 600
    fs.queue("zlin.a", ok("zlin.a", post("1", "Neviděl někdo psa?", created=int(clock.t)),
                          post("2", "Neviděl někdo psa?", created=int(clock.t))))
    await c.run_round()
    assert {(await db.get_post(f"555:{i}")).status for i in (1, 2)} == {"new"}


async def test_old_items_are_not_processed(env):
    db, fs, feeds, clock, c = env
    g = await add_fb(c, fs, "zlin.a", "555")
    clock.t += 5 * 86400
    fs.queue("zlin.a", ok("zlin.a", post("1", created=g.added_at - 3600),      # до добавления источника
                          post("2", created=int(clock.t) - 4 * 86400),         # старше 3 суток
                          post("3", created=None)))                            # дата неизвестна — новая
    await c.run_round()
    assert [(await db.get_post(f"555:{i}")).status for i in (1, 2, 3)] == ["seen", "seen", "new"]


# -- RSS -----------------------------------------------------------------------

async def test_rss_conditional_request_and_not_modified(env):
    db, fs, feeds, clock, c = env
    url = "https://zlin.cz/feed/"
    await add_rss(c, feeds, url, item("a"))
    feeds.queue(url, FetchResult(url, "not_modified", etag='W/"1"'))
    clock.t += 1200
    r = await c.run_round()
    assert feeds.conditional[-1] == (url, 'W/"1"')          # ETag ушёл в запрос
    assert r.outcomes[0].result.ok and r.new_posts == 0     # 304 — это «читается», просто без новостей
    assert not r.global_failure

    feeds.queue(url, feed_ok(url, item("b", created=int(clock.t)), etag='W/"2"'))
    clock.t += 1200
    await c.run_round()
    assert (await db.get_post("rss:b")).status == "new"
    assert (await db.get_group(1)).etag == 'W/"2"'          # новый ETag сохранён


async def test_rss_media_and_fields_are_stored(env):
    db, fs, feeds, clock, c = env
    url = "https://zlin.cz/feed/"
    await add_rss(c, feeds, url)
    clock.t += 600
    photo = Media("photo", "https://zlin.cz/wp-content/uploads/2026/09/a.jpg")
    feeds.queue(url, feed_ok(url, item("c", created=int(clock.t), media=(photo,))))
    await c.run_round()
    p = await db.get_post("rss:c")
    assert p.media == [{"kind": "photo", "url": photo.url}] and p.author == "Redakce"
    assert p.permalink == "https://zlin.cz/zpravy/c/"


# -- здоровье источников -------------------------------------------------------

async def test_facebook_block_does_not_kill_groups_while_rss_alive(env):
    """Главный случай сентября 2026: FB под стеной логина, RSS работает."""
    db, fs, feeds, clock, c = env
    a = await add_fb(c, fs, "zlin.a", "555")
    url = "https://zlin.cz/feed/"
    await add_rss(c, feeds, url, item("a"))
    fs.queue("zlin.a", fail("zlin.a", "login_wall"))
    feeds.queue(url, feed_ok(url, item("b", created=T0 + 100)))

    r = await c.run_round()
    assert r.global_failure and r.failed_kinds == ["fb"]
    assert "Facebook требует вход" in r.alerts[0]
    assert (await db.get_group(a.id)).status == "active" and (await db.get_group(a.id)).fail_streak == 0
    assert (await db.get_post("rss:b")).status == "new"     # RSS тем временем работает

    for _ in range(2):  # пока идёт пауза, FB не трогаем, а RSS ходит каждый круг
        clock.t += 1200
        fs.calls.clear()
        r = await c.run_round()
        assert "zlin.a" not in fs.calls and r.skipped_kinds == ["fb"]
        assert [o.group.kind for o in r.outcomes] == ["rss"]
        assert r.alerts == []                                # тревога была один раз

    clock.t += col.COOLDOWN_MAX          # пауза вышла — FB пробуем снова, снова мимо
    fs.calls.clear()
    r = await c.run_round()
    assert "zlin.a" in fs.calls and r.global_failure and r.alerts == []
    group = await db.get_group(a.id)      # и всё равно не наказан: он не виноват
    assert (group.status, group.fail_streak) == ("active", 0)


async def test_cooldown_expires_and_facebook_recovers(env):
    db, fs, feeds, clock, c = env
    a = await add_fb(c, fs, "zlin.a", "555")
    fs.queue("zlin.a", fail("zlin.a", "no_feed"))
    r = await c.run_round()
    assert r.global_failure and "стена логина" in r.alerts[0]

    clock.t += col.COOLDOWN_MAX          # пауза вышла — пробуем снова, снова мимо: пауза растёт
    r = await c.run_round()
    assert r.global_failure and r.alerts == []
    fs.results["zlin.a"] = [ok("zlin.a")]
    clock.t += col.COOLDOWN_MAX
    r = await c.run_round()
    assert not r.global_failure and r.alerts == ["✅ Facebook снова читается."]
    assert (await db.get_group(a.id)).fail_streak == 0


async def test_single_source_failing_while_others_ok_becomes_unavailable(env):
    db, fs, feeds, clock, c = env
    a = await add_fb(c, fs, "zlin.a", "555")
    await add_fb(c, fs, "zlin.b", "777")
    fs.queue("zlin.a", fail("zlin.a", "no_feed"))
    fs.queue("zlin.b", ok("zlin.b", fb_id="777"))
    for _ in range(col.UNAVAILABLE_AFTER - 1):
        clock.t += 1200
        assert not (await c.run_round()).alerts
    clock.t += 1200
    r = await c.run_round()
    assert r.outcomes[0].became_unavailable and "unavailable" in r.alerts[0]
    assert (await db.get_group(a.id)).status == "unavailable"

    fs.calls.clear()  # недоступный источник не трогаем до суточной перепроверки
    clock.t += 1200
    await c.run_round()
    assert "zlin.a" not in fs.calls

    fs.results["zlin.a"] = [ok("zlin.a")]
    clock.t += col.RECHECK_UNAVAILABLE
    r = await c.run_round()
    assert r.outcomes[0].recovered and "снова читается" in r.alerts[0]
    assert (await db.get_group(a.id)).status == "active"


async def test_structural_change_alerts_immediately_once(env):
    db, fs, feeds, clock, c = env
    await add_fb(c, fs, "zlin.a", "555")
    await add_fb(c, fs, "zlin.b", "777")
    fs.queue("zlin.a", fail("zlin.a", "no_ids"))
    fs.queue("zlin.b", ok("zlin.b", fb_id="777"))
    r1 = await c.run_round()
    r2 = await c.run_round()
    assert "ID не находятся" in r1.alerts[0] and r2.alerts == []


async def test_paused_source_is_skipped(env):
    db, fs, feeds, clock, c = env
    a = await add_fb(c, fs, "zlin.a", "555")
    await db.update_group(a.id, status="paused")
    fs.calls.clear()
    r = await c.run_round()
    assert fs.calls == [] and not r.global_failure


async def test_activity_sampled_twice_a_day(env):
    db, fs, feeds, clock, c = env
    a = await add_fb(c, fs, "zlin.a", "555")
    fs.activity["zlin.a"] = Activity(today=19, month=577)
    for _ in range(3):
        await c.run_round()
        clock.t += 1200
    clock.t += col.ACTIVITY_EVERY
    await c.run_round()
    assert [t for _, t in await db.activity(a.id, 0)] == [19, 19]


async def test_check_group_bypasses_cooldown_and_does_not_count_failures(env):
    db, fs, feeds, clock, c = env
    a = await add_fb(c, fs, "zlin.a", "555")
    fs.queue("zlin.a", fail("zlin.a"))
    await c.run_round()                       # глобальный сбой -> пауза для fb
    fs.results["zlin.a"] = [ok("zlin.a")]
    o = await c.check_group(a.id)             # «проверить сейчас» идёт в обход паузы
    assert o.result.ok and (await db.get_group(a.id)).fail_streak == 0
