"""Несколько пользователей разных ролей одновременно: состояния не протекают.

`concurrent_updates(True)`: апдейты разных пользователей идут параллельно,
поэтому проверяем не порядок, а адресата каждого ответа."""

import asyncio

from tgbot import i18n

from .conftest import ADMIN, ALLOWED, STRANGER, button

T = i18n.texts("ru")


async def test_two_users_reference_answers_go_to_their_askers(bot):
    mark = bot.tg.mark()
    bot.send(ADMIN, "Вопрос админа про кампании")
    bot.send(ALLOWED, "Вопрос пользователя про звонки")
    await asyncio.gather(
        bot.wait("sendMessage", chat=ADMIN, after=mark, contains="Ответ фейка"),
        bot.wait("sendMessage", chat=ALLOWED, after=mark, contains="Ответ фейка"),
    )
    assert bot.llm.prompts("Вопрос админа") and bot.llm.prompts("Вопрос пользователя")


async def test_access_request_approved_while_admin_asks_reference(bot):
    mark = bot.send(ADMIN, "Вопрос админа про кампании")
    ask_name = await bot.ask(STRANGER, "/request_access")
    assert ask_name.params["text"] == T.ACCESS_QUESTION
    await bot.wait("sendMessage", chat=ADMIN, after=mark, contains="Ответ фейка")
    mark = bot.send(STRANGER, "Иван Петров, отдел продаж")
    card = await bot.wait("sendMessage", chat=ADMIN, after=mark, contains=str(STRANGER))
    mark = bot.click(ADMIN, card.result, button(card, "access:approve:"))
    await bot.wait("sendMessage", chat=STRANGER, after=mark, contains=T.ACCESS_GRANTED)
    start = await bot.ask(STRANGER, "/start")
    assert T.START in start.params["text"]


async def test_access_request_denied_keeps_stranger_out(bot):
    await bot.ask(STRANGER, "/request_access")
    mark = bot.send(STRANGER, "Иван Петров")
    card = await bot.wait("sendMessage", chat=ADMIN, after=mark, contains=str(STRANGER))
    mark = bot.click(ADMIN, card.result, button(card, "access:deny:"))
    await bot.wait("sendMessage", chat=STRANGER, after=mark, contains=T.ACCESS_REJECTED)
    await bot.wait("editMessageText", chat=ADMIN, after=mark)
    refusal = await bot.ask(STRANGER, "/help")
    assert refusal.params["text"] == T.REFUSAL
