"""Переключение языка и англоязычный интерфейс (спека localization)."""

from types import SimpleNamespace

from tgbot import db
from tgbot.i18n import texts as i18n_texts
from tgbot.access import Access
from tgbot.bot import make_post_init
from tgbot.handlers.support import send_feedback_reminder
from tgbot.locales import en, ru

from .conftest import make_handlers, TEST_CONFIG, ADMIN_ID, buttons, log_rows, make_context, make_update

USER = 777
EN_ADMIN = 102


class RecordingBot:
    """`context.bot` для пушей: помнит отправленное, правку принимает молча."""

    def __init__(self):
        self.sent: list[SimpleNamespace] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        message = SimpleNamespace(chat_id=chat_id, message_id=1, text=text)
        self.sent.append(message)
        return message

    async def edit_message_text(self, **kwargs):
        pass

    def by_chat(self) -> dict[int, str]:
        """Последний текст, ушедший в каждый чат."""
        return {message.chat_id: message.text for message in self.sent}



async def pick_language(handlers, user_id: int, code: str):
    update = make_update(user_id, callback_data=f"lang:{code}")
    await handlers["language_callback"](update, make_context())
    return update


# --- /language ------------------------------------------------------------
async def test_language_menu_marks_current(conn, handlers):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    update = make_update(USER)
    await handlers["language"](update, make_context())

    labels = [label for label, _ in buttons(update)]
    data = [payload for _, payload in buttons(update)]
    assert data == ["lang:ru", "lang:en"]
    assert ru.STRINGS["LANGUAGE_NAMES"]["ru"] in labels[0]
    # Текущий язык помечен, второй - нет.
    assert labels[0] != ru.STRINGS["LANGUAGE_NAMES"]["ru"]
    assert labels[1] == ru.STRINGS["LANGUAGE_NAMES"]["en"]
    assert update.effective_message.replies[0] == ru.STRINGS["LANGUAGE_PROMPT"]


async def test_language_switch_confirms_in_new_language(conn, handlers):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    update = await pick_language(handlers, USER, "en")

    assert db.get_lang(conn, USER) == "en"
    assert update.effective_message.replies[-1] == en.STRINGS["LANGUAGE_SET"]
    assert log_rows(conn, "language_changed")[0]["lang"] == "en"


async def test_next_command_answers_in_new_language(conn, handlers):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    await pick_language(handlers, USER, "en")

    update = make_update(USER)
    await handlers["start"](update, make_context())
    assert update.effective_message.replies[0] == en.STRINGS["START"]


async def test_repeated_choice_is_not_an_error(conn, handlers):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    await pick_language(handlers, USER, "ru")
    update = await pick_language(handlers, USER, "ru")

    assert db.get_lang(conn, USER) == "ru"
    assert update.effective_message.replies[-1] == ru.STRINGS["LANGUAGE_SET"]


async def test_unknown_language_code_is_ignored(conn, handlers):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    await pick_language(handlers, USER, "klingon")
    assert db.get_lang(conn, USER) is None


async def test_language_available_without_access(conn, handlers):
    """`/language` - как /whoami и /request_access: до проверки allowlist."""
    update = make_update(USER)
    await handlers["language"](update, make_context())
    assert update.effective_message.replies


# --- автоопределение ------------------------------------------------------
async def test_english_profile_gets_english_without_asking(conn, handlers):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    update = make_update(USER, language_code="en-GB")
    await handlers["start"](update, make_context())
    assert update.effective_message.replies[0] == en.STRINGS["START"]


async def test_russian_profile_stays_russian(conn, handlers):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    update = make_update(USER, language_code="ru")
    await handlers["start"](update, make_context())
    assert update.effective_message.replies[0] == ru.STRINGS["START"]


# --- язык получателя ------------------------------------------------------
async def test_feedback_reminder_uses_each_admin_language(conn):
    db.set_lang(conn, ADMIN_ID, "ru")
    db.set_lang(conn, EN_ADMIN, "en")
    db.add_feedback(conn, USER, None, "bot is slow")

    bot = RecordingBot()
    await send_feedback_reminder(bot, conn, [ADMIN_ID, EN_ADMIN])
    sent = bot.by_chat()
    assert sent[ADMIN_ID] == ru.STRINGS["FEEDBACK_REMINDER"].format(n=1)
    assert sent[EN_ADMIN] == en.STRINGS["FEEDBACK_REMINDER"].format(n=1)


async def test_access_card_in_each_admin_language(conn):
    """Карточка заявки - на языке каждого админа, ФИО и причина - как есть."""
    db.set_lang(conn, ADMIN_ID, "ru")
    db.set_lang(conn, EN_ADMIN, "en")
    access = Access(conn, [ADMIN_ID, EN_ADMIN])
    handlers = make_handlers(access, TEST_CONFIG)
    db.set_lang(conn, USER, "en")

    bot = RecordingBot()
    await handlers["request_access"](
        make_update(USER, language_code="en"), SimpleNamespace(args=[], bot=bot)
    )
    request = db.open_access_request(conn, USER)
    update = make_update(USER, text="John Doe, need access for planning")
    await handlers["fallback"](update, SimpleNamespace(args=[], bot=bot))

    by_admin = bot.by_chat()
    ru_head = ru.STRINGS["ACCESS_CARD"].split("\n")[0].split("#")[0]
    en_head = en.STRINGS["ACCESS_CARD"].split("\n")[0].split("#")[0]
    assert ru_head in by_admin[ADMIN_ID]
    assert en_head in by_admin[EN_ADMIN]
    # Пользовательский текст не переводится и не теряется.
    assert "John Doe" in by_admin[ADMIN_ID]
    assert request is not None


async def test_applicant_is_notified_in_their_language(conn):
    db.set_lang(conn, ADMIN_ID, "ru")
    db.set_lang(conn, USER, "en")
    access = Access(conn, [ADMIN_ID])
    handlers = make_handlers(access, TEST_CONFIG)

    bot = RecordingBot()
    ctx = SimpleNamespace(args=[], bot=bot)
    await handlers["request_access"](make_update(USER), ctx)
    request = db.open_access_request(conn, USER)
    await handlers["access_callback"](
        make_update(USER, callback_data=f"access:skip:{request['id']}"), ctx
    )
    await handlers["access_callback"](
        make_update(ADMIN_ID, callback_data=f"access:approve:{request['id']}"), ctx
    )
    assert bot.by_chat()[USER] == en.STRINGS["ACCESS_GRANTED"]


# --- меню команд ----------------------------------------------------------
async def test_command_menu_registered_for_both_languages(conn):
    calls: list[dict] = []

    class Bot:
        async def set_my_commands(self, commands, scope=None, language_code=None):
            calls.append(
                {
                    "commands": [(c.command, c.description) for c in commands],
                    "scope": scope,
                    "language_code": language_code,
                }
            )

    class App:
        def __init__(self):
            self.bot = Bot()

        bot_data: dict = {}

    app = App()
    await make_post_init([ADMIN_ID], conn)(app)

    default = next(c for c in calls if c["scope"] is None and not c["language_code"])
    english = next(c for c in calls if c["language_code"] == "en")
    names = [name for name, _ in default["commands"]]
    assert "language" in names
    assert "done" not in names
    assert [name for name, _ in english["commands"]] == names
    assert all(desc for _, desc in english["commands"])
    assert english["commands"] != default["commands"]


async def test_menu_registration_failure_does_not_break_startup(conn):
    class Bot:
        async def set_my_commands(self, *args, **kwargs):
            raise RuntimeError("Bot API down")

    class App:
        def __init__(self):
            self.bot = Bot()

        bot_data: dict = {}

    await make_post_init([ADMIN_ID], conn)(App())  # не должно бросить


# --- изоляция между пользователями ----------------------------------------
OTHER = 778


async def test_switch_touches_only_the_caller_row(conn, handlers):
    """`/language` пишет ровно одну строку `seen_users` - свою."""
    db.add_user(conn, USER, added_by=ADMIN_ID)
    db.add_user(conn, OTHER, added_by=ADMIN_ID)
    db.remember_user(conn, OTHER, "other", lang="ru")

    await pick_language(handlers, USER, "en")

    assert db.get_lang(conn, USER) == "en"
    assert db.get_lang(conn, OTHER) == "ru"
    # Никого больше в таблице выбор не задел.
    rows = dict(conn.execute("SELECT user_id, lang FROM seen_users").fetchall())
    assert rows[USER] == "en"
    assert rows[OTHER] == "ru"


async def test_other_user_keeps_russian_after_someone_switches(conn, handlers):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    db.add_user(conn, OTHER, added_by=ADMIN_ID)

    await pick_language(handlers, USER, "en")

    mine = make_update(USER)
    theirs = make_update(OTHER)
    await handlers["start"](mine, make_context())
    await handlers["start"](theirs, make_context())

    assert mine.effective_message.replies[0] == en.STRINGS["START"]
    assert theirs.effective_message.replies[0] == ru.STRINGS["START"]


async def test_admin_language_does_not_leak_into_user_replies(conn):
    """Язык админа не подменяет язык пользователя и наоборот."""
    db.set_lang(conn, ADMIN_ID, "en")
    access = Access(conn, [ADMIN_ID])
    handlers = make_handlers(access, TEST_CONFIG)
    db.add_user(conn, USER, added_by=ADMIN_ID)
    db.set_lang(conn, USER, "ru")

    admin_update = make_update(ADMIN_ID)
    user_update = make_update(USER)
    await handlers["start"](admin_update, make_context())
    await handlers["start"](user_update, make_context())

    assert admin_update.effective_message.replies[0] == en.STRINGS["START"]
    assert user_update.effective_message.replies[0] == ru.STRINGS["START"]


def test_catalog_objects_are_shared_but_not_per_user_state():
    """Кэш каталога - по языку, а не по пользователю: общий объект на язык
    и одинаковый для всех, поэтому "чужого" состояния в нём быть не может."""
    assert i18n_texts("ru") is i18n_texts("ru")
    assert i18n_texts("ru") is not i18n_texts("en")
