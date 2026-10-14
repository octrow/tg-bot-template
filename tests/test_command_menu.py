"""Меню команд из трёх уровней (clearer-bot-output, спека command-menu).

Чужой пользователь видел рабочие команды и получал отказ, допущенный -
`/whoami` и `/request_access`, которые ему не нужны. Теперь: ограниченное меню
по умолчанию, рабочее - персонально каждому допущенному, админу - рабочее плюс
админское. Меню хранит Telegram, поэтому `/deny` обязан перезаписать его.

Запуск: uv run pytest tests/test_command_menu.py -q
"""

from types import SimpleNamespace

from tgbot.handlers import common as tx
from tgbot import db
from tgbot.bot import make_post_init
from tgbot.locales import en, ru

from .conftest import ADMIN_ID, make_update

USER = 555
LIMITED = ["whoami", "request_access", "language"]
WORKING = ["start", "help", "echo", "support", "language"]
ADMIN_ONLY = ["allow", "deny", "users", "cost", "cache_clear", "logs", "feedback"]


class MenuBot:
    """Помнит каждый `set_my_commands`; `fail=True` - Bot API недоступен."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []

    async def set_my_commands(self, commands, scope=None, language_code=None, **kwargs):
        if self.fail:
            raise RuntimeError("Bot API недоступен")
        self.calls.append(
            {
                "names": [c.command for c in commands],
                "descriptions": [c.description for c in commands],
                "chat_id": getattr(scope, "chat_id", None),
                "language_code": language_code,
            }
        )

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        return SimpleNamespace(chat_id=chat_id, message_id=1, text=text)

    async def edit_message_text(self, **kwargs):
        pass

    def menu_of(self, chat_id: int) -> dict:
        """Последнее меню, поставленное этому чату."""
        return [c for c in self.calls if c["chat_id"] == chat_id][-1]


class App:
    def __init__(self, bot):
        self.bot = bot

    bot_data: dict = {}


def ctx(bot, *args: str):
    return SimpleNamespace(args=list(args), bot=bot)


# --- 3.1 наборы и post_init ------------------------------------------------


def test_the_three_command_sets() -> None:
    assert list(tx.LIMITED_COMMAND_NAMES) == LIMITED
    assert list(tx.USER_COMMAND_NAMES) == WORKING
    assert [n for n, _ in tx.menu_commands("ru", "admin")] == WORKING + ADMIN_ONLY


async def test_the_default_menu_is_the_limited_one_in_both_languages(conn):
    bot = MenuBot()
    await make_post_init([ADMIN_ID], conn)(App(bot))
    defaults = [c for c in bot.calls if c["chat_id"] is None]
    assert [c["language_code"] for c in defaults] == [None, "en"]
    assert all(c["names"] == LIMITED for c in defaults)
    assert all(all(c["descriptions"]) for c in defaults)
    assert defaults[0]["descriptions"] != defaults[1]["descriptions"]


async def test_every_allowed_user_gets_the_working_menu_in_their_language(conn):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    db.set_lang(conn, USER, "en")
    bot = MenuBot()
    await make_post_init([ADMIN_ID], conn)(App(bot))
    menu = bot.menu_of(USER)
    assert menu["names"] == WORKING
    assert menu["descriptions"][0] == en.STRINGS["CMD_DESCRIPTIONS"]["start"]


async def test_the_admin_gets_the_working_and_the_admin_menu(conn):
    db.add_user(conn, ADMIN_ID, added_by=ADMIN_ID)  # админ и в allowlist тоже
    bot = MenuBot()
    await make_post_init([ADMIN_ID], conn)(App(bot))
    assert bot.menu_of(ADMIN_ID)["names"] == WORKING + ADMIN_ONLY
    assert len([c for c in bot.calls if c["chat_id"] == ADMIN_ID]) == 1


# --- 3.3 /allow, одобрение заявки, /deny, /language -----------------------


async def test_allow_gives_the_working_menu(conn, handlers):
    bot = MenuBot()
    await handlers["allow"](make_update(ADMIN_ID, text=f"/allow {USER}"), ctx(bot, str(USER)))
    assert bot.menu_of(USER)["names"] == WORKING


async def test_deny_puts_the_limited_menu_back_explicitly(conn, handlers):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    bot = MenuBot()
    await handlers["deny"](make_update(ADMIN_ID, text=f"/deny {USER}"), ctx(bot, str(USER)))
    assert bot.menu_of(USER)["names"] == LIMITED
    assert not db.is_allowed(conn, USER)


async def test_approving_an_access_request_gives_the_working_menu(conn, handlers):
    bot = MenuBot()
    await handlers["request_access"](make_update(USER), ctx(bot))
    request = db.open_access_request(conn, USER)
    await handlers["access_callback"](
        make_update(USER, callback_data=f"access:skip:{request['id']}"), ctx(bot)
    )
    await handlers["access_callback"](
        make_update(ADMIN_ID, callback_data=f"access:approve:{request['id']}"), ctx(bot)
    )
    assert bot.menu_of(USER)["names"] == WORKING


async def test_language_rebuilds_the_current_tier_in_the_new_language(conn, handlers):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    bot = MenuBot()
    await handlers["language_callback"](make_update(USER, callback_data="lang:en"), ctx(bot))
    menu = bot.menu_of(USER)
    assert menu["names"] == WORKING
    assert menu["descriptions"][0] == en.STRINGS["CMD_DESCRIPTIONS"]["start"]

    await handlers["language_callback"](make_update(ADMIN_ID, callback_data="lang:ru"), ctx(bot))
    admin_menu = bot.menu_of(ADMIN_ID)
    assert admin_menu["names"] == WORKING + ADMIN_ONLY
    assert admin_menu["descriptions"][0] == ru.STRINGS["CMD_DESCRIPTIONS"]["start"]


async def test_a_failing_bot_api_does_not_cancel_the_action(conn, handlers):
    bot = MenuBot(fail=True)
    allow = make_update(ADMIN_ID, text=f"/allow {USER}")
    await handlers["allow"](allow, ctx(bot, str(USER)))
    assert db.is_allowed(conn, USER)
    assert allow.effective_message.replies == [ru.STRINGS["ALLOW_ADDED"].format(target=USER)]

    await handlers["language_callback"](make_update(USER, callback_data="lang:en"), ctx(bot))
    assert db.get_lang(conn, USER) == "en"

    deny = make_update(ADMIN_ID, text=f"/deny {USER}")
    await handlers["deny"](deny, ctx(bot, str(USER)))
    assert not db.is_allowed(conn, USER)
    assert deny.effective_message.replies == [ru.STRINGS["DENY_REMOVED"].format(target=USER)]


# --- убранная из меню команда работает -------------------------------------


async def test_whoami_still_works_for_an_allowed_user(conn, handlers):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    update = make_update(USER, text="/whoami")
    await handlers["whoami"](update, ctx(MenuBot()))
    assert str(USER) in update.effective_message.replies[0]


# --- 3.4 /start и /help не зовут служебные команды рабочими ----------------


def test_start_and_help_do_not_advertise_the_limited_commands() -> None:
    from tgbot.handlers.common import help_text

    for lang, catalog in (("ru", ru.STRINGS), ("en", en.STRINGS)):
        for is_admin in (False, True):
            text = help_text("gpt-test", is_admin=is_admin, lang=lang)
            assert "/whoami" not in text and "/request_access" not in text
            assert "/support" in text
        assert "/whoami" not in catalog["START"]
        assert "/request_access" not in catalog["START"]


# --- /start чинит меню, не поставленное раньше -----------------------------


async def test_start_applies_the_menu_that_allow_failed_to_set_once(conn, handlers):
    # /allow для того, кто ещё не открывал чат с ботом: Bot API отвечает
    # "chat not found", меню не встало. Первый /start ставит рабочее меню.
    failing = MenuBot(fail=True)
    await handlers["allow"](make_update(ADMIN_ID, text=f"/allow {USER}"), ctx(failing, str(USER)))
    bot = MenuBot()
    await handlers["start"](make_update(USER, text="/start"), ctx(bot))
    assert bot.menu_of(USER)["names"] == WORKING

    await handlers["start"](make_update(USER, text="/start"), ctx(bot))
    assert len([c for c in bot.calls if c["chat_id"] == USER]) == 1


async def test_start_does_not_repeat_a_menu_that_allow_already_set(conn, handlers):
    bot = MenuBot()
    await handlers["allow"](make_update(ADMIN_ID, text=f"/allow {USER}"), ctx(bot, str(USER)))
    await handlers["start"](make_update(USER, text="/start"), ctx(bot))
    assert len([c for c in bot.calls if c["chat_id"] == USER]) == 1
