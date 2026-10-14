"""Отправка сообщений глазами Bot API: сбои, разметка, длинные тексты,
повторное нажатие, Справка, язык, отзывы, журнал."""

import asyncio

from tgbot import db, i18n

from ..fakes.llm_api import DEFAULT_TEXT
from ..fakes.telegram_api import CANT_PARSE, NOT_MODIFIED
from .conftest import ADMIN, ALLOWED, button

T = i18n.texts("ru")
EN = i18n.texts("en")
U = ALLOWED


def _is_failure(params) -> bool:
    return str(params.get("text", "")).startswith(T.FAILURE_BOT[:30])


# --- сбои -------------------------------------------------------------------

async def test_handler_crash_gives_incident_code_button_and_admin_push(bot):
    bot.tg.fail("sendMessage", "Bad Request: chat not found",
                when=lambda p: p.get("chat_id") == U and not _is_failure(p))
    mark = bot.send(U, "/help")
    failure = await bot.wait("sendMessage", chat=U, after=mark, contains=T.FAILURE_BOT[:30])
    code = T.FAILURE_CODE.split("{")[0]
    assert code in failure.params["text"] and button(failure, "fail:")
    await bot.wait("sendMessage", chat=ADMIN, after=mark)  # пуш админу

    mark = bot.click(U, failure.result, button(failure, "fail:"))
    edit = await bot.wait("editMessageReplyMarkup", chat=U, after=mark)
    assert not (edit.params.get("reply_markup") or {}).get("inline_keyboard")
    await bot.wait("sendMessage", chat=U, after=mark)


async def test_not_modified_is_not_reported_as_failure(bot):
    mark = bot.send(U, "Что такое кампания?")
    answer = await bot.wait("sendMessage", chat=U, after=mark, contains=DEFAULT_TEXT)
    bot.tg.fail("editMessageText", NOT_MODIFIED)
    mark = bot.click(U, answer.result, button(answer, "ref:bad:"))
    await bot.wait("editMessageText", chat=U, after=mark)
    await asyncio.sleep(0.5)
    assert not bot.tg.calls_of("sendMessage", _is_failure)


# --- разметка и длина -------------------------------------------------------

async def test_html_rejected_is_resent_without_tags(bot):
    bot.tg.fail("sendMessage", CANT_PARSE,
                when=lambda p: p.get("parse_mode") == "HTML" and DEFAULT_TEXT in p.get("text", ""))
    mark = bot.send(U, "Что такое кампания?")
    plain = await bot.wait("sendMessage", chat=U, after=mark,
                           contains=DEFAULT_TEXT, count=2)
    assert plain[0].params.get("parse_mode") == "HTML"
    assert plain[1].params.get("parse_mode") is None and "<" not in plain[1].params["text"]


async def test_long_text_is_split_into_several_messages(bot):
    for uid in range(5000, 5400):
        db.add_user(bot.conn, uid, ADMIN)
    mark = bot.send(ADMIN, "/users")
    await asyncio.sleep(1.5)
    parts = [c for c in bot.tg.calls[mark:]
             if c.method == "sendMessage" and c.params.get("chat_id") == ADMIN]
    assert len(parts) >= 2
    assert all(len(c.params["text"]) <= 4096 for c in parts)


# --- повторное нажатие -------------------------------------------------------

async def test_double_press_on_same_rating_is_ignored(bot):
    mark = bot.send(U, "Что такое кампания?")
    answer = await bot.wait("sendMessage", chat=U, after=mark, contains=DEFAULT_TEXT)
    bot.llm.reply("Второй ответ фейка.", sticky=True)
    data = button(answer, "ref:bad:")
    mark = bot.click(U, answer.result, data)
    await bot.wait("sendMessage", chat=U, after=mark, contains="Второй ответ фейка.")
    mark = bot.click(U, answer.result, data)
    await bot.wait("answerCallbackQuery", after=mark)
    await asyncio.sleep(0.5)
    retries = bot.tg.calls_of("sendMessage", lambda p: "Второй ответ фейка." in str(p.get("text")))
    assert len(retries) == 1
    assert not bot.tg.calls_of("sendMessage", _is_failure)


# --- Справка -----------------------------------------------------------------

async def test_reference_shows_reaction_status_then_answer(bot):
    mark = bot.send(U, "Что такое кампания?")
    answer = await bot.wait("sendMessage", chat=U, after=mark, contains=DEFAULT_TEXT)
    methods = [c.method for c in bot.tg.calls[mark:bot.tg.calls.index(answer)]]
    assert "setMessageReaction" in methods and "deleteMessage" in methods
    assert answer.params.get("parse_mode") == "HTML"
    assert T.REFERENCE_UNVERIFIED not in answer.params["text"]


async def test_reference_bad_rating_gives_second_answer(bot):
    mark = bot.send(U, "Что такое кампания?")
    answer = await bot.wait("sendMessage", chat=U, after=mark, contains=DEFAULT_TEXT)
    bot.llm.reply("Второй ответ фейка.")
    mark = bot.click(U, answer.result, button(answer, "ref:bad:"))
    await bot.wait("sendMessage", chat=U, after=mark, contains="Второй ответ фейка.")


async def test_reference_without_tools_is_marked_unverified(bot):
    bot.llm.use_tools = False
    mark = bot.send(U, "Что такое кампания?")
    answer = await bot.wait("sendMessage", chat=U, after=mark, contains=DEFAULT_TEXT)
    assert T.REFERENCE_UNVERIFIED in answer.params["text"]


async def test_button_without_handler_is_answered_silently(bot):
    """Кнопка старой версии бота: часики гаснут, в чат ничего (click-idempotency)."""
    shown = await bot.ask(U, "/help")
    mark = bot.click(U, shown.result, "gone:1")
    await bot.wait("answerCallbackQuery", after=mark)
    await bot.silent(U, mark)


# --- язык, отзывы, журнал ---------------------------------------------------

async def test_language_switch_sends_new_message_in_english(bot):
    prompt = await bot.ask(U, "/language")
    mark = bot.click(U, prompt.result, "lang:en")
    await bot.wait("answerCallbackQuery", after=mark)
    confirm = await bot.wait("sendMessage", chat=U, after=mark)
    assert confirm.params["text"] == EN.LANGUAGE_SET
    start = await bot.ask(U, "/start")
    assert EN.START in start.params["text"]


async def test_support_then_admin_reads_feedback(bot):
    saved = await bot.ask(U, "/support бот долго думает")
    assert saved.params["text"].startswith(T.SUPPORT_SAVED[:20])
    mark = bot.send(ADMIN, "/feedback")
    card = await bot.wait("sendMessage", chat=ADMIN, after=mark, contains="бот долго думает")
    mark = bot.click(ADMIN, card.result, button(card, "fb_read:"))
    await bot.wait("answerCallbackQuery", after=mark)
    await bot.wait("editMessageReplyMarkup", chat=ADMIN, after=mark)


async def test_logs_sends_log_file(bot):
    page = await bot.ask(ADMIN, "/logs")
    mark = bot.click(ADMIN, page.result, button(page, "logs:f:"))
    doc = await bot.wait("sendDocument", chat=ADMIN, after=mark)
    assert doc.params["document"]["size"] > 0


# --- лимиты Telegram -----------------------------------------------------------

async def test_flood_limit_is_retried_and_reply_arrives(bot):
    """429 с retry_after - повтор после паузы, а не потерянный ответ (core.telegram.org:
    в тестовом окружении лимиты не выше, бот обязан ретраить)."""
    bot.tg.fail("sendMessage", "Too Many Requests: retry after 1", status=429, retry_after=1,
                when=lambda p: p.get("chat_id") == U)
    mark = bot.send(U, "/start")
    calls = await bot.wait("sendMessage", chat=U, after=mark, count=2, timeout=8)
    assert calls[0].result is None and calls[1].result is not None
    assert calls[1].at - calls[0].at >= 1
    assert T.START in calls[1].params["text"]


async def test_long_flood_wait_is_not_waited_out(bot):
    """retry_after больше потолка: ждать минуты внутри апдейта нельзя - как раньше."""
    bot.tg.fail("sendMessage", "Too Many Requests: retry after 600", status=429,
                retry_after=600, when=lambda p: p.get("chat_id") == U)
    mark = bot.send(U, "/start")
    await bot.wait("sendMessage", chat=U, after=mark)
    await asyncio.sleep(1)
    assert len(bot.tg.calls_of("sendMessage", lambda p: p.get("chat_id") == U)) == 1
