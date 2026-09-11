"""
Извлечение постов из HTML страницы группы Facebook.

Единственное место (вместе с selectors.py), которое знает, как устроена разметка
поста. Когда Facebook переедет — чинить здесь. Модуль чистый: без сети и браузера,
на входе HTML (page.content() после того, как scraper.py раскрыл «Zobrazit víc»),
на выходе FeedResult. Поэтому тестируется на сохранённых фикстурах.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from . import selectors as sel

FB_ROOT = "https://www.facebook.com"
MAX_POSTS = 25

Health = Literal["ok", "no_feed", "no_articles", "no_ids"]


# ---------------------------------------------------------------------------
# Контракт
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Media:
    kind: Literal["photo", "video"]
    url: str


@dataclass(frozen=True, slots=True)
class Post:
    post_id: str               # "gid:pid" — ключ навсегда; gid канонический (числовой, если известен)
    group_id: str              # тот самый gid
    pid: str                   # числовой ID поста из ссылки
    permalink: str             # чистая ссылка без трекинга
    text: str                  # очищенный текст самого поста ("" если только репост/фото)
    shared_text: str | None    # текст поста, которым поделились (репост), иначе None
    author: str | None
    group_name: str | None
    media: tuple[Media, ...]
    created_at: int | None     # unix-время из JSON страницы; None — не нашли
    created_label: str | None  # видимая метка («1 h», «Včera v 14:30») — только для показа
    truncated: bool            # осталось «… Zobrazit víc» — раскрыть не удалось


@dataclass(frozen=True, slots=True)
class FeedResult:
    feed_found: bool
    group_name: str | None
    group_numeric_id: str | None
    articles: int              # непустые статьи верхнего уровня
    skeletons: int             # пустые заглушки
    without_id: int            # непустые статьи, у которых не нашли ID
    posts: tuple[Post, ...]

    @property
    def health(self) -> Health:
        """ok / no_feed (стена логина или новая вёрстка) / no_articles (сменилась разметка
        постов) / no_ids (сменились ссылки)."""
        if not self.feed_found:
            return "no_feed"
        if self.articles == 0:
            return "no_articles"
        if not self.posts:
            return "no_ids"
        return "ok"


@dataclass(frozen=True, slots=True)
class Activity:
    today: int | None          # «Dnes N nových příspěvků»
    month: int | None          # «M za poslední měsíc»


# ---------------------------------------------------------------------------
# Адреса групп
# ---------------------------------------------------------------------------

def parse_group_slug(url: str) -> str | None:
    """Любая ссылка на группу -> slug или числовой ID из неё. Не группа -> None."""
    m = sel.GROUP_URL_RE.search(url or "")
    if not m or m.group(1).lower() in {"feed", "discover", "joins", "search", "create"}:
        return None
    return m.group(1)


def group_feed_url(slug: str) -> str:
    return f"{FB_ROOT}/groups/{slug}/?{sel.CHRONO_QUERY}"


def group_about_url(slug: str) -> str:
    return f"{FB_ROOT}/groups/{slug}/about"


# ---------------------------------------------------------------------------
# Лента
# ---------------------------------------------------------------------------

def extract_feed(html: str, *, group_slug: str | None = None, group_id: str | None = None,
                 limit: int = MAX_POSTS) -> FeedResult:
    """HTML страницы группы -> посты.

    group_slug — slug из ссылки на группу (чтобы у репоста из другой группы взять
    правильную ссылку); group_id — канонический gid, если уже известен (из БД).
    """
    soup = BeautifulSoup(html, "html.parser")
    group_name = _group_name(soup)
    numeric_id = _first_group(sel.GROUP_NUMERIC_ID_RES, html)
    gid = group_id or numeric_id or group_slug
    own_gids = {g.lower() for g in (group_slug, numeric_id, group_id) if g}

    feed = soup.select_one(sel.FEED)
    if feed is None:
        return FeedResult(False, group_name, numeric_id, 0, 0, 0, ())

    times = _creation_times(html)
    articles = skeletons = without_id = 0
    posts: list[Post] = []
    seen: set[str] = set()
    for art in _top_level_articles(feed):
        if not art.get_text(strip=True) and art.find("img") is None:
            skeletons += 1
            continue
        articles += 1
        post = _parse_article(art, gid, own_gids, group_name, times)
        if post is None:
            without_id += 1
            continue
        if post.post_id in seen:
            continue
        seen.add(post.post_id)
        posts.append(post)
        if len(posts) >= limit:
            break
    return FeedResult(True, group_name, numeric_id, articles, skeletons, without_id, tuple(posts))


def _top_level_articles(feed: Tag) -> list[Tag]:
    return [a for a in feed.select(sel.ARTICLE) if a.find_parent(attrs={"role": "article"}) is None]


def _own(nodes: Iterable[Tag], art: Tag) -> list[Tag]:
    """Только узлы самой статьи, без вложенных статей (комментариев)."""
    return [n for n in nodes if n.find_parent(attrs={"role": "article"}) is art]


def _parse_article(art: Tag, gid: str | None, own_gids: set[str], group_name: str | None,
                   times: dict[str, int]) -> Post | None:
    anchors = _own(art.select(sel.LINK), art)
    hit = _find_post_link(anchors, own_gids)
    if hit is not None:
        anchor, link_gid, pid = hit
        created_label = _norm_line(anchor.get("aria-label") or anchor.get_text(" ", strip=True)) or None
    else:
        pid = _find_fallback_pid(anchors)
        if pid is None:
            return None
        link_gid, created_label = gid, None
    gid_final = gid or link_gid
    if not gid_final:
        return None

    authors = [_norm_line(n.get_text(" ", strip=True)) for n in _own(art.select(sel.AUTHOR), art)]
    authors = [a for a in authors if a]
    author = authors[0] if authors else _fallback_author(art)
    names = [*authors, *([author] if author else [])]

    own_text, shared_raw = _message_texts(art)
    raw = own_text if own_text is not None else _node_text(art, skip_hidden=True)
    truncated = bool(sel.TRUNCATED_RE.search(raw) or (shared_raw and sel.TRUNCATED_RE.search(shared_raw)))
    text = clean_text(raw, names=names, group_name=group_name)
    shared_text = clean_text(shared_raw, names=names, group_name=group_name) if shared_raw else None

    return Post(
        post_id=f"{gid_final}:{pid}",
        group_id=gid_final,
        pid=pid,
        permalink=f"{FB_ROOT}/groups/{link_gid or gid_final}/posts/{pid}/",
        text=text,
        shared_text=shared_text or None,
        author=author,
        group_name=group_name,
        media=_media(art),
        created_at=times.get(pid),
        created_label=created_label,
        truncated=truncated,
    )


def _find_post_link(anchors: list[Tag], own_gids: set[str]) -> tuple[Tag, str, str] | None:
    """Первая ссылка /groups/<gid>/posts/<pid>. Ссылки на текущую группу — в приоритете:
    у репоста из другой группы в статье две такие ссылки, наша идёт первой, но проверяем явно."""
    first = None
    for a in anchors:
        m = sel.POST_LINK_RE.search(a.get("href", ""))
        if not m:
            continue
        if m.group(1).lower() in own_gids:
            return a, m.group(1), m.group(2)
        first = first or (a, m.group(1), m.group(2))
    return first


def _find_fallback_pid(anchors: list[Tag]) -> str | None:
    for rx in sel.POST_ID_FALLBACK_RES:
        for a in anchors:
            m = rx.search(a.get("href", ""))
            if m:
                return m.group(1)
    return None


def _fallback_author(art: Tag) -> str | None:
    for node in _own(art.select(sel.AUTHOR_FALLBACK), art):
        name = _norm_line(node.get_text(" ", strip=True))
        if name:
            return name
    return None


def _message_texts(art: Tag) -> tuple[str | None, str | None]:
    """(текст самого поста, текст расшаренного поста). None — блока нет.

    Блок текста, стоящий после второго имени автора, принадлежит расшаренному посту —
    так репост без своего комментария не выдаёт чужой текст за свой.
    """
    nodes: list[Tag] = []
    for css in sel.MESSAGE_SELECTORS:
        found = _own(art.select(css), art)
        found_ids = {id(n) for n in found}  # Tag.__eq__ сравнивает содержимое, нужна идентичность
        nodes = [n for n in found if not any(id(p) in found_ids for p in n.parents)]
        if nodes:
            break
    if not nodes:
        return None, None

    authors = _own(art.select(sel.AUTHOR), art)
    boundary = authors[1] if len(authors) > 1 else None
    order = {id(n): i for i, n in enumerate(art.descendants)} if boundary else {}
    own, shared = [], []
    for n in nodes:
        is_shared = boundary is not None and order.get(id(n), 0) > order.get(id(boundary), 0)
        (shared if is_shared else own).append(_node_text(n, skip_hidden=False))
    return ("\n".join(own) if own else ""), ("\n".join(shared) if shared else None)


def _media(art: Tag) -> tuple[Media, ...]:
    out: list[Media] = []
    seen: set[str] = set()
    for img in _own(art.select(sel.PHOTO_IMG), art):
        src = img.get("src", "")
        if src.startswith("http") and src not in seen:
            seen.add(src)
            out.append(Media("photo", src))
    for video in _own(art.select(sel.VIDEO), art):
        src = video.get("src", "")
        if src.startswith("http") and src not in seen:  # blob: — сегменты, не файл
            seen.add(src)
            out.append(Media("video", src))
    return tuple(out)


# ---------------------------------------------------------------------------
# Текст
# ---------------------------------------------------------------------------

_BLOCK_TAGS = frozenset({"div", "p", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
                         "blockquote", "section", "article", "tr", "header", "footer"})
_SKIP_TAGS = frozenset({"script", "style", "svg", "template", "noscript", "head", "title"})
_WS_RE = re.compile(r"[ \t\u00a0\u202f\u2009\u200b]+")


def _node_text(node: Tag, *, skip_hidden: bool) -> str:
    """Текст узла с переносами строк по блокам и <br>, эмодзи из alt картинок.
    Вложенные статьи (комментарии) и <svg> пропускаются."""
    parts: list[str] = []
    at_line_start = True

    def text(s: str) -> None:
        nonlocal at_line_start
        s = s.replace("\n", " ")  # перевод строки внутри текстового узла — форматирование HTML
        if s.strip():
            parts.append(s)
            at_line_start = False
        elif not at_line_start:
            parts.append(" ")     # пробел между инлайнами; отступы между блоками игнорируем

    def newline(force: bool = False) -> None:
        nonlocal at_line_start
        if force or not at_line_start:
            parts.append("\n")
            at_line_start = True

    def walk(n: Tag) -> None:
        for ch in n.children:
            if isinstance(ch, Comment):
                continue
            if isinstance(ch, NavigableString):
                text(str(ch))
                continue
            if not isinstance(ch, Tag) or ch.name in _SKIP_TAGS:
                continue
            if ch.get("role") == "article" or (skip_hidden and ch.get("aria-hidden") == "true"):
                continue
            if ch.name == "img":
                alt = ch.get("alt") or ""
                if alt and sel.EMOJI_SRC_RE.search(ch.get("src", "")):
                    text(alt)
                continue
            if ch.name == "br":
                newline(force=True)
                continue
            block = ch.name in _BLOCK_TAGS
            if block:
                newline()
            walk(ch)
            if block:
                newline()

    walk(node)
    return "".join(parts)


def _norm_line(line: str) -> str:
    return _WS_RE.sub(" ", line).strip()


def clean_text(raw: str, *, names: Iterable[str] = (), group_name: str | None = None) -> str:
    """Вычищает служебный мусор из текста поста.

    Убирает: шапку (имя автора дублем, «1 h», «·»), подвал («Všechny reakce:», счётчики,
    «To se mi líbí · Komentář · Sdílet»), хвост «… Zobrazit víc», одиночные строки
    интерфейса, шапку расшаренного поста посреди текста. Даты и числа в самом тексте
    не трогает.
    """
    if not raw:
        return ""
    known = {_norm_line(n) for n in names if n}
    if group_name:
        known.add(_norm_line(group_name))
    lines = [sel.SEE_MORE_TAIL_RE.sub("", _norm_line(l)) for l in raw.replace("\r", "").split("\n")]
    lines = ["" if l.startswith(sel.UNAVAILABLE_PREFIXES) else l for l in lines]

    # подвал: первая строка-маркер среди последних FOOTER_WINDOW строк и всё после неё
    start = max(0, len(lines) - sel.FOOTER_WINDOW)
    for i in range(start, len(lines)):
        if lines[i] in sel.FOOTER_MARKERS or lines[i].startswith(sel.FOOTER_PREFIXES):
            lines = lines[:i]
            break
    while lines and (not lines[-1] or lines[-1] in sel.UI_LINES or sel.COUNTER_RE.match(lines[-1])):
        lines.pop()

    # шапка в начале: имя(имена) обязательно выкидываем, дальше опционально время и «·»
    i = 0
    while i < len(lines) and (not lines[i] or lines[i] in known):
        i += 1
    if i and any(lines[k] in known for k in range(i)):
        i = _skip_time_and_dot(lines, i)

    out: list[str] = []
    while i < len(lines):
        line = lines[i]
        if line in known:  # шапка расшаренного поста посреди текста: имя + время/«·»
            j = _skip_time_and_dot(lines, i + 1)
            if j > i + 1:
                i = j
                continue
        if line not in sel.UI_LINES:
            out.append(line)
        i += 1

    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _skip_time_and_dot(lines: list[str], i: int) -> int:
    """После имени: пропустить пустые строки, одну метку времени и разделитель «·».
    Возвращает индекс первой строки тела (== i, если шапки не было)."""
    j = i
    while j < len(lines) and not lines[j]:
        j += 1
    consumed = False
    if j < len(lines) and sel.RELATIVE_TIME_RE.match(lines[j]):
        j += 1
        consumed = True
        while j < len(lines) and not lines[j]:
            j += 1
    if j < len(lines) and lines[j] == "·":
        j += 1
        consumed = True
    return j if consumed else i


# ---------------------------------------------------------------------------
# JSON и шапка страницы
# ---------------------------------------------------------------------------

def _first_group(patterns: Iterable[re.Pattern[str]], text: str) -> str | None:
    for rx in patterns:
        m = rx.search(text)
        if m:
            return m.group(1)
    return None


def _creation_times(html: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for rx in sel.CREATION_TIME_RES:
        for m in rx.finditer(html):
            out.setdefault(m.group("pid"), int(m.group("ts")))
    return out


def _group_name(soup: BeautifulSoup) -> str | None:
    meta = soup.find("meta", attrs={"property": "og:title"})
    name = meta.get("content") if meta else None
    if not name and soup.head and soup.head.title:
        name = soup.head.title.get_text()
    if not name:
        return None
    name = re.sub(r"^\(\d+\)\s*", "", _norm_line(name))
    name = re.sub(r"\s*\|\s*Facebook$", "", name)
    return name if name and name != "Facebook" else None


# ---------------------------------------------------------------------------
# Страница «Informace»
# ---------------------------------------------------------------------------

def parse_activity(html: str) -> Activity:
    """Счётчики активности со страницы /about группы (видны без логина)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "svg", "template", "noscript"]):
        tag.decompose()
    text = _WS_RE.sub(" ", soup.get_text("\n"))
    today = 0 if sel.ACTIVITY_TODAY_ZERO_RE.search(text) else _count(sel.ACTIVITY_TODAY_RES, text)
    return Activity(today=today, month=_count(sel.ACTIVITY_MONTH_RES, text))


def _count(patterns: Iterable[re.Pattern[str]], text: str) -> int | None:
    raw = _first_group(patterns, text)
    return int(re.sub(r"[ ,]", "", raw)) if raw else None
