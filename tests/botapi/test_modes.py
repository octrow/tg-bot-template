"""Режим по умолчанию - Справка: свободный текст и шапка режима.

Пользователь разрешённый, но не админ: у админа те же режимы, плюс пуши,
которые здесь только мешали бы искать ответ."""

from tgbot import i18n

from ..fakes.llm_api import DEFAULT_TEXT
from .conftest import ALLOWED

T = i18n.texts("ru")
U = ALLOWED


async def _answer(bot, mark, contains=DEFAULT_TEXT):
    return await bot.wait("sendMessage", chat=U, after=mark, contains=contains)


async def test_free_text_outside_modes_is_reference(bot):
    mark = bot.send(U, "Что такое кампания?")
    await _answer(bot, mark)
    assert bot.llm.prompts("Что такое кампания?")


async def test_start_then_text_goes_to_reference(bot):
    start = await bot.ask(U, "/start")
    assert T.START in start.params["text"]
    mark = bot.send(U, "Что такое кампания?")
    await _answer(bot, mark)


# --- шапка 'режим · этап' -----------------------------------------

REF_HEAD = "💬 Справка\n\n"


def _text(call) -> str:
    return str(call.params.get("text", ""))


async def test_reference_answer_starts_with_the_reference_header(bot):
    mark = bot.send(U, "Что такое кампания?")
    answer = await _answer(bot, mark)
    assert _text(answer).startswith(REF_HEAD)


async def test_english_reference_header(bot):
    mark = bot.send(U, "What is a campaign?", lang="en")
    answer = await _answer(bot, mark)
    assert _text(answer).startswith("💬 Help\n\n")


async def test_help_has_no_mode_header(bot):
    help_reply = await bot.ask(U, "/help")
    assert not _text(help_reply).startswith(("💬", "🔁"))
