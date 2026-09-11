"""Круг сбора: база новой группы, дедупликация, здоровье групп — с поддельным скрапером."""
import random

import pytest

from zlinbot import collector as col
from zlinbot.collector import Collector
from zlinbot.db import Database
from zlinbot.fb.extract import Activity, FeedResult, Post
from zlinbot.fb.scraper import ScrapeResult

T0 = 1_789_000_000
AD = "Prodám kolo Author 26 palců, výborný stav, cena 4 500 Kč, Zlín-Jižní Svahy"


def post(pid: str, text: str = "", *, gid: str = "555", shared: str | None = None,
         created: int | None = None) -> Post:
    return Post(post_id=f"{gid}:{pid}", group_id=gid, pid=pid,
                permalink=f"https://www.facebook.com/groups/{gid}/posts/{pid}/", text=text or f"Unikátní text {pid} " * 3,
                shared_text=shared, author="A", group_name="G", media=(), created_at=created,
                created_label=None, truncated=False)


def ok(slug: str, *posts: Post, fb_id: str = "555", name: str = "Skupina") -> ScrapeResult:
    return ScrapeResult(slug, "ok", FeedResult(True, name, fb_id, len(posts), 0, 0, tuple(posts)))


def fail(slug: str, status: str = "no_feed") -> ScrapeResult:
    feed = None if status in ("login_wall", "error") else FeedResult(status != "no_feed", None, None, 0, 0, 0, ())
    return ScrapeResult(slug, status, feed)


class FakeScraper:
    """Очередь результатов на группу; когда она пуста — повторяется последний выданный."""

    def __init__(self) -> None:
        self.results: dict[str, list[ScrapeResult]] = {}
        self.last: dict[str, ScrapeResult] = {}
        self.activity: dict[str, Activity] = {}
        self.calls: list[str] = []

    def queue(self, slug: str, *results: ScrapeResult) -> None:
        self.results.setdefault(slug, []).extend(results)

    async def fetch_group(self, slug, *, group_id=None):
        self.calls.append(slug)
        if self.results.get(slug):
            self.last[slug] = self.results[slug].pop(0)
        return self.last[slug]

    async def fetch_activity(self, slug):
        self.calls.append(f"about:{slug}")
        return self.activity.get(slug)


class Clock:
    def __init__(self) -> None:
        self.t = float(T0)

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def env(tmp_path):
    async with Database(tmp_path / "t.db") as db:
        fs, clock = FakeScraper(), Clock()
        yield db, fs, clock, Collector(db, fs, clock=clock, rng=random.Random(1))


async def add(c: Collector, fs: FakeScraper, slug: str, fb_id: str, *visible: Post):
    fs.queue(slug, ok(slug, *visible, fb_id=fb_id, name=slug.upper()))
    res = await c.add_group(f"https://www.facebook.com/groups/{slug}/")
    assert res.ok, res.reason
    return res.group


# -- добавление ----------------------------------------------------------------

async def test_add_group_baselines_visible_posts(env):
    db, fs, clock, c = env
    g = await add(c, fs, "zlin.a", "555", post("1", created=T0 - 60))
    assert (g.fb_id, g.name, g.status) == ("555", "ZLIN.A", "active")
    assert (await db.get_post("555:1")).status == "seen"
    assert await db.event_counts(0) == {}  # база не считается «собранным»


async def test_add_rejects_unreadable_group(env):
    db, fs, clock, c = env
    fs.queue("zavrena", fail("zavrena", "no_feed"))
    res = await c.add_group("https://www.facebook.com/groups/zavrena/")
    assert not res.ok and "не читается" in res.reason
    assert await db.list_groups() == []


async def test_add_rejects_page_and_duplicates(env):
    db, fs, clock, c = env
    res = await c.add_group("https://www.facebook.com/zpravodajstvi.zlin.cz")
    assert not res.ok and "Page" in res.reason
    await add(c, fs, "zlin.a", "555")
    assert "уже добавлена" in (await c.add_group("https://m.facebook.com/groups/ZLIN.A/?ref=x")).reason
    fs.queue("555", ok("555", fb_id="555"))  # та же группа по числовой ссылке — узнаём по fb_id
    res = await c.add_group("https://www.facebook.com/groups/555/")
    assert not res.ok and "под другой ссылкой" in res.reason
    assert len(await db.list_groups()) == 1


# -- дедупликация --------------------------------------------------------------

async def test_new_known_and_cross_group_duplicate(env):
    db, fs, clock, c = env
    await add(c, fs, "zlin.a", "555")
    await add(c, fs, "zlin.b", "777")
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


async def test_duplicate_of_baseline_post(env):
    db, fs, clock, c = env
    await add(c, fs, "zlin.a", "555", post("1", AD, created=T0 - 60))
    await add(c, fs, "zlin.b", "777")
    clock.t += 600
    fs.queue("zlin.b", ok("zlin.b", post("2", AD, gid="777", created=int(clock.t)), fb_id="777"))
    fs.queue("zlin.a", ok("zlin.a", post("1", AD)))
    await c.run_round()
    assert (await db.get_post("777:2")).dup_of == "555:1"


async def test_short_texts_are_not_hash_duplicates(env):
    db, fs, clock, c = env
    await add(c, fs, "zlin.a", "555")
    clock.t += 600
    fs.queue("zlin.a", ok("zlin.a", post("1", "Neviděl někdo psa?", created=int(clock.t)),
                          post("2", "Neviděl někdo psa?", created=int(clock.t))))
    await c.run_round()
    assert {(await db.get_post(f"555:{i}")).status for i in (1, 2)} == {"new"}


async def test_old_posts_are_not_processed(env):
    db, fs, clock, c = env
    g = await add(c, fs, "zlin.a", "555")
    clock.t += 5 * 86400
    fs.queue("zlin.a", ok("zlin.a", post("1", created=g.added_at - 3600),       # до добавления группы
                          post("2", created=int(clock.t) - 4 * 86400),          # старше 3 суток
                          post("3", created=None)))                             # дата неизвестна — новый
    await c.run_round()
    assert [(await db.get_post(f"555:{i}")).status for i in (1, 2, 3)] == ["seen", "seen", "new"]


# -- здоровье групп ------------------------------------------------------------

async def test_global_failure_does_not_punish_groups(env):
    db, fs, clock, c = env
    await add(c, fs, "zlin.a", "555")
    await add(c, fs, "zlin.b", "777")
    fs.queue("zlin.a", fail("zlin.a"))
    fs.queue("zlin.b", fail("zlin.b"))
    delays, alerts = [], []
    for _ in range(5):
        clock.t += 1200
        r = await c.run_round()
        assert r.global_failure
        delays.append(r.next_delay)
        alerts.append(r.alerts)
    assert "Ни одна группа" in alerts[0][0]
    assert all(a == [] for a in alerts[1:])  # тревога — один раз, а не каждый круг
    assert all(g.status == "active" and g.fail_streak == 0 for g in await db.list_groups())
    assert delays[0] > col.ROUND_INTERVAL[1] and delays[-1] == col.BACKOFF_MAX  # пауза растёт до потолка


async def test_global_alert_text_and_recovery(env):
    db, fs, clock, c = env
    await add(c, fs, "zlin.a", "555")
    fs.queue("zlin.a", fail("zlin.a", "no_ids"), ok("zlin.a"))
    r = await c.run_round()
    assert "сменил вёрстку" in r.alerts[0]
    r = await c.run_round()
    assert not r.global_failure and r.alerts == ["✅ Facebook снова читается (после 1 неудачных кругов подряд)."]
    assert r.next_delay <= col.ROUND_INTERVAL[1]


async def test_single_group_failing_while_others_ok_becomes_unavailable(env):
    db, fs, clock, c = env
    a = await add(c, fs, "zlin.a", "555")
    await add(c, fs, "zlin.b", "777")
    fs.queue("zlin.a", fail("zlin.a", "no_feed"))
    fs.queue("zlin.b", ok("zlin.b", fb_id="777"))
    for _ in range(col.UNAVAILABLE_AFTER - 1):
        clock.t += 1200
        r = await c.run_round()
        assert not r.alerts
    clock.t += 1200
    r = await c.run_round()
    assert r.outcomes[0].became_unavailable and "unavailable" in r.alerts[0]
    assert (await db.get_group(a.id)).status == "unavailable"

    fs.calls.clear()  # недоступную не трогаем до суточной перепроверки
    clock.t += 1200
    await c.run_round()
    assert "zlin.a" not in fs.calls

    fs.results["zlin.a"] = [ok("zlin.a")]
    clock.t += col.RECHECK_UNAVAILABLE
    r = await c.run_round()
    assert r.outcomes[0].recovered and "снова читается" in r.alerts[0]
    assert (await db.get_group(a.id)).status == "active"


async def test_structural_change_alerts_immediately_once(env):
    db, fs, clock, c = env
    await add(c, fs, "zlin.a", "555")
    await add(c, fs, "zlin.b", "777")
    fs.queue("zlin.a", fail("zlin.a", "no_ids"))
    fs.queue("zlin.b", ok("zlin.b", fb_id="777"))
    r1 = await c.run_round()
    r2 = await c.run_round()
    assert "ID не находятся" in r1.alerts[0] and r2.alerts == []


async def test_paused_group_is_skipped(env):
    db, fs, clock, c = env
    a = await add(c, fs, "zlin.a", "555")
    await db.update_group(a.id, status="paused")
    fs.calls.clear()
    r = await c.run_round()
    assert fs.calls == [] and not r.global_failure


async def test_activity_sampled_at_most_hourly(env):
    db, fs, clock, c = env
    a = await add(c, fs, "zlin.a", "555")
    fs.activity["zlin.a"] = Activity(today=19, month=577)
    for _ in range(3):  # три круга за 40 минут — один замер
        await c.run_round()
        clock.t += 1200
    clock.t += 3600
    await c.run_round()
    assert [t for _, t in await db.activity(a.id, 0)] == [19, 19]


async def test_check_group_recovers_but_does_not_count_failures(env):
    db, fs, clock, c = env
    a = await add(c, fs, "zlin.a", "555")
    fs.queue("zlin.a", fail("zlin.a"))
    await c.check_group(a.id)
    assert (await db.get_group(a.id)).fail_streak == 0
    await db.update_group(a.id, status="unavailable")
    fs.results["zlin.a"] = [ok("zlin.a")]
    o = await c.check_group(a.id)
    assert o.recovered and (await db.get_group(a.id)).status == "active"
