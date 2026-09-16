"""Что важно при запуске в контейнере: настройки из переменных окружения и жизнь без Chromium."""
import pytest

from zlinbot import config
from zlinbot.fb import scraper as sc


# -- настройки без .env --------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for key in ("BOT_TOKEN", "ADMIN_ID", "CHANNEL_ID", "GEMINI_API_KEY", "GEMINI_MODEL",
                "DB_PATH", "MEDIA_DIR", "DEBUG_DIR", "HEADLESS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "ROOT", tmp_path)   # чтобы не подхватился настоящий .env
    return monkeypatch


def test_settings_come_from_environment(clean_env):
    clean_env.setenv("BOT_TOKEN", "123:abc")
    clean_env.setenv("ADMIN_ID", "610088839")
    clean_env.setenv("CHANNEL_ID", "-1001234567890")
    clean_env.setenv("GEMINI_API_KEY", "key")
    clean_env.setenv("DB_PATH", "/data/zlinbot.db")
    clean_env.setenv("MEDIA_DIR", "/data/media")
    cfg = config.load()
    assert cfg.bot_token == "123:abc" and cfg.admin_id == 610088839
    assert cfg.channel_id == "-1001234567890" and cfg.gemini_key == "key"
    assert str(cfg.db_path) == "/data/zlinbot.db" or str(cfg.db_path).endswith("zlinbot.db")
    assert str(cfg.media_dir).endswith("media")


def test_missing_settings_are_none_not_crash(clean_env):
    cfg = config.load()
    assert (cfg.bot_token, cfg.admin_id, cfg.channel_id, cfg.gemini_key) == (None, None, None, None)
    assert cfg.gemini_model                        # модель по умолчанию всегда есть


def test_broken_admin_id_does_not_crash_startup(clean_env):
    clean_env.setenv("ADMIN_ID", "не число")
    assert config.load().admin_id is None          # запуск скажет «не хватает настроек»


# -- запуск без Chromium (образ для Render его не содержит) --------------------

class Boom:
    launches = 0

    async def __aenter__(self):
        Boom.launches += 1
        raise RuntimeError("Executable doesn't exist at /ms-playwright/chromium/chrome")


@pytest.fixture
def no_browser(monkeypatch):
    Boom.launches = 0
    monkeypatch.setattr(sc, "FacebookScraper", lambda **kwargs: Boom())
    return sc.LazyFacebookScraper(headless=True)


async def test_missing_chromium_is_a_source_error_not_a_crash(no_browser):
    result = await no_browser.fetch_group("zlin.test")
    assert result.status == "error"
    assert "Chromium" in result.error and "INSTALL_CHROMIUM" in result.error


async def test_browser_launch_is_not_retried_every_round(no_browser):
    for _ in range(3):
        await no_browser.fetch_group("zlin.test")
    assert Boom.launches == 1                      # пробуем не чаще раза в LAUNCH_RETRY
    assert await no_browser.fetch_activity("zlin.test") is None


async def test_retry_happens_after_the_cooldown(no_browser, monkeypatch):
    await no_browser.fetch_group("zlin.test")
    # отсчитываем от момента сбоя: монотонные часы машины уже давно «тикают»
    monkeypatch.setattr(sc.time, "monotonic", lambda: no_browser._error_at + sc.LAUNCH_RETRY + 1)
    await no_browser.fetch_group("zlin.test")
    assert Boom.launches == 2
