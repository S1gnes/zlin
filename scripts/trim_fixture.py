"""
Ужать сохранённую страницу FB до того, что реально читает extract.py.

Полная страница — 1–2 МБ, почти всё это JS самого Facebook и чужие данные.
Для фикстуры нужны: <title>, ID группы, JSON-фрагменты с creation_time и сама лента.

    .venv\\Scripts\\python scripts\\trim_fixture.py debug\\<дамп>.html tests\\fixtures\\new_case.html
    .venv\\Scripts\\python scripts\\trim_fixture.py about.html tests\\fixtures\\about_case.html --about
"""
from __future__ import annotations

import argparse
import html as htmllib
import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zlinbot.fb import selectors as sel  # noqa: E402

_JUNK_TAGS = ["script", "style", "noscript", "template", "link", "meta"]


def trim_feed(page: str) -> str:
    soup = BeautifulSoup(page, "html.parser")
    title = soup.head.title.get_text() if soup.head and soup.head.title else ""
    snippets = [m.group(0) for rx in (*sel.GROUP_NUMERIC_ID_RES, *sel.CREATION_TIME_RES) for m in rx.finditer(page)]
    feed = soup.select_one(sel.FEED)
    if feed is None:
        raise SystemExit("в странице нет ленты (div[role=feed]) — это не страница группы или стена логина")
    for tag in feed(["script", "style", "noscript", "template"]):
        tag.decompose()
    json_block = "\n".join(dict.fromkeys(snippets))  # без повторов, порядок сохранён
    return (f'<!DOCTYPE html>\n<!-- ужато scripts/trim_fixture.py: только то, что читает extract.py -->\n'
            f'<html><head><meta charset="utf-8"><title>{htmllib.escape(title)}</title></head><body>\n'
            f'<script type="text/plain" data-trimmed="json-snippets">\n{json_block}\n</script>\n'
            f'<div role="main">{feed}</div>\n</body></html>\n')


def trim_about(page: str) -> str:
    """Только карточка «Aktivita»: самый глубокий элемент, где есть и «Dnes …», и «… za poslední měsíc».
    Остальное (описание, админы, обложка) тестам не нужно и в репозиторий не идёт."""
    soup = BeautifulSoup(page, "html.parser")
    main = soup.select_one('div[role="main"]') or soup.body
    for tag in main([*_JUNK_TAGS, "svg", "img", "image"]):
        tag.decompose()
    card = None
    for el in main.find_all(True):  # документный порядок: потомки идут после предков
        text = el.get_text(" ", strip=True)
        has_today = any(rx.search(text) for rx in sel.ACTIVITY_TODAY_RES) or sel.ACTIVITY_TODAY_ZERO_RE.search(text)
        if has_today and any(rx.search(text) for rx in sel.ACTIVITY_MONTH_RES):
            card = el
    if card is None:
        raise SystemExit("на странице нет счётчиков активности — это не /about группы или FB сменил тексты")
    return (f'<!DOCTYPE html>\n<!-- ужато scripts/trim_fixture.py: только карточка «Aktivita» со страницы /about -->\n'
            f'<html><head><meta charset="utf-8"></head><body><div role="main">\n{card}\n</div></body></html>\n')


def main() -> None:
    ap = argparse.ArgumentParser(description="Ужать сохранённую страницу FB в фикстуру для тестов")
    ap.add_argument("src", type=Path)
    ap.add_argument("dst", type=Path)
    ap.add_argument("--about", action="store_true", help="это страница /about, а не лента")
    args = ap.parse_args()
    page = args.src.read_text(encoding="utf-8")
    out = trim_about(page) if args.about else trim_feed(page)
    args.dst.write_text(out, encoding="utf-8")
    print(f"{args.src} ({len(page) // 1024} КБ) -> {args.dst} ({len(out) // 1024} КБ)")


if __name__ == "__main__":
    main()
