"""Демо-режим `/echo`: сессия режима, шапка, шаги, выход в Справку.

Агент Справки - заглушка из `test_reference_bot`.

Запуск: uv run pytest tests/test_echo.py -q
"""

from tgbot import db
from tgbot.locales.ru import STRINGS as RU

from .conftest import ADMIN_ID, buttons, log_rows, make_context, make_update
from . import test_reference_bot

agent = test_reference_bot.agent

U = 42
EXIT = "echo:exit"


async def command(handlers, name, *args, user_id=U):
    update = make_update(user_id, text=f"/{name} {' '.join(args)}".strip())
    await handlers[name](update, make_context(*args))
    return update


async def send(handlers, text, user_id=U):
    update = make_update(user_id, text=text)
    await handlers["fallback"](update, make_context())
    return update


async def click(handlers, data, user_id=U):
    update = make_update(user_id, callback_data=data)
    await handlers["echo_callback"](update, make_context())
    return update


def _allow(conn):
    db.add_user(conn, U, added_by=ADMIN_ID)


async def test_echo_opens_a_session_and_shows_the_header(conn, handlers):
    _allow(conn)
    update = await command(handlers, "echo")
    session = db.active_session(conn, U)
    assert (session["mode"], session["step"]) == ("echo", 0)
    assert update.effective_message.headers[-1] == RU["MODE_HEADERS"]["echo"]
    assert update.effective_message.replies[-1] == RU["ECHO_HINT"]
    assert buttons(update) == [(RU["ECHO_EXIT_BUTTON"], EXIT)]


async def test_each_text_is_echoed_with_the_next_step(conn, handlers, agent):
    _allow(conn)
    await command(handlers, "echo")
    first = await send(handlers, "раз")
    second = await send(handlers, "два")
    assert first.effective_message.replies == ["раз"]
    assert first.effective_message.headers == [f"{RU['MODE_HEADERS']['echo']} · шаг 1"]
    assert second.effective_message.headers == [f"{RU['MODE_HEADERS']['echo']} · шаг 2"]
    assert buttons(second) == [(RU["ECHO_EXIT_BUTTON"], EXIT)]
    assert db.active_session(conn, U)["step"] == 2
    assert agent.calls == []  # Справка молчит


async def test_exit_closes_the_session_and_text_goes_to_reference(conn, handlers, agent):
    _allow(conn)
    await command(handlers, "echo")
    left = await click(handlers, EXIT)
    assert left.effective_message.replies[-1] == RU["ECHO_LEFT"]
    assert db.active_session(conn, U) is None
    await send(handlers, "что такое нода?")
    assert agent.calls


async def test_a_stale_exit_button_only_answers_the_click(conn, handlers):
    _allow(conn)
    update = await click(handlers, EXIT)
    assert update.callback_query.answered
    assert update.effective_message.replies == []


async def test_a_stale_exit_button_loses_its_keyboard(conn, handlers):
    """Режим уже закрыт: кнопка снимается, как у устаревшей кнопки перехода,
    ответа текстом нет."""
    _allow(conn)
    update = await click(handlers, EXIT)
    assert update.effective_message.markup_edits == [None]
    assert update.effective_message.replies == []


async def test_start_leaves_echo(conn, handlers):
    _allow(conn)
    await command(handlers, "echo")
    await command(handlers, "start")
    assert db.active_session(conn, U) is None


async def test_echo_again_starts_a_new_session(conn, handlers):
    _allow(conn)
    await command(handlers, "echo")
    first = db.active_session(conn, U)["id"]
    await send(handlers, "раз")
    await command(handlers, "echo")
    session = db.active_session(conn, U)
    assert session["id"] != first and session["step"] == 0
    assert db.get_session(conn, first)["status"] == db.DONE_STATUS


async def test_echo_text_opens_no_reference_log_session(conn, handlers):
    _allow(conn)
    await command(handlers, "echo")
    update = make_update(U, text="раз")
    await handlers["log_update"](update, make_context())
    await handlers["fallback"](update, make_context())
    statuses = [r["status"] for r in conn.execute("SELECT status FROM sessions")]
    assert statuses == ["active"]


async def test_echo_needs_access(conn, handlers):
    update = await command(handlers, "echo", user_id=7)
    assert db.active_session(conn, 7) is None
    assert RU["REFUSAL"] in update.effective_message.replies[-1]


async def test_feedback_and_cost_use_the_echo_session(conn, handlers):
    _allow(conn)
    await command(handlers, "echo")
    session_id = db.active_session(conn, U)["id"]
    await command(handlers, "support", "эхо", "сломалось")
    assert db.recent_feedback(conn)[0]["session_id"] == session_id
    cost = await command(handlers, "cost", str(session_id), user_id=ADMIN_ID)
    assert f"Сессия {session_id} (active)" in cost.effective_message.replies[-1]
    assert log_rows(conn, "echo_entered")


async def test_a_step_after_concurrent_exit_does_not_reopen_the_session(conn, access, handlers):
    """Выход закрыл сессию, пока шаг держал её старый снимок: шаг молча
    ничего не делает, сессия остаётся закрытой (спека echo-mode)."""
    from tgbot.bot import build_deps
    from tgbot.handlers import echo

    from .conftest import TEST_CONFIG, FakeMessage

    _allow(conn)
    await command(handlers, "echo")
    stale = db.active_session(conn, U)
    await click(handlers, EXIT)
    message = FakeMessage("поздний текст")

    await echo.text_step(build_deps(access, TEST_CONFIG), message, "ru", stale, "поздний текст")

    assert db.active_session(conn, U) is None
    assert db.get_session(conn, stale["id"])["status"] != "active"
    assert message.replies == []
    assert log_rows(conn, "echo_step") == []
