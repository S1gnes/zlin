"""Извлечение постов и ID — на сохранённых фикстурах, без живого Facebook."""
from pathlib import Path

import pytest

from zlinbot.fb import extract
from zlinbot.fb import selectors as sel

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Реальная группа (сохранено 2026-09-11 скриптом scripts/scrape.py --save-html)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real() -> extract.FeedResult:
    return extract.extract_feed(load("group_feed_Zlin.Udalosti.html"), group_slug="Zlin.Udalosti")


def test_real_feed_health(real):
    assert real.health == "ok"
    assert real.group_name == "Události Zlín a okolí"
    assert real.group_numeric_id == "1028789523845607"
    # без логина FB рисует 1 пост и 2 пустые заглушки
    assert (real.articles, real.skeletons, real.without_id) == (1, 2, 0)


def test_real_post_id_is_canonical(real):
    [post] = real.posts
    # в ссылке slug "Zlin.Udalosti", а ключ — на числовом ID группы
    assert post.post_id == "1028789523845607:28585844851046703"
    assert post.pid == "28585844851046703"
    assert post.permalink == "https://www.facebook.com/groups/Zlin.Udalosti/posts/28585844851046703/"


def test_real_post_fields(real):
    [post] = real.posts
    assert post.author == "Galerie T 2 Kroměříž"
    assert post.text == "A můžete navštívit i výstavu Všeslovanská epopej Ivana Mládka"
    assert post.shared_text == "Zítra to bude neskutečný. Marek Juras. Olejotisky. Zítra v 17:00"
    assert post.created_at == 1789137366
    assert post.created_label == "2 h"
    assert not post.truncated
    assert [m.kind for m in post.media] == ["photo", "photo"]
    assert all(m.url.startswith("https://scontent") for m in post.media)


def test_real_post_has_no_ui_junk(real):
    [post] = real.posts
    blob = post.text + (post.shared_text or "")
    for junk in ("To se mi líbí", "Komentář", "Sdíleno s:", "·", "Všechny reakce"):
        assert junk not in blob


def test_real_activity():
    act = extract.parse_activity(load("group_about_Zlin.Udalosti.html"))
    assert act == extract.Activity(today=20, month=577)


# ---------------------------------------------------------------------------
# Синтетика: пограничные случаи
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def syn() -> extract.FeedResult:
    return extract.extract_feed(load("synthetic_feed.html"), group_slug="zlin.test")


def by_pid(res: extract.FeedResult, pid: str) -> extract.Post:
    return next(p for p in res.posts if p.pid == pid)


def test_syn_counts(syn):
    assert syn.group_numeric_id == "555"
    assert syn.group_name == "Test Zlín"
    assert syn.skeletons == 1
    assert syn.articles == 8
    assert syn.without_id == 2                     # статьи 6 и 7
    assert [p.pid for p in syn.posts] == ["111", "222", "444", "333", "555000"]  # дубль 111 выкинут


def test_syn_regular_post(syn):
    p = by_pid(syn, "111")
    assert p.post_id == "555:111"
    assert p.permalink == "https://www.facebook.com/groups/zlin.test/posts/111/"  # /permalink/ -> /posts/, без трекинга
    assert p.author == "Jan Novák"                                                # двойной пробел схлопнут
    assert p.text == ("Uzavírka na třídě Tomáše Bati 🚧\n"
                      "Od 15. září do 20. září, objízdná trasa přes Kvítkovou.\n"
                      "Cena parkování 40 Kč/h.")
    assert p.created_at == 1789000000
    assert p.created_label == "Včera v 14:30"
    assert p.shared_text is None


def test_syn_comment_is_ignored(syn):
    p = by_pid(syn, "111")
    assert "Díky za info" not in p.text
    assert all("comment.jpg" not in m.url for m in p.media)


def test_syn_media_skips_blob_video(syn):
    p = by_pid(syn, "111")
    assert p.media == (
        extract.Media("photo", "https://scontent.xx.fbcdn.net/v/photo1.jpg?oe=ABC"),
        extract.Media("video", "https://video.xx.fbcdn.net/v/clip.mp4?oe=DEF"),
    )


def test_syn_share_with_comment(syn):
    p = by_pid(syn, "222")
    assert p.post_id == "555:222"
    assert p.author == "Eva Malá"
    assert p.text == "Sdílím, ať to ví všichni"
    assert p.shared_text == "Ztratil se pes, černý labrador, Zlín-Jižní Svahy"


def test_syn_share_without_own_text(syn):
    p = by_pid(syn, "444")
    assert p.text == ""
    assert p.shared_text == "Oznámení města: odstávka vody 20. září 8:00–14:00"
    assert p.permalink == "https://www.facebook.com/groups/555/posts/444/"


def test_syn_fallback_id_and_truncation(syn):
    p = by_pid(syn, "333")
    assert p.post_id == "555:333"
    assert p.created_label is None
    assert p.truncated
    assert p.text == "Hledám hlídání pro dvě děti, dlouhý text …"


def test_syn_no_data_attributes_fallback(syn):
    p = by_pid(syn, "555000")
    assert p.author == "Marek Dvořák"
    assert p.text == "Na Štípě bude v sobotu farmářský trh.\nZačátek v 8:00."


def test_known_group_id_overrides_page():
    res = extract.extract_feed(load("synthetic_feed.html"), group_slug="zlin.test", group_id="777")
    assert res.posts[0].post_id == "777:111"


# ---------------------------------------------------------------------------
# Сигналы здоровья
# ---------------------------------------------------------------------------

def test_health_no_feed_on_login_wall():
    html = '<html><body><div role="dialog"><form action="/login/"><input name="email"></form></div></body></html>'
    assert extract.extract_feed(html).health == "no_feed"


def test_health_no_articles():
    assert extract.extract_feed('<div role="feed"><div>…</div></div>').health == "no_articles"


def test_health_no_ids():
    html = '<div role="feed"><div role="article"><div data-ad-preview="message">text</div></div></div>'
    res = extract.extract_feed(html, group_slug="x")
    assert (res.health, res.without_id) == ("no_ids", 1)


# ---------------------------------------------------------------------------
# Разбор ссылок
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url, slug", [
    ("https://www.facebook.com/groups/Zlin.Udalosti/", "Zlin.Udalosti"),
    ("https://www.facebook.com/groups/Zlin.Udalosti/?ref=share&mibextid=abc", "Zlin.Udalosti"),
    ("https://m.facebook.com/groups/1028789523845607/permalink/1/", "1028789523845607"),
    ("facebook.com/groups/zlin.bazar", "zlin.bazar"),
    ("https://www.facebook.com/zpravodajstvi.zlin.cz", None),   # страница, не группа
    ("https://www.facebook.com/groups/feed/", None),
    ("", None),
])
def test_parse_group_slug(url, slug):
    assert extract.parse_group_slug(url) == slug


@pytest.mark.parametrize("href, expected", [
    ("https://www.facebook.com/groups/Zlin.Udalosti/posts/28585844851046703/?__cft__[0]=AZ", ("Zlin.Udalosti", "28585844851046703")),
    ("/groups/1028789523845607/permalink/28585844851046703/", ("1028789523845607", "28585844851046703")),
    ("https://www.facebook.com/groups/x/posts/123?comment_id=5", ("x", "123")),
    ("https://www.facebook.com/zpravodajstvi.zlin.cz/posts/pfbid0Qbh3PPz", None),
    ("https://www.facebook.com/photo/?fbid=1&set=pcb.2", None),
])
def test_post_link_regex(href, expected):
    m = sel.POST_LINK_RE.search(href)
    assert (m.groups() if m else None) == expected


def test_own_group_link_preferred_over_foreign():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup('<a href="/groups/other/posts/9/">x</a><a href="/groups/mine/posts/1/">y</a>', "html.parser")
    anchor, gid, pid = extract._find_post_link(soup.find_all("a"), {"mine"})
    assert (gid, pid) == ("mine", "1")


# ---------------------------------------------------------------------------
# Счётчик активности
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, today, month", [
    ("Aktivita Dnes 19 nových příspěvků 577 za poslední měsíc", 19, 577),
    ("Dnes 1 nový příspěvek · 30 za poslední měsíc", 1, 30),
    ("Dnes 2 nové příspěvky", 2, None),
    ("Dnes žádné nové příspěvky 4 za poslední měsíc", 0, 4),
    ("Dnes 1 234 nových příspěvků 12 345 za poslední měsíc", 1234, 12345),
    ("12 new posts today 1,234 in the last month", 12, 1234),
    ("nic", None, None),
])
def test_parse_activity(text, today, month):
    assert extract.parse_activity(f"<html><body><div>{text}</div></body></html>") == extract.Activity(today, month)
