import asyncio as _asyncio

import pytest
import telegram.error as tg_error

from tgbot import utils
from tgbot.locales.ru import STRINGS as _RU

from .conftest import FakeMessage


class RaisingMessage:
    """Сообщение, чей reply_text падает на первом вызове заданной ошибкой,
    а на втором - отвечает успешно. Помнит все вызовы для проверки."""

    def __init__(self, error: Exception):
        self._error = error
        self._raised = False
        self.calls: list[dict] = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.calls.append({"text": text, "reply_markup": reply_markup, **kwargs})
        if not self._raised:
            self._raised = True
            raise self._error
        return self.calls[-1]


@pytest.mark.asyncio
async def test_reply_on_bad_request_resends_once_without_tags():
    message = RaisingMessage(tg_error.BadRequest("Can't parse entities"))

    await utils.reply(message, "<b>привет</b>")

    assert len(message.calls) == 2
    assert "parse_mode" in message.calls[0]
    assert "parse_mode" not in message.calls[1]
    assert message.calls[1]["text"] == "привет"


@pytest.mark.parametrize(
    "error",
    [
        tg_error.TimedOut(),
        tg_error.NetworkError("network down"),
        tg_error.RetryAfter(5),
    ],
)
@pytest.mark.asyncio
async def test_reply_on_other_errors_does_not_retry(error):
    message = RaisingMessage(error)

    with pytest.raises(type(error)):
        await utils.reply(message, "<b>привет</b>")

    assert len(message.calls) == 1


# --- utils.waiting: статус с таймером (show-wait-timer) ----------------------


async def _yield(_seconds):
    await _asyncio.sleep(0)


async def _spin(times: int = 20):
    for _ in range(times):
        await _asyncio.sleep(0)


def _elapsed(clock: str) -> str:
    return _RU["STATUS_ELAPSED"].format(clock=clock)


async def test_waiting_shows_the_status_without_time_before_the_first_tick(monkeypatch):
    async def never(_seconds):
        await _asyncio.Event().wait()

    monkeypatch.setattr(utils, "wait_sleep", never)
    message = FakeMessage()
    async with utils.waiting(message, "Ищу ответ..."):
        await _spin()
    assert message.replies == ["Ищу ответ..."]
    assert message.sent[0].edited == []
    assert message.deleted == ["Ищу ответ..."]


async def test_waiting_adds_the_elapsed_time_and_typing_on_each_tick(monkeypatch):
    monkeypatch.setattr(utils, "wait_sleep", _yield)
    message = FakeMessage()
    async with utils.waiting(message, "Ищу ответ..."):
        await _spin()
    edited = message.sent[0].edited
    assert edited[:2] == [
        f"Ищу ответ...\n{_elapsed('0:10')}",
        f"Ищу ответ...\n{_elapsed('0:20')}",
    ]
    assert message.chat_actions.count("typing") >= 1 + len(edited)


async def test_waiting_stops_ticking_after_the_block(monkeypatch):
    monkeypatch.setattr(utils, "wait_sleep", _yield)
    message = FakeMessage()
    async with utils.waiting(message, "Ищу ответ..."):
        await _spin()
    edits = len(message.sent[0].edited)
    await _spin()
    assert len(message.sent[0].edited) == edits
    assert message.deleted


async def test_waiting_survives_a_failing_edit(monkeypatch):
    monkeypatch.setattr(utils, "wait_sleep", _yield)
    message = FakeMessage()

    async def broken(*_args, **_kwargs):
        raise RuntimeError("message is not modified")

    async with utils.waiting(message, "Ищу ответ..."):
        message.sent[0].edit_text = broken
        await _spin()
        result = "ответ"
    assert result == "ответ"
    assert message.deleted


async def test_waiting_removes_the_status_when_the_block_raises(monkeypatch):
    monkeypatch.setattr(utils, "wait_sleep", _yield)
    message = FakeMessage()
    try:
        async with utils.waiting(message, "Ищу ответ..."):
            await _spin()
            raise ValueError("model refused")
    except ValueError:
        pass
    edits = len(message.sent[0].edited)
    await _spin()
    assert message.deleted and len(message.sent[0].edited) == edits


async def test_waiting_without_a_message_does_nothing():
    async with utils.waiting(None, "Ищу ответ..."):
        pass


async def test_waiting_without_text_only_types(monkeypatch):
    monkeypatch.setattr(utils, "wait_sleep", _yield)
    message = FakeMessage()
    async with utils.waiting(message, None):
        await _spin()
    assert message.replies == [] and message.chat_actions == ["typing"]


async def test_waiting_status_explains_the_next_rung(monkeypatch):
    """Подключение запасной модели меняет текст статуса (спека llm-model-ladder);
    запасного провайдера нет (ADR 0004), так что текст один."""
    async def never(_seconds):
        await _asyncio.Event().wait()

    monkeypatch.setattr(utils, "wait_sleep", never)
    message = FakeMessage()
    async with utils.waiting(message, "Ищу ответ...") as wait:
        await wait.on_step(1, "openrouter")
    assert message.sent[0].edited == [_RU["FAILURE_SLOW"]]


async def test_waiting_without_status_ignores_steps():
    message = FakeMessage()
    async with utils.waiting(message, None) as wait:
        await wait.on_step(1, "openrouter")
    assert message.replies == []


async def test_safe_answer_swallows_a_stale_callback_query():
    """После рестарта бот получает старые нажатия: Telegram отказывает в
    answerCallbackQuery, но сам шаг обработчика терять нельзя."""
    from types import SimpleNamespace

    import telegram.error

    from tgbot import utils

    async def stale(*args, **kwargs):
        raise telegram.error.BadRequest("Query is too old and response timeout expired")

    await utils.safe_answer(SimpleNamespace(answer=stale))
    await utils.safe_answer(None)  # апдейт без callback_query


# --- session-log-completeness: реакция "принято" ставится один раз ----------


async def test_react_sets_the_reaction_once_per_message():
    message = FakeMessage("вопрос")
    await utils.react(message)
    await utils.react(message)
    assert len(message.reactions) == 1


async def test_react_sets_the_reaction_on_each_new_message():
    first, second = FakeMessage("раз"), FakeMessage("два")
    await utils.react(first)
    await utils.react(second)
    assert len(first.reactions) == 1 and len(second.reactions) == 1
