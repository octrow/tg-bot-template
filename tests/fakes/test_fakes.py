"""Самопроверка фейков: от них зависят слои Bot API и True E2E, и фейк,
который врёт, даёт зелёные тесты на сломанном боте."""

import json
import time
from pathlib import Path

import httpx
import pytest

from tgbot.agent import openrouter, stages
from tgbot.agent.result import is_unverified

from .llm_api import FakeLLM
from .telegram_api import CANT_PARSE, FakeTelegram


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture
def tg():
    fake = FakeTelegram().start()
    yield fake
    fake.stop()


def _post(tg, method, **data):
    return httpx.post(f"{tg.base_url}123:abc/{method}", data=data, timeout=5)


def test_get_updates_respects_offset(tg):
    update_id = tg.push_update({"message": {"text": "hi"}})

    first = _post(tg, "getUpdates", offset="0", timeout="0").json()["result"]
    again = _post(tg, "getUpdates", offset=str(update_id + 1), timeout="0").json()["result"]

    assert [u["update_id"] for u in first] == [update_id]
    assert again == []


def test_get_updates_timeout_zero_does_not_hang(tg):
    started = time.monotonic()
    assert _post(tg, "getUpdates", timeout="0").json()["result"] == []
    assert time.monotonic() - started < 1


def test_get_updates_long_poll_wakes_on_push(tg):
    import threading

    threading.Timer(0.2, tg.push_update, args=({"message": {"text": "x"}},)).start()
    started = time.monotonic()
    result = _post(tg, "getUpdates", timeout="5").json()["result"]
    assert len(result) == 1 and time.monotonic() - started < 2


def test_send_message_records_decoded_params(tg):
    markup = {"inline_keyboard": [[{"text": "A", "callback_data": "a"}]]}
    resp = _post(
        tg, "sendMessage", chat_id="42", text="1", parse_mode="HTML",
        reply_markup=json.dumps(markup),
    ).json()

    params = tg.wait_for("sendMessage")[0].params
    assert params == {"chat_id": 42, "text": "1", "parse_mode": "HTML", "reply_markup": markup}
    assert resp["result"]["chat"]["id"] == 42 and resp["result"]["reply_markup"] == markup


def test_html_text_comes_back_without_tags(tg):
    resp = _post(tg, "sendMessage", chat_id="1", text="<b>a &amp; b</b>", parse_mode="HTML")
    assert resp.json()["result"]["text"] == "a & b"


def test_ordered_failure_has_real_status(tg):
    tg.fail("sendMessage", CANT_PARSE, when=lambda p: p.get("parse_mode") == "HTML")

    failed = _post(tg, "sendMessage", chat_id="1", text="<x>", parse_mode="HTML")
    plain = _post(tg, "sendMessage", chat_id="1", text="x")

    assert failed.status_code == 400 and failed.json()["description"] == CANT_PARSE
    assert plain.status_code == 200


def test_retry_after_carries_parameters(tg):
    tg.fail("sendMessage", "Too Many Requests: retry after 1", status=429, retry_after=1)
    body = _post(tg, "sendMessage", chat_id="1", text="x").json()
    assert body["parameters"] == {"retry_after": 1}


def test_multipart_document(tg):
    httpx.post(
        f"{tg.base_url}123:abc/sendDocument", data={"chat_id": "5"},
        files={"document": ("tz.md", b"# TZ")}, timeout=5,
    )
    params = tg.wait_for("sendDocument")[0].params
    assert params["chat_id"] == 5 and params["document"]["filename"] == "tz.md"


@pytest.fixture
def llm():
    fake = FakeLLM().start()
    yield fake
    fake.stop()


async def test_llm_full_call_reads_a_file_first(llm):
    answer = await openrouter.ask_openai(
        "вопрос", api_key="k", model="m", roots=[Path("docs").resolve()],
        responses_url=f"{llm.base_url}/responses", provider=stages.PROVIDER_OPENROUTER,
    )
    assert str(answer) == "Ответ фейка LLM."
    assert answer.files_read and not is_unverified(answer)
    assert len(llm.requests) == 2


async def test_llm_override_wins_once(llm):
    llm.reply("свой ответ")
    url = f"{llm.base_url}/chat/completions"
    first = await openrouter.ask_openai("x", api_key="k", model="m", chat_url=url)
    second = await openrouter.ask_openai("x", api_key="k", model="m", chat_url=url)
    assert (str(first), str(second)) == ("свой ответ", "Ответ фейка LLM.")
