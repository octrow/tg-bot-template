"""Быстрый классификатор намерения Справки (спека reference-mode).

Jev подменён: тест проверяет порог, обрезку и fail-open, а не typesafe.ai.

Запуск: .venv/bin/python -m pytest tests/test_intent.py -q
"""

import asyncio

import pytest

from tgbot import db, intent
from tgbot.handlers import echo, registry
from tgbot.modes import Mode

from .conftest import log_rows

# Критерии режимов из реестра - их передаёт Справка.
INTENTS = registry.intents()


def _jev(monkeypatch, result, seen=None):
    """`result` - (вероятности, причина) для каждого вопроса запроса."""
    async def choices(api_key, text, questions, timeout):
        if seen is not None:
            seen.append(text)
        if isinstance(result, BaseException):
            raise result
        probs, reason = result
        return (None if probs is None else {name: probs for name in questions}), reason

    monkeypatch.setattr(intent.jev, "choices", choices)


def _probs(question=0.0, chat=0.0):
    return {"question": question, "chat": chat}


@pytest.mark.parametrize("probs, track, outcome", [
    (_probs(chat=0.8, question=0.2), "chat", "offered:chat"),
    (_probs(chat=0.69, question=0.31), None, "below"),
    # Уверенный "вопрос" - тот же обычный путь: наружу трека нет.
    (_probs(question=0.9, chat=0.1), None, "offered:question"),
])
async def test_the_track_comes_only_at_the_threshold(
    tmp_path, monkeypatch, probs, track, outcome
):
    conn = db.connect(tmp_path / "db.sqlite")
    _jev(monkeypatch, (probs, None))
    assert await intent.classify(conn, 42, "key", "у нас кампания перестала звонить", INTENTS) == track
    row = log_rows(conn, "reference_intent")[0]
    assert row["outcome"] == outcome
    assert row["probabilities"] == probs
    assert "elapsed_ms" in row


@pytest.mark.parametrize("reason", ["no_key", "timeout", "error: 500"])
async def test_an_unavailable_classifier_gives_no_track(tmp_path, monkeypatch, reason):
    conn = db.connect(tmp_path / "db.sqlite")
    _jev(monkeypatch, (None, reason))
    assert await intent.classify(conn, 42, "key", "вопрос", INTENTS) is None
    assert log_rows(conn, "reference_intent")[0]["outcome"] == f"unavailable:{reason}"


async def test_a_slow_classifier_is_cut_at_the_deadline(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    monkeypatch.setattr(intent, "INTENT_TIMEOUT_S", 0.01)

    async def slow(*args):
        await asyncio.sleep(10)

    monkeypatch.setattr(intent.jev, "choices", slow)
    assert await intent.classify(conn, 42, "key", "вопрос", INTENTS) is None
    assert log_rows(conn, "reference_intent")[0]["outcome"] == "unavailable:timeout"


async def test_only_the_head_of_the_question_is_sent(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    seen = []
    _jev(monkeypatch, (_probs(question=1.0), None), seen)
    await intent.classify(conn, 42, "key", "я" * (intent.INTENT_MAX_CHARS + 100), INTENTS)
    assert seen == ["я" * intent.INTENT_MAX_CHARS]


async def test_the_reference_classifier_logs_its_context(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    _jev(monkeypatch, (_probs(question=1.0), None))
    await intent.classify(conn, 42, "key", "вопрос", INTENTS)
    assert log_rows(conn, "reference_intent")[0]["context"] == intent.REFERENCE_CONTEXT


async def test_the_classifier_input_and_question_are_logged(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    _jev(monkeypatch, (_probs(question=0.9, chat=0.1), None))
    await intent.classify(conn, 42, "key", "у нас кампания перестала звонить", INTENTS)
    row = log_rows(conn, "reference_intent")[0]
    assert row["input"] == "у нас кампания перестала звонить"
    assert row["question"] and isinstance(row["question"], dict)


# --- reference-language-and-grounding: болтовня и вопросы о боте -------------

@pytest.mark.parametrize("probs, track", [
    ({"question": 0.2, "chat": 0.8}, intent.CHAT),
    ({"question": 0.35, "chat": 0.65}, None),
])
async def test_chat_comes_at_the_threshold(tmp_path, monkeypatch, probs, track):
    conn = db.connect(tmp_path / "db.sqlite")
    _jev(monkeypatch, (probs, None))
    assert await intent.classify(conn, 42, "key", "привет", INTENTS) == track


# --- переход в другой режим по просьбе словами ---------------------


async def test_jev_asks_several_questions_in_one_request(monkeypatch):
    import httpx

    from tgbot import jev

    sent = []

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            sent.append(json)
            body = {"answers": {
                "verdict": {"probabilities": {"question": 0.9, "new": 0.1}},
                "mode": {"probabilities": {"stay": 0.2, "task": 0.8}},
            }}
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    answers, reason = await jev.choices("key", "текст", {"verdict": {}, "mode": {}}, 1)
    assert reason is None and len(sent) == 1
    assert set(sent[0]["questions"]) == {"verdict", "mode"}
    assert answers == {"verdict": {"question": 0.9, "new": 0.1}, "mode": {"stay": 0.2, "task": 0.8}}

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert (await jev.choices("key", "т", {"mode": {}}, 1))[0] is None
    assert await jev.choices(None, "т", {"mode": {}}, 1) == (None, "no_key")


# --- метки режимов из реестра режимов ----------------------------------------


def test_reference_labels_come_from_the_mode_registry():
    criteria = intent.reference_question(INTENTS)["criteria"]
    assert set(criteria) == {intent.QUESTION, intent.CHAT, *registry.intents()}
    assert criteria[echo.MODE] == echo.SPEC.intent
    assert Mode.REFERENCE.value not in criteria  # Справка - режим по умолчанию


@pytest.mark.parametrize("probs, track", [
    ({"question": 0.1, "chat": 0.1, "echo": 0.8}, "echo"),
    ({"question": 0.3, "chat": 0.05, "echo": 0.65}, None),
])
async def test_a_mode_comes_only_at_the_threshold(tmp_path, monkeypatch, probs, track):
    conn = db.connect(tmp_path / "db.sqlite")
    _jev(monkeypatch, (probs, None))
    assert await intent.classify(conn, 42, "key", "повтори за мной: привет", INTENTS) == track
