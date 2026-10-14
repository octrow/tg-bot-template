"""Длинные сообщения (спека long-message-send)."""

import datetime as dt

import httpx
import pytest
import telegram.error
from telegram import Chat, InlineKeyboardMarkup, Message
from telegram.ext import ExtBot

from tgbot import db, utils
from tgbot.agent import cache, failures, ladder, openrouter
from tgbot.utils import TELEGRAM_TEXT_LIMIT as LIMIT, split_text, tg_len

from .conftest import log_rows


def test_text_at_the_limit_is_not_split():
    assert split_text("а" * LIMIT) == ["а" * LIMIT]
    parts = split_text("а" * (LIMIT + 1))
    assert len(parts) == 2 and all(tg_len(p) <= LIMIT for p in parts)


def test_paragraphs_are_the_first_cut():
    paras = [f"Абзац {i} " + "слово " * 300 for i in range(4)]
    parts = split_text("\n\n".join(paras))
    assert all(tg_len(p) <= LIMIT for p in parts)
    assert "\n\n".join(parts) == "\n\n".join(paras)  # разрезы ровно по абзацам


def test_html_bold_paragraph_is_never_torn():
    bold = "<b>" + "жирный " * 50 + "</b>"
    text = "\n\n".join(["обычный " * 400, bold, "ещё " * 600])
    parts = split_text(text, html_mode=True)
    assert len(parts) > 1
    assert all(p.count("<b>") == p.count("</b>") for p in parts)
    assert any(bold in p for p in parts)


def test_huge_html_paragraph_goes_without_tags():
    text = "<b>" + "слово " * 1500 + "</b>"
    parts = split_text(text, html_mode=True)
    assert len(parts) > 1 and all("<b>" not in p and "</b>" not in p for p in parts)
    assert all(tg_len(p) <= LIMIT for p in parts)


def test_emoji_counts_as_two_units():
    text = "🙂" * (LIMIT // 2 + 1)  # 4098 единиц UTF-16, хотя символов 2049
    assert tg_len(text) == LIMIT + 2
    parts = split_text(text)
    assert len(parts) == 2 and all(tg_len(p) <= LIMIT for p in parts)


@pytest.fixture
def sent(monkeypatch):
    """Перехват отправки на уровне ExtBot: сеть не нужна."""
    class _Calls(list):
        fail_on: dict

    calls = _Calls()
    calls.fail_on = {}
    fail_on = calls.fail_on

    async def fake_send(self, chat_id, text, **kwargs):
        calls.append({"text": text, **kwargs})
        error = fail_on.pop(len(calls), None)
        if error:
            raise error
        return f"msg{len(calls)}"

    async def fake_edit(self, text, *args, **kwargs):
        calls.append({"edit": text})
        return True

    monkeypatch.setattr(ExtBot, "send_message", fake_send)
    monkeypatch.setattr(ExtBot, "edit_message_text", fake_edit)
    return calls


def _message(bot) -> Message:
    message = Message(1, dt.datetime.now(dt.UTC), Chat(42, Chat.PRIVATE))
    message.set_bot(bot)
    return message


async def test_reply_of_9000_chars_is_three_messages(sent):
    bot = utils.SplittingBot("123:abc")
    keyboard = InlineKeyboardMarkup([])
    text = "\n\n".join("строка " * 100 for _ in range(13))  # ~9 100 символов
    assert len(text) > 9000

    result = await _message(bot).reply_text(text, reply_markup=keyboard)

    assert len(sent) == 3 and result == "msg3"
    assert [c.get("reply_markup") for c in sent] == [None, None, keyboard]
    assert all(tg_len(c["text"]) <= LIMIT for c in sent)


async def test_parse_error_in_second_part_repeats_only_it(sent):
    bot = utils.SplittingBot("123:abc")
    text = "\n\n".join(["<b>раз</b> " + "а " * 1900, "<b>два</b> " + "б " * 1900])
    sent.fail_on[2] = telegram.error.BadRequest("Can't parse entities: unclosed tag")

    await bot.send_message(42, text, parse_mode="HTML")

    texts = [c["text"] for c in sent]
    assert len(texts) == 3
    assert texts[0].startswith("<b>раз</b>")  # первая часть ушла один раз
    assert texts[2].startswith("два") and "parse_mode" not in sent[2]


async def test_too_long_edit_is_skipped_and_logged(sent, tmp_path):
    conn = db.connect(tmp_path / "db.sqlite")
    bot = utils.SplittingBot("123:abc", conn=conn)

    assert await bot.edit_message_text("а" * (LIMIT + 1), chat_id=42, message_id=1) is None
    await bot.edit_message_text("коротко", chat_id=42, message_id=1)

    assert sent == [{"edit": "коротко"}]
    assert log_rows(conn, "edit_skipped_too_long")[0]["length"] == LIMIT + 1


class _Replier:
    """`message.reply_text`, отвергающий первую попытку заданной ошибкой."""

    def __init__(self, error):
        self.error, self.texts = error, []

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)
        if self.error:
            error, self.error = self.error, None
            raise error
        return "ok"


async def test_send_html_does_not_retry_on_length():
    message = _Replier(telegram.error.BadRequest("Message is too long"))
    with pytest.raises(telegram.error.BadRequest):
        await utils.send_html(message, "<b>текст</b>")
    assert message.texts == ["<b>текст</b>"]


async def test_send_html_retries_without_tags_on_parse_error():
    message = _Replier(telegram.error.BadRequest("Can't parse entities: bad tag"))
    assert await utils.send_html(message, "<b>текст</b>") == "ok"
    assert message.texts == ["<b>текст</b>", "текст"]


def _reply_client(text):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            body = {"choices": [{"message": {"content": text}}]}
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    return _Client


@pytest.mark.parametrize("length, rejected", [(5000, True), (2933, False)])
async def test_long_chat_answer_is_too_long_defect(tmp_path, monkeypatch, length, rejected):
    conn = db.connect(tmp_path / "db.sqlite")
    text = ("Ответ про настройку агента. " * 200)[:length]
    monkeypatch.setattr(httpx, "AsyncClient", _reply_client(text))

    call = openrouter.ask_openai(
        "вопрос", api_key="sk", model="m", conn=conn, user_id=42,
        context_label="reference_question",
    )
    if rejected:
        with pytest.raises(failures.AnswerRejected, match="too_long"):
            await call
    else:
        assert await call == text
    assert bool(log_rows(conn, "llm_rejected")) is rejected


async def test_too_long_answer_rejected_and_not_cached(tmp_path, monkeypatch):
    from types import SimpleNamespace

    conn = db.connect(tmp_path / "db.sqlite")
    monkeypatch.setattr(httpx, "AsyncClient", _reply_client("а " * 2600))
    config = SimpleNamespace(
        repo_path=None, openrouter_api_key="sk-or",
        openrouter_models=("m",), llm_provider="openrouter", agent_cache_ttl_s=3600,
    )

    assert await ladder.call_agent(config, conn, "вопрос", 42, "reference_question", light=True) is None
    row = log_rows(conn, "llm_rejected")[0]
    assert row["defect"] == "too_long" and row["length"] == 5200
    assert db.cache_get(conn, cache.cache_key(42, "openrouter", True, "вопрос", "ru"), 3600) is None
