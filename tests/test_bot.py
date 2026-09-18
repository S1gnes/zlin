"""Кнопки черновика — прогоном апдейтов через настоящий диспетчер с моком бота."""
import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from mock_bot import (ADMIN_ID, CHANNEL_ID, DISCUSSION_ID, STRANGER_ID, callback_update, forward_update,
                      make_bot, message_update)
from zlinbot.bot.app import build_dispatcher
from zlinbot.bot.publisher import Publisher
from zlinbot.db import Database
from zlinbot.gemini import Verdict

T0 = 1_789_000_000
REWRITTEN = Verdict(False, "Kratší verze.", "Короче.", "Коротше.", "Shorter.", ("fakt",),
                    model="gemini-2.5-flash")


class FakeGemini:
    model = "gemini-2.5-flash"

    def __init__(self, answer=REWRITTEN):
        self.answer = answer
        self.calls: list[str] = []

    async def summarize(self, text, *, source="", criteria="", extra=""):
        self.calls.append(extra)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
async def env(tmp_path):
    async with Database(tmp_path / "t.db") as db:
        await db.add_group(kind="rss", url="https://zlin.cz/feed/", slug="https://zlin.cz/feed/",
                           fb_id=None, name="ZLIN.CZ", now=T0)
        await db.insert_post(post_id="rss:a", group_id=1, permalink="https://zlin.cz/zpravy/a/",
                             author="Redakce", text="Uzavírka na třídě Tomáše Bati od 20. září.",
                             shared_text=None, media=[], created_at=T0, status="pending",
                             text_hash=None, now=T0)
        draft_id = await db.add_draft(post_id="rss:a", summary="Uzavírka potrvá do 30. října.",
                                      summary_ru="Перекрытие продлится до 30 октября.",
                                      summary_ua="Перекриття триватиме до 30 жовтня.",
                                      summary_en="The closure lasts until 30 October.",
                                      facts=["od 20. září"], model="gemini-2.5-flash", now=T0)
        bot, session = make_bot()
        gemini = FakeGemini()
        yield db, bot, session, gemini, draft_id


def dispatcher(db, bot, gemini, *, session=None):
    publisher = Publisher(bot, db, CHANNEL_ID, channel_username="zlin_kanal", clock=lambda: T0)
    return build_dispatcher(db, publisher, gemini, admin_id=ADMIN_ID)


# -- доступ --------------------------------------------------------------------

async def test_stranger_cannot_press_any_button(env):
    db, bot, session, gemini, draft_id = env
    dp = dispatcher(db, bot, gemini)
    for action in ("publish", "reject", "rewrite", "again"):
        await dp.feed_update(bot, callback_update(f"d:{action}:{draft_id}", user_id=STRANGER_ID))
    assert session.count("SendMessage") == 0                       # в канал ничего не ушло
    assert (await db.get_draft(draft_id)).status == "pending"
    answers = [c for c in session.calls if type(c).__name__ == "AnswerCallbackQuery"]
    assert len(answers) == 4 and all("только владельца" in a.text for a in answers)


async def test_stranger_commands_are_ignored(env):
    db, bot, session, gemini, draft_id = env
    dp = dispatcher(db, bot, gemini)
    await dp.feed_update(bot, message_update("/pending", user_id=STRANGER_ID))
    assert session.calls == []


async def test_owner_sees_the_queue(env):
    db, bot, session, gemini, draft_id = env
    dp = dispatcher(db, bot, gemini)
    await dp.feed_update(bot, message_update("/pending"))
    texts = [c.text for c in session.calls if isinstance(c, SendMessage)]
    assert "Черновиков в очереди: 1" in texts[0]
    assert "Uzavírka potrvá" in texts[1] and "Перекрытие продлится" in texts[1]
    assert "od 20. září" in texts[1]                                 # факты для сверки
    card = session.last("SendMessage")
    buttons = [b.text for row in card.reply_markup.inline_keyboard for b in row]
    assert buttons == ["✅ Опубликовать", "✏️ Переписать", "🚫 Отклонить", "🔗 Оригинал"]
    assert (await db.get_draft(draft_id)).admin_msg_id is not None


# -- публикация ----------------------------------------------------------------

async def test_publish_posts_to_channel_in_the_required_format(env):
    db, bot, session, gemini, draft_id = env
    dp = dispatcher(db, bot, gemini)
    await dp.feed_update(bot, callback_update(f"d:publish:{draft_id}"))

    post = next(c for c in session.calls if isinstance(c, SendMessage) and c.chat_id == CHANNEL_ID)
    assert post.text.startswith("Uzavírka potrvá do 30. října.")
    assert '📍 <a href="https://zlin.cz/zpravy/a/">ZLIN.CZ</a>' in post.text   # ссылка в гипертексте
    assert "🔗 https://" not in post.text                            # голого URL в посте больше нет
    assert post.link_preview_options.is_disabled is True
    assert (await db.get_draft(draft_id)).status == "published"
    assert (await db.get_post("rss:a")).status == "published"
    assert (await db.event_counts(0)).get("published") == 1
    assert "t.me/zlin_kanal/" in session.last("SendMessage").text   # ссылка на пост владельцу


async def test_second_press_does_not_publish_twice(env):
    db, bot, session, gemini, draft_id = env
    dp = dispatcher(db, bot, gemini)
    await dp.feed_update(bot, callback_update(f"d:publish:{draft_id}"))
    to_channel = sum(1 for c in session.calls if isinstance(c, SendMessage) and c.chat_id == CHANNEL_ID)

    await dp.feed_update(bot, callback_update(f"d:publish:{draft_id}", update_id=2))
    assert sum(1 for c in session.calls if isinstance(c, SendMessage) and c.chat_id == CHANNEL_ID) == to_channel
    answer = [c for c in session.calls if type(c).__name__ == "AnswerCallbackQuery"][-1]
    assert "уже опубликован" in answer.text


async def test_telegram_refusal_returns_draft_to_the_queue(env):
    db, bot, session, gemini, draft_id = env
    session.set_error("SendMessage", TelegramBadRequest(method=SendMessage(chat_id=1, text="x"),
                                                        message="CHAT_WRITE_FORBIDDEN"))
    dp = dispatcher(db, bot, gemini)
    await dp.feed_update(bot, callback_update(f"d:publish:{draft_id}"))
    assert (await db.get_draft(draft_id)).status == "pending"       # можно нажать ещё раз
    assert (await db.get_post("rss:a")).status == "pending"


async def test_all_four_languages_go_in_the_post_itself(env):
    """Переводы больше не уходят комментарием в группу обсуждений: пост самодостаточен."""
    db, bot, session, gemini, draft_id = env
    dp = dispatcher(db, bot, gemini)
    await dp.feed_update(bot, callback_update(f"d:publish:{draft_id}"))
    post = next(c for c in session.calls if isinstance(c, SendMessage) and c.chat_id == CHANNEL_ID)

    assert post.text.startswith("Uzavírka potrvá do 30. října.\n\n")   # чешский ведущим абзацем
    assert "RU · Перекрытие продлится до 30 октября." in post.text
    assert "UA · Перекриття триватиме до 30 жовтня." in post.text
    assert "EN · The closure lasts until 30 October." in post.text
    assert "tg-spoiler" not in post.text                            # спойлера больше нет
    assert "🇷🇺" not in post.text                                    # флагов стран в посте нет
    assert post.text.index("RU ·") < post.text.index("UA ·") < post.text.index("EN ·")
    assert post.text.rstrip().endswith("</a>")                      # источник со ссылкой — последним


async def test_channel_forward_no_longer_produces_a_comment(env):
    """Служебная пересылка в группу обсуждений больше не обрабатывается вообще."""
    db, bot, session, gemini, draft_id = env
    dp = dispatcher(db, bot, gemini)
    await dp.feed_update(bot, callback_update(f"d:publish:{draft_id}"))
    channel_msg_id = (await db.get_draft(draft_id)).channel_msg_id
    before = session.count("SendMessage")

    await dp.feed_update(bot, forward_update(channel_msg_id, update_id=2))
    assert session.count("SendMessage") == before
    assert (await db.get_draft(draft_id)).comment_msg_id is None


# -- отклонение и переписывание ------------------------------------------------

async def test_reject_marks_draft_and_post(env):
    db, bot, session, gemini, draft_id = env
    dp = dispatcher(db, bot, gemini)
    await dp.feed_update(bot, callback_update(f"d:reject:{draft_id}"))
    assert (await db.get_draft(draft_id)).status == "rejected"
    assert (await db.get_post("rss:a")).status == "rejected"
    assert session.count("EditMessageReplyMarkup") == 1             # кнопки убраны
    assert (await db.event_counts(0)).get("rejected") == 1


async def test_rewrite_asks_what_to_change_then_regenerates(env):
    db, bot, session, gemini, draft_id = env
    dp = dispatcher(db, bot, gemini)
    await dp.feed_update(bot, callback_update(f"d:rewrite:{draft_id}"))
    question = session.last("SendMessage")
    assert "Что поправить" in question.text
    assert question.reply_markup.inline_keyboard[0][0].text == "🔁 Просто заново"

    await dp.feed_update(bot, message_update("сделай короче и без цен", update_id=2))
    assert gemini.calls == ["сделай короче и без цен"]               # указание дошло до модели
    assert (await db.get_draft(draft_id)).status == "superseded"
    new_draft = (await db.drafts(status="pending"))[0]
    assert new_draft.summary == "Kratší verze." and new_draft.id != draft_id
    assert "Kratší verze." in session.last("SendMessage").text


async def test_rewrite_again_button_regenerates_without_instruction(env):
    db, bot, session, gemini, draft_id = env
    dp = dispatcher(db, bot, gemini)
    await dp.feed_update(bot, callback_update(f"d:rewrite:{draft_id}"))
    await dp.feed_update(bot, callback_update(f"d:again:{draft_id}", update_id=2))
    assert gemini.calls == [""]
    assert (await db.drafts(status="pending"))[0].summary == "Kratší verze."


async def test_plain_message_without_rewrite_state_is_not_treated_as_instruction(env):
    db, bot, session, gemini, draft_id = env
    dp = dispatcher(db, bot, gemini)
    await dp.feed_update(bot, message_update("просто болтовня"))
    assert gemini.calls == []
    assert (await db.get_draft(draft_id)).status == "pending"

# -- проверки при старте -------------------------------------------------------

from aiogram.types import Chat, ChatMemberAdministrator, ChatMemberMember, User  # noqa: E402

from zlinbot.bot.app import check_channel, startup_report  # noqa: E402

BOT_USER = User(id=424242, is_bot=True, first_name="zlinbot")


def member(status: str, *, can_post: bool = True):
    if status != "administrator":
        return ChatMemberMember(user=BOT_USER, status="member")
    # model_construct — чтобы не перечислять полтора десятка прав, которые тесту не нужны
    return ChatMemberAdministrator.model_construct(user=BOT_USER, status="administrator",
                                                   can_post_messages=can_post)


async def startup(*, linked: int | None, in_channel: str = "administrator",
                  in_group: str = "administrator", can_post: bool = True):
    bot, session = make_bot()
    session.set_response("GetChat", Chat(id=CHANNEL_ID, type="channel", title="Zlín kanál",
                                         username="zlin_kanal", linked_chat_id=linked))
    session.set_response("GetMe", BOT_USER)
    session.set_response("GetChatMember", [member(in_channel, can_post=can_post), member(in_group)])
    info = await check_channel(bot, CHANNEL_ID)
    return info, startup_report(info)


async def test_startup_is_quiet_when_everything_is_fine():
    info, report = await startup(linked=DISCUSSION_ID)
    assert info.ok and "Проблем не вижу" in report


async def test_startup_says_nothing_about_discussion_group_any_more():
    """Группа обсуждений боту больше не нужна — и её отсутствие не повод для тревоги."""
    _, report = await startup(linked=None)
    assert "Проблем не вижу" in report and "обсужден" not in report


async def test_startup_warns_when_bot_is_not_channel_admin():
    _, report = await startup(linked=DISCUSSION_ID, in_channel="member")
    assert "не администратор канала" in report


async def test_startup_warns_when_bot_cannot_post():
    _, report = await startup(linked=DISCUSSION_ID, can_post=False)
    assert "нет права публиковать" in report


# -- медиа ---------------------------------------------------------------------

def with_media(db, bot, gemini, tmp_path, *, files: int = 1, mode: str = "copy"):
    """Публикатор с кэшем медиа: кладём файлы так, как их кладёт MediaStore."""
    from zlinbot.media import MediaStore
    store = MediaStore(tmp_path / "media")
    folder = store.dir_for(1)
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (folder / f"{i:02d}-photo-p{i}.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 100)
    publisher = Publisher(bot, db, CHANNEL_ID, channel_username="zlin_kanal",
                          store=store, clock=lambda: T0)
    return build_dispatcher(db, publisher, gemini, admin_id=ADMIN_ID, store=store), store


async def test_single_photo_goes_with_caption(env, tmp_path):
    db, bot, session, gemini, draft_id = env
    dp, store = with_media(db, bot, gemini, tmp_path, files=1)
    await dp.feed_update(bot, callback_update(f"d:publish:{draft_id}"))
    sent = session.last("SendPhoto")
    assert sent.chat_id == CHANNEL_ID
    assert '📍 <a href="https://zlin.cz/zpravy/a/">ZLIN.CZ</a>' in sent.caption
    assert "Uzavírka potrvá" in sent.caption
    assert store.files(draft_id) == []                      # файлы убраны после публикации


async def test_several_photos_go_as_album_with_caption_on_first(env, tmp_path):
    db, bot, session, gemini, draft_id = env
    dp, _ = with_media(db, bot, gemini, tmp_path, files=3)
    await dp.feed_update(bot, callback_update(f"d:publish:{draft_id}"))
    album = session.last("SendMediaGroup")
    assert len(album.media) == 3
    assert album.media[0].caption and album.media[1].caption is None
    assert (await db.get_draft(draft_id)).channel_msg_id is not None


async def test_long_text_goes_after_the_album(env, tmp_path):
    db, bot, session, gemini, draft_id = env
    from zlinbot.bot import texts
    await db.set_draft_status(draft_id, "pending", now=T0)       # вернуть в очередь после фикстуры
    long_summary = "Dlouhý text. " * 120                          # заведомо больше 1024 символов подписи
    await db.add_draft(post_id="rss:a", summary=long_summary, summary_ru="Длинно.",
                       facts=[], model="m", now=T0)
    new_id = (await db.drafts(status="pending"))[-1].id
    dp, store = with_media(db, bot, gemini, tmp_path, files=2)
    folder = store.dir_for(new_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "00-photo-a.jpg").write_bytes(b"\xff\xd8\xffxx")

    await dp.feed_update(bot, callback_update(f"d:publish:{new_id}"))
    assert len(long_summary) > texts.MAX_CAPTION
    photo = session.last("SendPhoto")
    assert photo.caption is None                                  # в подпись не влезло
    tail = [c for c in session.calls if isinstance(c, SendMessage) and c.chat_id == CHANNEL_ID][-1]
    assert "Dlouhý text." in tail.text
    # текст отвечает именно на альбом, а не висит отдельно; тот же id сохранён у черновика
    assert tail.reply_parameters.message_id == (await db.get_draft(new_id)).channel_msg_id


async def test_media_rejected_by_telegram_falls_back_to_text(env, tmp_path):
    db, bot, session, gemini, draft_id = env
    session.set_error("SendPhoto", TelegramBadRequest(method=SendMessage(chat_id=1, text="x"),
                                                      message="IMAGE_PROCESS_FAILED"))
    dp, _ = with_media(db, bot, gemini, tmp_path, files=1)
    await dp.feed_update(bot, callback_update(f"d:publish:{draft_id}"))
    post = next(c for c in session.calls if isinstance(c, SendMessage) and c.chat_id == CHANNEL_ID)
    assert "Uzavírka potrvá" in post.text                          # новость вышла, пусть и без фото
    assert (await db.get_draft(draft_id)).status == "published"


async def test_link_only_mode_sends_no_media(env, tmp_path):
    db, bot, session, gemini, draft_id = env
    await db.set_setting("media_mode", "link")
    dp, _ = with_media(db, bot, gemini, tmp_path, files=2)
    await dp.feed_update(bot, callback_update(f"d:publish:{draft_id}"))
    assert session.count("SendMediaGroup") == 0 and session.count("SendPhoto") == 0
    assert next(c for c in session.calls if isinstance(c, SendMessage) and c.chat_id == CHANNEL_ID)
