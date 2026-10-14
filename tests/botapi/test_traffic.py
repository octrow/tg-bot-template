"""full-traffic-logging: каждый исходящий вызов Bot API в файловом журнале."""

import asyncio
import json
import time

import pytest

from ..fakes.llm_api import DEFAULT_TEXT
from ..fakes.telegram_api import CANT_PARSE
from .conftest import ADMIN, ALLOWED, TOKEN, button

U = ALLOWED


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(tmp_path / "logs"))
    return tmp_path / "logs"


def _calls(logs, method: str) -> list[dict]:
    out = []
    for path in logs.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["kind"] == "tg_call" and record["payload"]["method"] == method:
                out.append(record)
    return out


async def _eventually(read, timeout=2.0):
    """Журнал дописывается после ответа Telegram - чуть позже, чем его видит фейк."""
    deadline = time.monotonic() + timeout
    while not (found := read()) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return found


def _all_text(logs) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in logs.glob("*.jsonl"))


async def test_sent_message_is_logged_with_text_and_id_without_token(bot, logs):
    reply = await bot.ask(U, "/help")

    [call] = await _eventually(
        lambda: [c for c in _calls(logs, "sendMessage") if c["user_id"] == U]
    )
    assert call["payload"]["params"]["text"] == reply.params["text"]
    assert call["payload"]["ok"] is True
    assert call["payload"]["message_id"] == reply.result["message_id"]
    assert TOKEN not in _all_text(logs)


async def test_sent_md_document_is_logged_with_full_text(bot, logs):
    page = await bot.ask(ADMIN, "/logs")
    mark = bot.click(ADMIN, page.result, button(page, "logs:f:"))
    await bot.wait("sendDocument", chat=ADMIN, after=mark)

    [call] = await _eventually(lambda: _calls(logs, "sendDocument"))
    [entry] = call["payload"]["files"].values()
    assert entry["filename"].endswith(".md")
    assert entry["size"] > 0
    assert entry["text"].startswith("# ")


async def test_rejected_call_is_logged_with_its_error(bot, logs):
    bot.tg.fail("sendMessage", CANT_PARSE,
                when=lambda p: p.get("parse_mode") == "HTML" and DEFAULT_TEXT in p.get("text", ""))
    mark = bot.send(U, "Что такое кампания?")
    await bot.wait("sendMessage", chat=U, after=mark, contains=DEFAULT_TEXT, count=2)

    failed = await _eventually(
        lambda: [c for c in _calls(logs, "sendMessage") if c["payload"]["ok"] is False]
    )
    assert failed and CANT_PARSE in failed[0]["payload"]["error"]
    assert failed[0]["payload"]["params"]["parse_mode"] == "HTML"


async def test_call_without_chat_id_is_attributed_to_the_updates_user(bot, logs):
    page = await bot.ask(ADMIN, "/logs")
    mark = bot.click(ADMIN, page.result, button(page, "logs:f:"))
    await bot.wait("sendDocument", chat=ADMIN, after=mark)

    answers = await _eventually(lambda: _calls(logs, "answerCallbackQuery"))
    assert answers and all(c["user_id"] == ADMIN for c in answers)
