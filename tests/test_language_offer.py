"""Подсказка о смене языка и предложение переключиться по языку сообщения
(спека localization)."""

from datetime import datetime, timedelta, UTC

import pytest

from tgbot.handlers import reference as reference_handlers
from tgbot import db, i18n
from tgbot.agent.result import AgentResult
from tgbot.locales import en, ru

from .conftest import ADMIN_ID, buttons, log_rows, make_context, make_update

USER = 4242
ENGLISH = "How do I configure the voice agent greeting?"
RUSSIAN = "Как настроить приветствие голосового агента?"


# --- определение языка текста ----------------------------------------------
@pytest.mark.parametrize("text, lang", [
    (ENGLISH, "en"),
    (RUSSIAN, "ru"),
    ("ok", None),
    ("привет", None),  # меньше 20 букв
    ("Почему в логах agent_runtime ошибка timeout на https://example.com/x", None),
    ("", None),
    (None, None),
])
def test_text_lang(text, lang):
    assert i18n.text_lang(text) == lang


# --- журнал ----------------------------------------------------------------
def test_last_event_ts(conn):
    assert db.last_event_ts(conn, USER, "language_offer") is None
    db.log_event(conn, USER, "language_offer", {})
    db.log_event(conn, USER + 1, "language_offer", {})
    ts = db.last_event_ts(conn, USER, "language_offer")
    assert datetime.fromisoformat(ts) <= datetime.now(UTC)


# --- /start ------------------------------------------------------------------
@pytest.mark.parametrize("code", ["ru", "en"])
async def test_start_mentions_language_command(conn, handlers, code):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    db.set_lang(conn, USER, code)
    update = make_update(USER, text="/start")
    await handlers["start"](update, make_context())
    reply = update.effective_message.replies[0]
    assert "/language" in reply
    assert ru.STRINGS["START"].splitlines()[-1] == en.STRINGS["START"].splitlines()[-1]


# --- предложение -------------------------------------------------------------
@pytest.fixture
def agent(monkeypatch):
    async def fake_call_agent(config, conn, prompt, user_id, context_label, **kwargs):
        return AgentResult("Ответ.", files_read=[])

    monkeypatch.setattr(reference_handlers, "call_agent", fake_call_agent)


async def send(handlers, text, chat_type="private"):
    update = make_update(USER, text=text, chat_type=chat_type)
    await handlers["fallback"](update, make_context())
    return update


def offers(update):
    marks = ("Вы пишете по-", "You're writing in")
    return [r for r in update.effective_message.replies if r.startswith(marks)]


def age_last_offer(conn, days):
    old = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    conn.execute("UPDATE log SET ts = ? WHERE kind = 'language_offer'", (old,))
    conn.commit()


async def test_offer_on_english_text_in_russian_interface(conn, handlers, agent):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    update = await send(handlers, ENGLISH)

    [offer] = offers(update)
    assert "/language" in offer and "30" in offer
    # Двуязычное: есть и кириллица, и латиница.
    assert i18n.text_lang(offer) is None
    offer_index = update.effective_message.replies.index(offer)
    assert [data for _, data in buttons(update, offer_index)] == ["lang:en", "lang:ru"]
    # Сам вопрос обработан как обычно.
    assert len(update.effective_message.replies) > 1
    assert log_rows(conn, "language_offer")[0]["to"] == "en"


async def test_offer_on_russian_text_in_english_interface(conn, handlers, agent):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    db.set_lang(conn, USER, "en")
    update = await send(handlers, RUSSIAN)
    assert offers(update)
    offer_index = update.effective_message.replies.index(offers(update)[0])
    assert [data for _, data in buttons(update, offer_index)] == ["lang:ru", "lang:en"]


async def test_no_offer_when_language_matches(conn, handlers, agent):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    assert not offers(await send(handlers, RUSSIAN))


async def test_offer_at_most_once_in_30_days(conn, handlers, agent):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    assert offers(await send(handlers, ENGLISH))
    age_last_offer(conn, 7)
    assert not offers(await send(handlers, ENGLISH))
    age_last_offer(conn, 31)
    assert offers(await send(handlers, ENGLISH))


async def test_no_offer_for_commands_and_groups(conn, handlers, agent):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    assert not offers(await send(handlers, "/" + ENGLISH))
    assert not offers(await send(handlers, ENGLISH, chat_type="group"))
    assert log_rows(conn, "language_offer") == []


async def test_failed_offer_does_not_block_the_answer(conn, handlers, agent, monkeypatch):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    real = reference_handlers.utils.markup

    def broken(rows):
        if rows and rows[0][1] == "lang:en":
            raise RuntimeError("Bot API недоступен")
        return real(rows)

    monkeypatch.setattr(reference_handlers.utils, "markup", broken)
    update = await send(handlers, ENGLISH)
    assert update.effective_message.replies  # ответ на вопрос пришёл
    assert not offers(update)
    assert log_rows(conn, "language_offer") == []
