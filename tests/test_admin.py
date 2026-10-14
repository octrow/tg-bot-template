from types import SimpleNamespace

from tgbot import db, journal, logs_view
from tgbot.bot import make_post_init
from tgbot.handlers.common import LIMITED_COMMAND_NAMES, admin_commands, user_commands
from tgbot.locales.ru import STRINGS as RU

from .conftest import make_handlers, TEST_CONFIG, ADMIN_ID, buttons, log_rows, make_context, make_update

ADMIN_REFUSAL = RU["ADMIN_REFUSAL"]
LOGS_EMPTY = RU["LOGS_EMPTY"]
LOGS_FILE_GONE = RU["LOGS_FILE_GONE"]
USER_COMMANDS = user_commands("ru")
ADMIN_COMMANDS = admin_commands("ru")


async def test_admin_allow_adds_user_and_logs(conn, handlers):
    update = make_update(ADMIN_ID)
    await handlers["allow"](update, make_context("42"))

    assert db.is_allowed(conn, 42)
    assert "42" in update.effective_message.replies[0]

    entries = log_rows(conn, "allow")
    assert len(entries) == 1
    assert entries[0]["user_id"] == ADMIN_ID
    assert entries[0]["target_user_id"] == 42
    assert entries[0]["changed"] is True
    assert entries[0]["ts"]


async def test_admin_deny_removes_user_and_logs(conn, handlers):
    db.add_user(conn, 42, added_by=ADMIN_ID)
    update = make_update(ADMIN_ID)
    await handlers["deny"](update, make_context("42"))

    assert not db.is_allowed(conn, 42)
    entries = log_rows(conn, "deny")
    assert entries[0]["target_user_id"] == 42
    assert entries[0]["changed"] is True


async def test_allow_survives_reconnect(conn, handlers, tmp_path):
    await handlers["allow"](make_update(ADMIN_ID), make_context("42"))
    conn.close()
    reopened = db.connect(tmp_path / "test.db")
    assert db.is_allowed(reopened, 42)
    reopened.close()


async def test_users_lists_allowlist(conn, handlers):
    db.add_user(conn, 42, added_by=ADMIN_ID)
    update = make_update(ADMIN_ID)
    await handlers["users"](update, make_context())
    reply = update.effective_message.replies[0]
    assert "42" in reply
    assert str(ADMIN_ID) in reply


async def test_non_admin_allowlisted_user_is_refused(conn, handlers):
    db.add_user(conn, 5, added_by=ADMIN_ID)
    for name in ("allow", "deny", "users"):
        update = make_update(5)
        await handlers[name](update, make_context("42"))
        assert update.effective_message.replies == [ADMIN_REFUSAL]
    assert not db.is_allowed(conn, 42)


async def test_stranger_admin_command_gets_access_refusal_not_admin_action(conn, handlers):
    update = make_update(77)
    await handlers["allow"](update, make_context("42"))
    assert not db.is_allowed(conn, 42)
    assert update.effective_message.replies and ADMIN_REFUSAL not in update.effective_message.replies


async def test_bad_argument_does_not_change_allowlist(conn, handlers):
    # Нечисловой аргумент трактуется как username; неизвестный - внятный ответ,
    # allowlist не меняется.
    update = make_update(ADMIN_ID)
    await handlers["allow"](update, make_context("не-число"))
    assert "ещё не писал боту" in update.effective_message.replies[0]
    assert db.list_users(conn) == []


async def test_allow_by_username_after_user_seen(conn, handlers):
    # Пользователь написал боту - бот его запомнил (username фиксируется в gate/whoami)
    db.remember_user(conn, 777, "new_colleague")
    update = make_update(ADMIN_ID, text="/allow @new_colleague")
    await handlers["allow"](update, make_context("@new_colleague"))
    assert db.is_allowed(conn, 777)
    assert "777" in update.effective_message.replies[0]


async def test_deny_by_username(conn, handlers):
    db.remember_user(conn, 778, "leaver")
    db.add_user(conn, 778, added_by=ADMIN_ID)
    update = make_update(ADMIN_ID, text="/deny leaver")
    await handlers["deny"](update, make_context("leaver"))
    assert not db.is_allowed(conn, 778)


async def test_allow_unknown_username_explains_whoami(conn, handlers):
    update = make_update(ADMIN_ID, text="/allow @stranger")
    await handlers["allow"](update, make_context("@stranger"))
    assert "@stranger" in update.effective_message.replies[0]
    assert "/whoami" in update.effective_message.replies[0]
    assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0


async def test_users_shows_username(conn, handlers):
    db.remember_user(conn, 779, "shown_name")
    db.add_user(conn, 779, added_by=ADMIN_ID)
    update = make_update(ADMIN_ID, text="/users")
    await handlers["users"](update, make_context())
    assert "@shown_name" in update.effective_message.replies[0]


async def test_help_shows_the_active_model_and_logs(conn, access):
    """Цепочки ступеней больше нет - в справке одна модель, действующая."""
    db.add_user(conn, ADMIN_ID, added_by=ADMIN_ID)
    config = SimpleNamespace(
        llm_provider="openrouter", openrouter_models=("z-ai/glm-5.3", "openai/gpt-5.6-luna")
    )
    handlers = make_handlers(access, config)
    update = make_update(ADMIN_ID, text="/help")

    await handlers["help"](update, make_context())

    reply = update.effective_message.replies[0]
    assert "Модель: z-ai/glm-5.3" in reply  # дублёр в справке не показываем
    assert "Claude" not in reply
    entries = log_rows(conn, "command")
    assert entries[-1]["command"] == "/help"


async def test_help_falls_back_to_stage_default_model(conn, access):
    db.add_user(conn, ADMIN_ID, added_by=ADMIN_ID)
    handlers = make_handlers(access, TEST_CONFIG)
    update = make_update(ADMIN_ID, text="/help")

    await handlers["help"](update, make_context())

    assert "z-ai/glm-5.3-flash" in update.effective_message.replies[0]


class FakeBot:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[list, object | None]] = []

    async def set_my_commands(self, commands, scope=None, **kwargs):
        if self.fail:
            raise RuntimeError("Bot API недоступен")
        self.calls.append((commands, scope))


async def test_post_init_registers_default_and_admin_menus():
    bot = FakeBot()
    app = SimpleNamespace(bot=bot)
    post_init = make_post_init([ADMIN_ID])

    await post_init(app)

    # Ограниченное меню по умолчанию - русское и английское вторым набором
    # (language_code="en"), затем персональное меню админа (спека command-menu).
    assert len(bot.calls) == 3
    default_commands, default_scope = bot.calls[0]
    assert default_scope is None
    assert [c.command for c in default_commands] == list(LIMITED_COMMAND_NAMES)
    assert all(c.description for c in default_commands)

    en_commands, en_scope = bot.calls[1]
    assert en_scope is None
    assert [c.command for c in en_commands] == list(LIMITED_COMMAND_NAMES)

    admin_commands, admin_scope = bot.calls[2]
    assert admin_scope.chat_id == ADMIN_ID
    expected = [name for name, _ in USER_COMMANDS] + [name for name, _ in ADMIN_COMMANDS]
    assert [c.command for c in admin_commands] == expected


async def test_post_init_swallows_bot_api_errors():
    app = SimpleNamespace(bot=FakeBot(fail=True))
    post_init = make_post_init([ADMIN_ID])

    await post_init(app)  # не должно бросить исключение


async def test_help_shows_admin_commands_only_to_admin(conn, handlers):
    db.add_user(conn, 5, added_by=ADMIN_ID)
    for user_id, expected in ((ADMIN_ID, True), (5, False)):
        update = make_update(user_id, text="/help")
        await handlers["help"](update, make_context())
        reply = update.effective_message.replies[0]
        assert "/support" in reply
        assert ("/allow" in reply) is expected
        assert ("/users" in reply) is expected
        assert ("/logs" in reply) is expected


async def test_cache_clear_removes_entries_and_reports_count(conn, handlers):
    db.cache_put(conn, "hash-1", "ответ", provider="claude")
    db.cache_put(conn, "hash-2", "ответ", provider="openai")

    update = make_update(ADMIN_ID)
    await handlers["cache_clear"](update, make_context())

    assert conn.execute("SELECT COUNT(*) AS n FROM agent_cache").fetchone()["n"] == 0
    assert "2" in update.effective_message.replies[0]
    assert log_rows(conn, "cache_cleared")[0]["removed"] == 2


async def test_cache_clear_refused_for_non_admin(conn, handlers):
    db.add_user(conn, 777, ADMIN_ID)
    db.cache_put(conn, "hash-1", "ответ")

    update = make_update(777)
    await handlers["cache_clear"](update, make_context())

    assert update.effective_message.replies == [ADMIN_REFUSAL]
    assert conn.execute("SELECT COUNT(*) AS n FROM agent_cache").fetchone()["n"] == 1


# --- /logs: доступ администратора к файлам журнала ------------------------

# Имена как у journal._log_filename: время начала сессии, чья она, номер сессии.
LOG_NAMES = [f"20260813-1904{i:02d}-u111222333-s{i}.jsonl" for i in range(20, 0, -1)]


def fake_listing(monkeypatch, names):
    """Перечисление файлов журнала - заглушкой: список собирает `db`, здесь
    проверяется команда (порядок "свежие первыми" задаёт сам список)."""
    files = [SimpleNamespace(name=n, size=100, mtime=float(-i)) for i, n in enumerate(names)]
    monkeypatch.setattr(journal, "list_log_files", lambda: files, raising=False)
    return files


def fake_render(monkeypatch, parts):
    """Заглушка сборки .md (`logs_view`): команда отвечает за отправку частей,
    а не за их нарезку."""
    monkeypatch.setattr(logs_view, "render_log_file", lambda path, lang: "\n".join(parts))
    monkeypatch.setattr(logs_view, "split_for_telegram", lambda text, *a, **kw: list(parts))


def logs_dir(monkeypatch, tmp_path, *names):
    directory = tmp_path / "logs"
    directory.mkdir(exist_ok=True)
    for name in names:
        (directory / name).write_text('{"kind": "command"}\n', encoding="utf-8")
    monkeypatch.setattr(journal, "logs_dir", lambda: directory, raising=False)
    return directory


def file_names(update) -> list[str]:
    return [d[len("logs:f:"):] for _, d in buttons(update) if d.startswith("logs:f:")]


async def test_logs_shows_first_page_with_buttons_and_next(conn, handlers, monkeypatch):
    fake_listing(monkeypatch, LOG_NAMES)
    update = make_update(ADMIN_ID, text="/logs")

    await handlers["logs"](update, make_context())

    rows = buttons(update)
    assert file_names(update) == LOG_NAMES[: len(file_names(update))]
    assert 0 < len(file_names(update)) < len(LOG_NAMES)  # это именно страница
    assert any(data == "logs:p:1" for _, data in rows)
    # callback_data Bot API ограничивает 64 байтами (решение 4).
    assert all(len(data.encode()) <= 64 for _, data in rows)
    # Подпись позволяет опознать сессию до открытия файла.
    assert any("111222333" in label for label, _ in rows)


async def test_logs_refused_for_non_admin_without_file_names(conn, handlers, monkeypatch):
    db.add_user(conn, 5, added_by=ADMIN_ID)
    fake_listing(monkeypatch, LOG_NAMES)
    update = make_update(5, text="/logs")

    await handlers["logs"](update, make_context())

    assert update.effective_message.replies == [ADMIN_REFUSAL]
    assert buttons(update) == []
    assert all(name not in ADMIN_REFUSAL for name in LOG_NAMES)


async def test_logs_pages_keep_order_and_lose_nothing(conn, handlers, monkeypatch):
    fake_listing(monkeypatch, LOG_NAMES)
    update = make_update(ADMIN_ID, text="/logs")
    await handlers["logs"](update, make_context())

    seen = file_names(update)
    rows = buttons(update)
    page = 1
    while any(data == f"logs:p:{page}" for _, data in rows):
        update = make_update(ADMIN_ID, callback_data=f"logs:p:{page}")
        await handlers["logs_callback"](update, make_context())
        rows = _edited_buttons(update)
        seen += [d[len("logs:f:"):] for _, d in rows if d.startswith("logs:f:")]
        page += 1

    assert seen == LOG_NAMES


def _edited_buttons(update) -> list[tuple[str, str]]:
    markup = update.effective_message.edit_markups[-1]
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def test_logs_paging_edits_the_same_message(conn, handlers, monkeypatch):
    """clearer-bot-output: переход по страницам не плодит сообщения."""
    fake_listing(monkeypatch, LOG_NAMES)
    update = make_update(ADMIN_ID, callback_data="logs:p:1")
    await handlers["logs_callback"](update, make_context())
    message = update.effective_message
    assert message.replies == []
    assert "2" in message.edits[-1]
    assert ("logs:p:0" in [d for _, d in _edited_buttons(update)])


async def test_logs_file_button_sends_session_content(conn, handlers, monkeypatch, tmp_path):
    name = LOG_NAMES[0]
    logs_dir(monkeypatch, tmp_path, name)
    fake_render(monkeypatch, ["# Журнал сессии\n\n    сюда ушло в модель\n"])
    update = make_update(ADMIN_ID, callback_data=f"logs:f:{name}")

    await handlers["logs_callback"](update, make_context())

    assert len(update.effective_message.documents) == 1
    document, filename = update.effective_message.documents[0]
    assert b"\xd1\x83\xd1\x88\xd0\xbb\xd0\xbe" in bytes(document)  # "ушло" в UTF-8
    assert filename.endswith(".md")
    assert "s20" in filename  # видно, чья сессия


async def test_logs_missing_file_answers_instead_of_raising(conn, handlers, monkeypatch, tmp_path):
    logs_dir(monkeypatch, tmp_path)  # файл удалён ротацией между показом и нажатием
    fake_render(monkeypatch, ["не должно понадобиться"])
    update = make_update(ADMIN_ID, callback_data=f"logs:f:{LOG_NAMES[0]}")

    await handlers["logs_callback"](update, make_context())

    assert update.effective_message.documents == []
    assert update.effective_message.replies == [LOGS_FILE_GONE.format(name=LOG_NAMES[0])]


async def test_logs_empty_directory_explains(conn, handlers, monkeypatch):
    fake_listing(monkeypatch, [])
    update = make_update(ADMIN_ID, text="/logs")

    await handlers["logs"](update, make_context())

    assert update.effective_message.replies == [LOGS_EMPTY]
    assert buttons(update) == []


async def test_logs_delivery_is_journalled_with_admin_and_file(conn, handlers, monkeypatch, tmp_path):
    name = LOG_NAMES[1]
    logs_dir(monkeypatch, tmp_path, name)
    fake_render(monkeypatch, ["# Журнал\n"])
    update = make_update(ADMIN_ID, callback_data=f"logs:f:{name}")

    await handlers["logs_callback"](update, make_context())

    entries = log_rows(conn, "log_file_sent")
    assert len(entries) == 1
    assert entries[0]["user_id"] == ADMIN_ID
    assert entries[0]["file"] == name


async def test_logs_big_file_goes_in_numbered_parts(conn, handlers, monkeypatch, tmp_path):
    name = LOG_NAMES[2]
    logs_dir(monkeypatch, tmp_path, name)
    fake_render(monkeypatch, ["часть один", "часть два", "часть три"])
    update = make_update(ADMIN_ID, callback_data=f"logs:f:{name}")

    await handlers["logs_callback"](update, make_context())

    documents = update.effective_message.documents
    assert len(documents) == 3
    assert [bytes(d).decode() for d, _ in documents] == ["часть один", "часть два", "часть три"]
    # Номер части виден в имени файла: в ленте части лежат подряд и не путаются.
    for number, (_, filename) in enumerate(documents, 1):
        assert f"part{number}of3" in filename
