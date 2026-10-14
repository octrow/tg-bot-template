"""Демо-режим `/echo` на уровне Bot API: вход, шаги с шапкой, выход в Справку,
и переход в `/echo` кнопкой под ответом Справки (метка из реестра режимов)."""

import pytest

from tgbot import i18n, intent

from ..fakes.llm_api import DEFAULT_TEXT
from .conftest import ALLOWED

T = i18n.texts("ru")
U = ALLOWED
HEAD = T.MODE_HEADERS["echo"]


def _text(call) -> str:
    return str(call.params.get("text", ""))


async def _echoed(bot, text: str):
    mark = bot.send(U, text)
    return await bot.wait("sendMessage", chat=U, after=mark, contains=text)


async def test_echo_steps_then_exit_to_reference(bot):
    entered = await bot.ask(U, "/echo")
    assert _text(entered) == f"{HEAD}\n\n{T.ECHO_HINT}"

    first = await _echoed(bot, "раз")
    second = await _echoed(bot, "два")
    assert _text(first) == f"{HEAD} · шаг 1\n\nраз"
    assert _text(second) == f"{HEAD} · шаг 2\n\nдва"

    await bot.press(U, second, "echo:exit", contains=T.ECHO_LEFT)
    mark = bot.send(U, "Что такое кампания?")
    answer = await bot.wait("sendMessage", chat=U, after=mark, contains=DEFAULT_TEXT)
    assert _text(answer).startswith(T.MODE_HEADERS["reference"])
    assert bot.llm.prompts("Что такое кампания?")


async def test_reference_offers_echo_and_the_button_enters_it(bot, monkeypatch):
    async def choices(api_key, text, questions, timeout):
        return {name: {"question": 0.05, "chat": 0.05, "echo": 0.9} for name in questions}, None

    monkeypatch.setattr(intent.jev, "choices", choices)
    mark = bot.send(U, "повтори за мной: привет")
    answer = await bot.wait("sendMessage", chat=U, after=mark, contains=DEFAULT_TEXT)

    went = await bot.press(U, answer, "mode:go:", contains="повтори за мной: привет")
    assert _text(went) == f"{HEAD} · шаг 1\n\nповтори за мной: привет"


@pytest.mark.packs
async def test_a_message_pack_is_one_echo_step(bot):
    await bot.ask(U, "/echo")
    mark = bot.send(U, "раз")
    bot.send(U, "два")
    echoed = await bot.wait("sendMessage", chat=U, after=mark, contains="два")
    assert _text(echoed) == f"{HEAD} · шаг 1\n\nраз\nдва"
