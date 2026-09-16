"""Админка: источники, стоп-слова, статистика, настройки — через настоящий диспетчер."""
import pytest
from aiogram.methods import SendMessage

from mock_bot import ADMIN_ID, CHANNEL_ID, STRANGER_ID, callback_update, make_bot, message_update
from test_bot import FakeGemini
from zlinbot.bot.app import build_dispatcher
from zlinbot.bot.publisher import Publisher
from zlinbot.collector import AddResult, GroupOutcome, SourceResult
from zlinbot.db import Database

T0 = 1_789_000_000


class FakeCollector:
    """Коллектор целиком подменён: живой Facebook и сеть в тестах не нужны."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.added: list[str] = []
        self.checked: list[int] = []
        self.add_result: AddResult | None = None

    async def add_source(self, url: str) -> AddResult:
        self.added.append(url)
        if self.add_result is not None:
            return self.add_result
        group = await self.db.add_group(kind="rss", url=url, slug=url, fb_id=None, name="Новая лента", now=T0)
        return AddResult(True, group, seen=7)

    async def check_group(self, group_id: int) -> GroupOutcome | None:
        self.checked.append(group_id)
        group = await self.db.get_group(group_id)
        return GroupOutcome(group, SourceResult("ok"), counts=__import__("collections").Counter({"new": 2}))


@pytest.fixture
async def env(tmp_path):
    async with Database(tmp_path / "t.db") as db:
        await db.add_group(kind="rss", url="https://zlin.cz/feed/", slug="https://zlin.cz/feed/",
                           fb_id=None, name="ZLIN.CZ", now=T0)
        bot, session = make_bot()
        collector = FakeCollector(db)
        publisher = Publisher(bot, db, CHANNEL_ID, clock=lambda: T0)
        dp = build_dispatcher(db, publisher, FakeGemini(), admin_id=ADMIN_ID, collector=collector)
        yield db, bot, session, dp, collector


def texts_of(session) -> list[str]:
    return [c.text for c in session.calls if isinstance(c, SendMessage)]


# -- источники -----------------------------------------------------------------

async def test_groups_lists_sources_with_buttons(env):
    db, bot, session, dp, collector = env
    await dp.feed_update(bot, message_update("/groups"))
    card = session.last("SendMessage")
    assert "ZLIN.CZ" in card.text and "🟢" in card.text
    buttons = [b.text for row in card.reply_markup.inline_keyboard for b in row]
    assert buttons == ["⏸ Пауза", "🔄 Проверить сейчас", "🗑 Удалить"]


async def test_pause_and_resume(env):
    db, bot, session, dp, collector = env
    await dp.feed_update(bot, callback_update("s:pause:1"))
    assert (await db.get_group(1)).status == "paused"
    await dp.feed_update(bot, callback_update("s:resume:1", update_id=2))
    assert (await db.get_group(1)).status == "active"


async def test_check_now_runs_the_collector(env):
    db, bot, session, dp, collector = env
    await dp.feed_update(bot, callback_update("s:check:1"))
    assert collector.checked == [1]
    assert "новых 2" in texts_of(session)[-1]


async def test_delete_asks_confirmation_first(env):
    db, bot, session, dp, collector = env
    await dp.feed_update(bot, callback_update("s:delete:1"))
    assert "Удалить «ZLIN.CZ»?" in texts_of(session)[-1]
    assert await db.get_group(1) is not None          # пока ничего не удалено

    await dp.feed_update(bot, callback_update("s:delete_no:1", update_id=2))
    assert await db.get_group(1) is not None

    await dp.feed_update(bot, callback_update("s:delete:1", update_id=3))
    await dp.feed_update(bot, callback_update("s:delete_yes:1", update_id=4))
    assert await db.get_group(1) is None


async def test_add_asks_for_link_then_adds(env):
    db, bot, session, dp, collector = env
    await dp.feed_update(bot, message_update("/add"))
    assert "Пришли ссылку" in texts_of(session)[-1]

    await dp.feed_update(bot, message_update("https://zlin.eu/rss", update_id=2))
    assert collector.added == ["https://zlin.eu/rss"]
    answer = texts_of(session)[-1]
    assert "Добавил" in answer and "уже увиденные" in answer


async def test_add_reports_refusal(env):
    db, bot, session, dp, collector = env
    collector.add_result = AddResult(False, reason="Без входа группа не читается — Facebook требует вход.")
    await dp.feed_update(bot, message_update("/add"))
    await dp.feed_update(bot, message_update("https://www.facebook.com/groups/x/", update_id=2))
    assert "Facebook требует вход" in texts_of(session)[-1]
    assert len(await db.list_groups()) == 1


async def test_cancel_drops_the_state(env):
    db, bot, session, dp, collector = env
    await dp.feed_update(bot, message_update("/add"))
    await dp.feed_update(bot, message_update("/cancel", update_id=2))
    await dp.feed_update(bot, message_update("https://zlin.eu/rss", update_id=3))
    assert collector.added == []                       # ссылка после отмены источником не считается


# -- стоп-слова ----------------------------------------------------------------

async def test_filters_add_and_remove(env):
    db, bot, session, dp, collector = env
    await dp.feed_update(bot, message_update("/filters"))
    assert "Пока пусто" in texts_of(session)[-1]

    await dp.feed_update(bot, callback_update("f:add:0", update_id=2))
    await dp.feed_update(bot, message_update("Prodám", update_id=3))
    assert [w for _, w, _ in await db.list_filters()] == ["Prodám"]
    assert "Добавил «Prodám»" in texts_of(session)[-2]

    await dp.feed_update(bot, callback_update("f:rm:1", update_id=4))
    assert await db.list_filters() == []


async def test_too_short_filter_is_refused(env):
    db, bot, session, dp, collector = env
    await dp.feed_update(bot, callback_update("f:add:0"))
    await dp.feed_update(bot, message_update("до", update_id=2))
    assert "Слишком короткое" in texts_of(session)[-1]
    assert await db.list_filters() == []


# -- статистика и настройки ----------------------------------------------------

async def test_stats_counts_events_and_queue(env):
    db, bot, session, dp, collector = env
    import time
    now = time.time()
    for event in ("collected", "collected", "filtered", "published"):
        await db.log(event, now=now)
    await db.log("collected", now=now - 5 * 86400)          # попадёт только в недельный срез
    await dp.feed_update(bot, message_update("/stats"))
    text = texts_of(session)[-1]
    assert "собрано записей: 2" in text and "отсеяно стоп-словами: 1" in text
    assert "опубликовано: 1" in text
    assert "собрано записей: 3" in text.split("За неделю")[1]
    assert "черновиков ждёт решения: 0" in text


async def test_settings_show_and_toggle_media(env):
    db, bot, session, dp, collector = env
    await dp.feed_update(bot, message_update("/settings"))
    assert "переносить в канал" in texts_of(session)[-1]

    await dp.feed_update(bot, callback_update("set:media:", update_id=2))
    assert await db.get_setting("media_mode") == "link"
    edited = session.last("EditMessageText")
    assert "только ссылка" in edited.text


async def test_criteria_are_editable_from_the_bot(env):
    db, bot, session, dp, collector = env
    await dp.feed_update(bot, callback_update("set:criteria:"))
    new = "Только перекрытия дорог, аварии и отключения воды в Злине и окрестностях."
    await dp.feed_update(bot, message_update(new, update_id=2))
    assert await db.get_setting("relevance") == new
    assert "Критерии обновлены" in texts_of(session)[-2]


async def test_short_criteria_are_refused(env):
    db, bot, session, dp, collector = env
    await dp.feed_update(bot, callback_update("set:criteria:"))
    await dp.feed_update(bot, message_update("новости", update_id=2))
    assert await db.get_setting("relevance") is None
    assert "Слишком коротко" in texts_of(session)[-1]


async def test_model_can_be_switched_from_the_bot(env):
    db, bot, session, dp, collector = env
    await dp.feed_update(bot, callback_update("set:model:gemini-3.8-flash"))
    assert await db.get_setting("gemini_model") == "gemini-3.8-flash"


# -- доступ --------------------------------------------------------------------

@pytest.mark.parametrize("command", ["/groups", "/add", "/filters", "/stats", "/settings", "/models"])
async def test_stranger_gets_nothing_from_admin_commands(env, command):
    db, bot, session, dp, collector = env
    await dp.feed_update(bot, message_update(command, user_id=STRANGER_ID))
    assert session.calls == []


async def test_stranger_cannot_delete_a_source(env):
    db, bot, session, dp, collector = env
    await dp.feed_update(bot, callback_update("s:delete_yes:1", user_id=STRANGER_ID))
    assert await db.get_group(1) is not None
