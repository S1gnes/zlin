"""
Playwright-скрапер публичных групп Facebook без входа.

Вежливый темп встроен сюда, а не в вызывающий код: одна загрузка за раз
(asyncio.Lock) и случайная пауза 25–70 с между ЛЮБЫМИ двумя загрузками FB.
Так «проверить сейчас» из бота встанет в очередь за фоновым кругом, а не пойдёт
параллельно. Каждая загрузка — новый контекст браузера: без профиля и cookies.

Что делать с HTML, решает extract.py. Здесь только навигация: cookie-баннер,
оверлей входа, прокрутка, раскрытие «Zobrazit víc».
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import TimeoutError as PWTimeout

from . import extract
from . import selectors as sel

log = logging.getLogger(__name__)

PAUSE_BETWEEN_LOADS = (25.0, 70.0)   # секунд между любыми двумя загрузками FB
NAV_TIMEOUT_MS = 45_000
FEED_TIMEOUT_MS = 20_000
SETTLE_MS = 2_500                    # дать React дорисовать после появления ленты
SCROLL_SCREENS = 2
MAX_SEE_MORE_CLICKS = 30
RELAUNCH_EVERY = 50                  # перезапуск Chromium против утечек памяти в долгой работе
VIEWPORT = {"width": 1280, "height": 2400}
DEBUG_KEEP = 60                      # сколько последних HTML-дампов хранить

Status = Literal["ok", "login_wall", "checkpoint", "no_feed", "no_articles", "no_ids", "error"]


@dataclass(frozen=True, slots=True)
class ScrapeResult:
    slug: str
    status: Status
    feed: extract.FeedResult | None = None
    final_url: str | None = None
    see_more_clicks: int = 0
    debug_html: Path | None = None
    error: str | None = None


# Убрать окно входа вместе с его затемняющим слоем и вернуть прокрутку.
# Поднимаемся от диалога до самого внешнего предка, который НЕ содержит ленту, — это слой оверлея.
_REMOVE_LOGIN_JS = """
([dialogSel, loginSel, feedSel]) => {
  let removed = 0;
  for (const d of document.querySelectorAll(dialogSel)) {
    if (!d.querySelector(loginSel)) continue;
    let n = d;
    while (n.parentElement && n.parentElement !== document.body
           && !n.parentElement.querySelector(feedSel)) n = n.parentElement;
    n.remove();
    removed++;
  }
  for (const el of [document.documentElement, document.body]) el.style.overflow = 'auto';
  return removed;
}
"""

# Если у статьи нет ссылки на пост — FB мог не заполнить href до наведения мыши.
# «Наводим» на все её ссылки, чтобы он их заполнил.
_TOUCH_LINKS_JS = """
([feedSel, articleSel, linkRe]) => {
  const rx = new RegExp(linkRe);
  let touched = 0;
  for (const art of document.querySelectorAll(feedSel + ' ' + articleSel)) {
    if (art.parentElement.closest(articleSel) || !art.innerText.trim()) continue;
    const links = [...art.querySelectorAll('a')];
    if (links.some(a => rx.test(a.getAttribute('href') || ''))) continue;
    for (const a of links) {
      for (const t of ['mouseover', 'mouseenter', 'focus']) a.dispatchEvent(new Event(t, {bubbles: true}));
      touched++;
    }
  }
  return touched;
}
"""


class FacebookScraper:
    def __init__(self, *, headless: bool = True, debug_dir: Path | None = Path("debug")) -> None:
        self._headless = headless
        self._debug_dir = debug_dir
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._user_agent = ""
        self._lock = asyncio.Lock()
        self._last_load: float | None = None
        self._loads_since_launch = 0

    async def __aenter__(self) -> FacebookScraper:
        self._pw = await async_playwright().start()
        await self._launch()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # -- публичное API -------------------------------------------------------

    async def fetch_group(self, slug: str, *, group_id: str | None = None,
                          save_html: Path | None = None) -> ScrapeResult:
        """Загрузить ленту группы и извлечь посты. Никогда не бросает исключение."""
        async with self._lock:
            await self._pace()
            ctx = await self._new_context()
            try:
                page = await ctx.new_page()
                await page.goto(extract.group_feed_url(slug), wait_until="domcontentloaded",
                                timeout=NAV_TIMEOUT_MS)
                blocked = _blocked_status(page.url)
                if blocked:
                    log.warning("%s: FB увёл на %s", slug, page.url)
                    return ScrapeResult(slug, blocked, final_url=page.url)
                try:
                    await page.wait_for_selector(sel.FEED, state="attached", timeout=FEED_TIMEOUT_MS)
                except PWTimeout:
                    pass  # отсутствие ленты зафиксирует extract (feed_found=False)
                await page.wait_for_timeout(SETTLE_MS)
                await self._decline_cookies(page)
                await self._remove_login_overlay(page)
                await self._scroll(page)
                clicks = await self._expand_see_more(page)
                await self._remove_login_overlay(page)
                await self._touch_links(page)
                html, final_url = await page.content(), page.url
            except Exception as e:  # noqa: BLE001 — сбой одной группы не должен ронять круг
                log.exception("%s: ошибка загрузки", slug)
                return ScrapeResult(slug, "error", error=f"{type(e).__name__}: {e}")
            finally:
                await ctx.close()
                self._last_load = time.monotonic()

        if save_html:
            save_html.write_text(html, encoding="utf-8")
        feed = await asyncio.to_thread(extract.extract_feed, html, group_slug=slug, group_id=group_id)
        dump = None
        if feed.health != "ok" or any(p.truncated for p in feed.posts):
            dump = self._dump(slug, feed.health, html)
        return ScrapeResult(slug, feed.health, feed, final_url, clicks, dump)

    async def fetch_activity(self, slug: str, *, save_html: Path | None = None) -> extract.Activity | None:
        """Счётчик «Dnes N nových příspěvků» со страницы /about. None — не удалось."""
        async with self._lock:
            await self._pace()
            ctx = await self._new_context()
            try:
                page = await ctx.new_page()
                await page.goto(extract.group_about_url(slug), wait_until="domcontentloaded",
                                timeout=NAV_TIMEOUT_MS)
                if _blocked_status(page.url):
                    return None
                await page.wait_for_timeout(SETTLE_MS * 2)
                await self._decline_cookies(page)
                html = await page.content()
            except Exception:  # noqa: BLE001
                log.exception("%s: ошибка загрузки /about", slug)
                return None
            finally:
                await ctx.close()
                self._last_load = time.monotonic()
        if save_html:
            save_html.write_text(html, encoding="utf-8")
        return await asyncio.to_thread(extract.parse_activity, html)

    # -- внутреннее ----------------------------------------------------------

    async def _launch(self) -> None:
        assert self._pw is not None
        self._browser = await self._pw.chromium.launch(headless=self._headless)
        major = self._browser.version.split(".")[0]
        # Обычный десктопный UA вместо «HeadlessChrome» — версия совпадает с реальным движком.
        self._user_agent = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
        self._loads_since_launch = 0

    async def _new_context(self) -> BrowserContext:
        if self._loads_since_launch >= RELAUNCH_EVERY:
            log.info("перезапуск Chromium после %d загрузок", self._loads_since_launch)
            assert self._browser is not None
            await self._browser.close()
            await self._launch()
        self._loads_since_launch += 1
        assert self._browser is not None
        return await self._browser.new_context(locale="cs-CZ", timezone_id="Europe/Prague",
                                               user_agent=self._user_agent, viewport=VIEWPORT)

    async def _pace(self) -> None:
        if self._last_load is None:
            return
        wait = random.uniform(*PAUSE_BETWEEN_LOADS) - (time.monotonic() - self._last_load)
        if wait > 0:
            log.info("пауза %.0f с перед следующей загрузкой FB", wait)
            await asyncio.sleep(wait)

    async def _decline_cookies(self, page: Page) -> None:
        btn = page.get_by_role("button", name=sel.COOKIE_DECLINE_RE)
        try:
            if await btn.count():
                await btn.first.dispatch_event("click")
                await page.wait_for_timeout(1000)
        except Exception:  # noqa: BLE001 — баннер не критичен
            log.debug("cookie-баннер: не удалось нажать", exc_info=True)

    async def _remove_login_overlay(self, page: Page) -> None:
        removed = await page.evaluate(_REMOVE_LOGIN_JS, [sel.DIALOG, sel.LOGIN_FORM, sel.FEED])
        if removed:
            log.debug("убрано окон входа: %d", removed)

    async def _scroll(self, page: Page) -> None:
        for _ in range(SCROLL_SCREENS):
            await page.mouse.wheel(0, VIEWPORT["height"])
            await page.wait_for_timeout(1200)

    async def _expand_see_more(self, page: Page) -> int:
        """Прокликать все «Zobrazit víc» в ленте. Клик через dispatch_event — не зависит
        от того, перекрыт ли элемент чем-то сверху."""
        buttons = page.locator(sel.FEED).get_by_role("button", name=sel.SEE_MORE_RE)
        clicks = 0
        while clicks < MAX_SEE_MORE_CLICKS:
            before = await buttons.count()
            if not before:
                break
            try:
                await buttons.first.dispatch_event("click")
            except Exception:  # noqa: BLE001
                log.debug("«Zobrazit víc»: клик не прошёл", exc_info=True)
                break
            clicks += 1
            await page.wait_for_timeout(500)
            if await buttons.count() >= before:  # кнопка не исчезла — раскрытие не сработало
                log.warning("«Zobrazit víc» не раскрывает текст (кликов: %d)", clicks)
                break
        return clicks

    async def _touch_links(self, page: Page) -> None:
        touched = await page.evaluate(_TOUCH_LINKS_JS, [sel.FEED, sel.ARTICLE, sel.POST_LINK_RE.pattern])
        if touched:
            log.info("у статей без ссылки на пост наведено на %d ссылок", touched)
            await page.wait_for_timeout(800)

    def _dump(self, slug: str, status: str, html: str) -> Path | None:
        if not self._debug_dir:
            return None
        self._debug_dir.mkdir(parents=True, exist_ok=True)
        path = self._debug_dir / f"{datetime.now():%Y%m%d-%H%M%S}_{_safe(slug)}_{status}.html"
        path.write_text(html, encoding="utf-8")
        for old in sorted(self._debug_dir.glob("*.html"))[:-DEBUG_KEEP]:
            old.unlink(missing_ok=True)
        return path


def _blocked_status(url: str) -> Status | None:
    if sel.CHECKPOINT_RE.search(url):
        return "checkpoint"
    if sel.LOGIN_REDIRECT_RE.search(url):
        return "login_wall"
    return None


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in s)[:60]
