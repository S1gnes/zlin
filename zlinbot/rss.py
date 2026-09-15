"""
RSS/Atom — второй тип источника, не зависящий от Facebook.

Разбор снисходительный (bs4 + html.parser), а не строгий XML: реальные фиды бывают
невалидными. У zlin.cz не объявлен префикс szn:, и xml.etree на нём падает целиком.
Цена снисходительности — <link> в HTML считается пустым тегом, поэтому у RSS ссылку
берём из текста рядом с ним, а у Atom из атрибута href.

Контракт наружу — FeedItem с теми же полями, что нужны конвейеру от поста Facebook,
поэтому дедупликация, фильтры и черновики работают для обоих источников одинаково.
"""
from __future__ import annotations

import hashlib
import logging
import re
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from bs4 import BeautifulSoup, Tag, XMLParsedAsHTMLWarning

from .fb.extract import Media

log = logging.getLogger(__name__)

USER_AGENT = "zlinbot/0.1 (aggregator for a personal Telegram channel)"
TIMEOUT = 25.0
MAX_ITEMS = 25
_WS_RE = re.compile(r"\s+")
_IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)", re.I)
# WordPress отдаёт одну и ту же картинку в enclosure и в media:content, отличаются только размером
# в имени файла: ...-scaled-e178947.jpg и ...-scaled-e178947-800x450.jpg. Для сравнения размер убираем.
_IMAGE_SIZE_RE = re.compile(r"-\d{2,4}x\d{2,4}(?=\.\w{3,4}(\?|$))")


@dataclass(frozen=True, slots=True)
class FeedItem:
    post_id: str               # "rss:<хеш guid>" — ключ навсегда
    permalink: str
    text: str                  # заголовок + анонс: это уйдёт в дедупликацию и в Gemini
    title: str
    author: str | None
    media: tuple[Media, ...]
    created_at: int | None
    categories: tuple[str, ...] = ()
    shared_text: None = None   # у RSS репостов нет — поле ради общего контракта с постом FB


@dataclass(frozen=True, slots=True)
class ParsedFeed:
    title: str | None
    items: tuple[FeedItem, ...]


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status: str                # ok / not_modified / error
    feed: ParsedFeed | None = None
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None

    @property
    def items(self) -> tuple[FeedItem, ...]:
        return self.feed.items if self.feed else ()


# ---------------------------------------------------------------------------
# Разбор
# ---------------------------------------------------------------------------

def parse_feed(xml: str, *, limit: int = MAX_ITEMS) -> ParsedFeed:
    with warnings.catch_warnings():  # html.parser для XML — осознанный выбор, см. заголовок модуля
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(xml, "html.parser")
    entries = soup.find_all(["item", "entry"], limit=limit)
    items = tuple(item for item in (_item(e) for e in entries) if item is not None)
    return ParsedFeed(title=_feed_title(soup), items=items)


def _feed_title(soup: BeautifulSoup) -> str | None:
    root = soup.find(["channel", "feed"])
    title = root.find("title", recursive=False) if root else None
    return _text(title.get_text()) if title else None


def _item(node: Tag) -> FeedItem | None:
    link = _link(node)
    guid = _first_text(node, ["guid", "id"]) or link
    if not guid or not link:
        return None
    title = _text(_first_text(node, ["title"]) or "")
    summary = _html_text(_first_text(node, ["description", "summary", "content:encoded", "content"]) or "")
    if summary == title or len(summary) < 3:  # у zlin.cz часть анонсов — одна точка
        summary = ""
    return FeedItem(
        post_id="rss:" + hashlib.sha1(guid.strip().encode("utf-8")).hexdigest()[:20],
        permalink=link,
        title=title,
        text="\n".join(part for part in (title, summary) if part),
        author=_text(_first_text(node, ["dc:creator", "author", "name"]) or "") or None,
        media=_media(node),
        created_at=_date(node),
        categories=tuple(_text(c.get_text()) for c in node.find_all(["category", "dc:subject"])
                         if c.get_text(strip=True)),
    )


def _link(node: Tag) -> str | None:
    for tag in node.find_all("link"):
        href = tag.get("href")
        if href and tag.get("rel") in (None, "alternate"):   # Atom
            return href.strip()
        nxt = tag.next_sibling                               # RSS: html.parser закрывает <link> сразу
        if isinstance(nxt, str) and nxt.strip().startswith("http"):
            return nxt.strip().split()[0]
    guid = node.find("guid")
    if guid is not None and (guid.get("ispermalink") or "").lower() != "false":
        text = guid.get_text(strip=True)
        if text.startswith("http"):
            return text
    return None


def _first_text(node: Tag, names: list[str]) -> str | None:
    for name in names:
        tag = node.find(name)
        if tag is not None and tag.get_text().strip():
            return tag.get_text()
    return None


def _media(node: Tag) -> tuple[Media, ...]:
    out: list[Media] = []
    seen: set[str] = set()
    for tag in node.find_all(["enclosure", "media:content", "media:thumbnail"]):
        url = (tag.get("url") or "").strip()
        mime = (tag.get("type") or "").lower()
        medium = (tag.get("medium") or "").lower()
        key = _IMAGE_SIZE_RE.sub("", url)
        if not url.startswith("http") or key in seen:
            continue
        if mime.startswith("video") or medium == "video":
            kind = "video"
        elif mime.startswith("image") or medium == "image" or _IMAGE_EXT_RE.search(url):
            kind = "photo"
        else:
            continue
        seen.add(key)
        out.append(Media(kind, url))  # type: ignore[arg-type]
    return tuple(out)


def _date(node: Tag) -> int | None:
    raw = _first_text(node, ["pubdate", "published", "updated", "dc:date"])
    if not raw:
        return None
    raw = raw.strip()
    try:
        return int(parsedate_to_datetime(raw).timestamp())        # RFC 822 — RSS
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))   # ISO 8601 — Atom
    except ValueError:
        log.warning("не разобрал дату из фида: %r", raw[:40])
        return None
    return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())


def _html_text(raw: str) -> str:
    return _text(BeautifulSoup(raw, "html.parser").get_text(" "))


def _text(raw: str) -> str:
    return _WS_RE.sub(" ", raw.replace("\xa0", " ")).strip()


# ---------------------------------------------------------------------------
# Загрузка
# ---------------------------------------------------------------------------

class RssFetcher:
    """Условный GET: если фид не менялся, сервер ответит 304 и тело не поедет."""

    def __init__(self, *, timeout: float = TIMEOUT, client: httpx.AsyncClient | None = None) -> None:
        self._own = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def __aenter__(self) -> RssFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own:
            await self._client.aclose()

    async def fetch(self, url: str, *, etag: str | None = None,
                    last_modified: str | None = None) -> FetchResult:
        # заголовки ставим на запрос, а не на клиента: клиент могут передать снаружи,
        # и тогда бот ходил бы безымянным
        headers = {"User-Agent": USER_AGENT,
                   "Accept": "application/rss+xml, application/atom+xml, text/xml, */*"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            r = await self._client.get(url, headers=headers)
        except httpx.HTTPError as e:
            log.warning("%s: %s", url, e)
            return FetchResult(url, "error", error=f"{type(e).__name__}: {e}")
        if r.status_code == 304:
            return FetchResult(url, "not_modified", etag=etag, last_modified=last_modified)
        if r.status_code >= 400:
            return FetchResult(url, "error", error=f"HTTP {r.status_code}")
        feed = parse_feed(r.text)
        if not feed.items:
            return FetchResult(url, "error", error="в ответе нет ни одного элемента — это точно RSS/Atom?")
        return FetchResult(url, "ok", feed, r.headers.get("ETag"), r.headers.get("Last-Modified"))
