"""Разбор RSS/Atom и условные запросы — на сохранённом боевом фиде, без сети."""
from pathlib import Path

import httpx
import pytest

from zlinbot.rss import FeedItem, RssFetcher, parse_feed

FIX = Path(__file__).parent / "fixtures"
ZLIN = (FIX / "rss_zlin.xml").read_text(encoding="utf-8")

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Město Zlín</title>
  <entry>
    <title>Odstávka vody v Podvesné</title>
    <link rel="edit" href="https://zlin.eu/edit/1"/>
    <link href="https://zlin.eu/aktuality/odstavka-vody"/>
    <id>urn:uuid:1225c695-cfb8-4ebb-aaaa-80da344efa6a</id>
    <updated>2026-09-15T08:30:00Z</updated>
    <author><name>Magistrát</name></author>
    <content type="html">&lt;p&gt;Ve čtvrtek &lt;b&gt;20. září&lt;/b&gt; od 8:00 do 14:00.&lt;/p&gt;</content>
  </entry>
</feed>
"""


@pytest.fixture(scope="module")
def zlin() -> tuple[FeedItem, ...]:
    return parse_feed(ZLIN).items


def test_parses_invalid_xml_from_real_feed(zlin):
    # у zlin.cz не объявлен префикс szn: — строгий XML-парсер на этом фиде падает целиком
    assert "<szn:image>" in ZLIN
    assert parse_feed(ZLIN).title == "ZLIN.CZ"
    assert len(zlin) == 3


def test_rss_link_and_stable_id(zlin):
    first = zlin[0]
    assert first.permalink == "https://zlin.cz/zpravy/fotogalerie-uherskym-hradistem-prosly-stovky-krojovanych/"
    assert first.post_id.startswith("rss:") and len(first.post_id) == 24
    assert first.post_id == parse_feed(ZLIN).items[0].post_id          # ключ стабилен между разборами
    assert len({i.post_id for i in zlin}) == 3


def test_title_and_summary_become_text(zlin):
    with_summary = zlin[1]
    assert with_summary.title == "Martin a Martin ve Zlíně. Tak ať to klukům šlape!"
    assert with_summary.text.startswith(with_summary.title + "\n")
    assert "Co myslíte, pánové" in with_summary.text
    assert "<" not in with_summary.text                                 # HTML из анонса вычищен


def test_empty_summary_leaves_only_title(zlin):
    assert zlin[0].text == zlin[0].title                               # у этой новости анонс — одна точка


def test_dates_author_categories(zlin):
    assert zlin[0].created_at == 1789490400                            # Tue, 15 Sep 2026 16:40:00 +0000
    assert zlin[1].author == "Jan Čada"
    assert "Sport" in zlin[1].categories


def test_one_image_per_item_despite_several_sizes(zlin):
    # WordPress кладёт ту же картинку в enclosure и в media:content (800x450) — берём одну
    for i in zlin:
        assert [m.kind for m in i.media] == ["photo"]
        assert "-800x450" not in i.media[0].url


def test_atom_shape():
    [entry] = parse_feed(ATOM).items
    assert entry.permalink == "https://zlin.eu/aktuality/odstavka-vody"  # rel="edit" пропущен
    assert entry.author == "Magistrát"
    assert entry.created_at == 1789461000                                # 2026-09-15T08:30:00Z
    assert entry.text == "Odstávka vody v Podvesné\nVe čtvrtek 20. září od 8:00 do 14:00."


def test_garbage_is_not_a_feed():
    assert parse_feed("<html><body><h1>404</h1></body></html>").items == ()
    assert parse_feed("").items == ()


def fetcher(handler) -> RssFetcher:
    return RssFetcher(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_fetch_sends_conditional_headers_and_returns_etag():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        if request.headers.get("if-none-match") == 'W/"abc"':
            return httpx.Response(304)
        return httpx.Response(200, text=ZLIN, headers={"ETag": 'W/"abc"', "Last-Modified": "Tue, 15 Sep 2026 16:40:00 GMT"})

    async with fetcher(handler) as f:
        first = await f.fetch("https://zlin.cz/feed/")
        assert (first.status, first.etag, len(first.items)) == ("ok", 'W/"abc"', 3)
        again = await f.fetch("https://zlin.cz/feed/", etag=first.etag)
        assert (again.status, again.items, again.etag) == ("not_modified", (), 'W/"abc"')
        assert seen["if-none-match"] == 'W/"abc"'
        assert "zlinbot" in seen["user-agent"]


@pytest.mark.parametrize("response, expect", [
    (httpx.Response(404), "HTTP 404"),
    (httpx.Response(200, text="<html>не фид</html>"), "элемента"),
])
async def test_fetch_errors(response, expect):
    async with fetcher(lambda request: response) as f:
        r = await f.fetch("https://zlin.cz/feed/")
    assert r.status == "error" and expect in r.error


async def test_fetch_network_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("таймаут", request=request)

    async with fetcher(boom) as f:
        r = await f.fetch("https://zlin.cz/feed/")
    assert r.status == "error" and "ConnectTimeout" in r.error
