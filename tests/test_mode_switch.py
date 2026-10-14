"""Переход из Справки в режим по просьбе словами (спека bot-modes): кнопка под
ответом Справки, метка режима - из реестра режимов (`handlers.registry`).

Классификатор подменён на уровне `intent`: тест проверяет, что бот делает с его
решением, а не typesafe.ai. Цель перехода - демо-режим `/echo`.

Запуск: uv run pytest tests/test_mode_switch.py -q
"""

from types import SimpleNamespace

import pytest

from tgbot import db, intent
from tgbot.locales.ru import STRINGS as RU

from .conftest import make_handlers, ADMIN_ID, TEST_CONFIG, buttons, log_rows, make_context, make_update
from . import test_reference_bot

# Заглушка агента Справки из соседнего модуля.
agent = test_reference_bot.agent

U = 42
GO_ECHO = RU["MODE_GO"]["echo"]


@pytest.fixture
def jev(monkeypatch, conn):
    """Решение классификатора Справки: `track` - метка или None."""
    db.add_user(conn, U, added_by=ADMIN_ID)
    box = SimpleNamespace(track=None)

    async def classify(conn, user_id, api_key, text, intents):
        return box.track

    monkeypatch.setattr(intent, "classify", classify)
    return box


async def send(handlers, text):
    update = make_update(U, text=text)
    await handlers["fallback"](update, make_context())
    return update


def _data(update, prefix: str) -> str:
    return next(d for _, d in buttons(update) if d.startswith(prefix))


async def click(handlers, data):
    update = make_update(U, callback_data=data)
    await handlers["mode_callback"](update, make_context())
    return update


async def test_a_mode_request_gets_an_answer_and_a_switch_button(conn, handlers, jev, agent):
    jev.track = "echo"
    update = await send(handlers, "повтори за мной: привет")
    assert agent.calls  # Справка всё равно отвечает
    assert [label for label, _ in buttons(update)] == [RU["REFERENCE_UNCLEAR"], GO_ECHO]
    row = log_rows(conn, "mode_switch_offered")[0]
    assert (row["from"], row["to"]) == ("reference", "echo")


async def test_going_enters_echo_with_the_same_text(conn, handlers, jev, agent):
    jev.track = "echo"
    offer = await send(handlers, "повтори за мной: привет")
    went = await click(handlers, _data(offer, "mode:go:"))
    session = db.active_session(conn, U)
    assert (session["mode"], session["step"]) == ("echo", 1)
    assert went.effective_message.replies[-1] == "повтори за мной: привет"
    assert went.effective_message.headers[-1] == f"{RU['MODE_HEADERS']['echo']} · шаг 1"
    row = log_rows(conn, "mode_switch_chosen")[0]
    assert (row["from"], row["to"]) == ("reference", "echo")


async def test_no_mode_no_switch_button(conn, handlers, jev, agent):
    update = await send(handlers, "что такое кампания?")
    assert not any(d.startswith("mode:") for _, d in buttons(update))
    assert agent.calls


# --- новое сообщение, старые кнопки, рестарт --------------------------------


async def test_a_new_message_replaces_the_pending_offer(conn, handlers, jev, agent):
    jev.track = "echo"
    offer = await send(handlers, "повтори за мной")
    jev.track = None
    await send(handlers, "а что такое кампания?")

    update = await click(handlers, _data(offer, "mode:go:"))
    assert update.effective_message.replies == [RU["MODE_SWITCH_STALE"]]
    assert db.active_session(conn, U) is None


async def test_a_button_from_before_a_restart_asks_to_resend(conn, access, handlers, jev, agent):
    jev.track = "echo"
    offer = await send(handlers, "повтори за мной")
    restarted = make_handlers(access, TEST_CONFIG)
    update = make_update(U, callback_data=_data(offer, "mode:go:"))
    await restarted["mode_callback"](update, make_context())
    assert update.effective_message.replies == [RU["MODE_SWITCH_LOST"]]


# --- ревью: контекст алертов -------------------------------------------------


async def test_admin_alerts_carry_no_mode_header():
    """M1: пуш админу создаётся из шага режима, но шапки режима не получает."""
    import asyncio

    from tgbot import alerts, modes
    from tgbot.modes import Mode

    seen = []

    class Bot:
        async def send_message(self, chat_id, text, **kwargs):
            seen.append((modes.get(), modes.ui_lang.get()))

    alerts.reset()
    alerts.configure(Bot(), [1])
    modes.enter(Mode.REFERENCE)
    modes.ui_lang.set("en")
    alerts.notify("timeout", 7)
    await asyncio.gather(*alerts._tasks)
    alerts.reset()
    assert seen == [(None, None)]
