"""Распознанный скриншот: формат по байтам и вызов модели (спека
screenshot-input). Сети нет - подменяется `vision._post`.

Запуск: .venv/bin/python -m pytest tests/test_vision.py -q
"""

import httpx
import pytest

from tgbot import vision

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 16
WEBP = b"RIFF\x10\0\0\0WEBPVP8 " + b"\0" * 8
HEIC = b"\0\0\0\x18ftypheic" + b"\0" * 8
GIF = b"GIF89a" + b"\0" * 8
MODELS = ("google/gemini-3.1-flash-lite", "google/gemini-3.8-flash")


@pytest.mark.parametrize(
    "head, fmt",
    [(PNG, "png"), (JPEG, "jpeg"), (WEBP, "webp"), (HEIC, None), (GIF, None), (b"", None)],
)
def test_the_format_is_read_from_the_first_bytes(head, fmt):
    assert vision.detect_format(head) == fmt


def test_the_payload_carries_the_image_the_fallback_and_the_quantization_filter():
    payload = vision._payload(PNG, "png", MODELS)
    content = payload["messages"][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert payload["models"] == list(MODELS) and "route" not in payload
    assert payload["provider"]["quantizations"]
    assert payload["reasoning"] == {"effort": "minimal"}


def test_a_single_model_sends_no_fallback_fields():
    payload = vision._payload(PNG, "png", MODELS[:1])
    assert "models" not in payload and "route" not in payload


def _answer(text, model=MODELS[1]):
    async def fake_post(*args):
        return {"model": model, "choices": [{"message": {"content": text}}]}
    return fake_post


async def test_a_good_answer_is_the_recognized_text_with_the_model_that_answered(monkeypatch):
    monkeypatch.setattr(vision, "_post", _answer("1. ТЕКСТ: call-out-6a7eef9ec3b74c24"))
    result = await vision.recognize(PNG, "png", models=MODELS, api_key="k")
    assert result.text == "1. ТЕКСТ: call-out-6a7eef9ec3b74c24"
    assert result.model == MODELS[1]
    assert result.reason is None


@pytest.mark.parametrize(
    "error, reason",
    [(httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectError("down"), "error")],
)
async def test_a_failed_call_is_a_reason_not_an_exception(monkeypatch, error, reason):
    async def fake_post(*args):
        raise error
    monkeypatch.setattr(vision, "_post", fake_post)
    result = await vision.recognize(PNG, "png", models=MODELS, api_key="k")
    assert result.text is None and result.reason == reason


async def test_an_empty_answer_is_a_failure(monkeypatch):
    monkeypatch.setattr(vision, "_post", _answer("   "))
    result = await vision.recognize(PNG, "png", models=MODELS, api_key="k")
    assert result.text is None and result.reason == "empty"


async def test_a_degenerate_answer_is_a_failure(monkeypatch):
    monkeypatch.setattr(vision, "_post", _answer("ТЕКСТ: кнопка"))

    async def degenerate(text, **kwargs):
        return 3
    monkeypatch.setattr(vision.degeneration, "is_degenerate", degenerate)
    result = await vision.recognize(PNG, "png", models=MODELS, api_key="k")
    assert result.text is None and result.reason == "degenerate"


# --- full-traffic-logging -------------------------------------------------------


async def test_the_exchange_is_logged_without_the_image_bytes(monkeypatch, tmp_path):
    import hashlib
    import json

    from tgbot import db

    conn = db.connect(tmp_path / "db.sqlite")
    monkeypatch.setattr(vision, "_post", _answer("1. ТЕКСТ: экран кампании"))
    await vision.recognize(PNG, "png", models=MODELS, api_key="k", conn=conn, user_id=42)

    rows = {r["kind"]: json.loads(r["payload"]) for r in conn.execute(
        "SELECT kind, payload FROM log WHERE kind IN ('llm_request', 'llm_response')"
    )}
    request = rows["llm_request"]
    assert request["context"] == vision.CONTEXT_LABEL and request["prompt"] == vision.PROMPT
    assert request["image"] == {
        "bytes": len(PNG), "fmt": "png", "sha256": hashlib.sha256(PNG).hexdigest(),
    }
    assert "base64" not in json.dumps(request)
    assert rows["llm_response"]["text"] == "1. ТЕКСТ: экран кампании"
    assert rows["llm_response"]["model"] == MODELS[1]


async def test_a_failed_call_is_logged_with_its_reason(monkeypatch, tmp_path):
    import json

    from tgbot import db

    async def fake_post(*args):
        raise httpx.ReadTimeout("slow")

    conn = db.connect(tmp_path / "db.sqlite")
    monkeypatch.setattr(vision, "_post", fake_post)
    await vision.recognize(PNG, "png", models=MODELS, api_key="k", conn=conn, user_id=42)

    [row] = conn.execute("SELECT payload FROM log WHERE kind = 'llm_response'").fetchall()
    assert json.loads(row["payload"])["reason"] == "timeout"
