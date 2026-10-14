"""Распознанный скриншот: картинка один раз переводится в текст (ADR-0005).

Дальше вопрос идёт текстом по обычному пути бота, картинка нигде не хранится
и в другие вызовы модели не передаётся. Модели выбраны замером 07.10 в исходном боте
на 14 настоящих скриншотах.
"""

import asyncio
import base64
import hashlib
import time
from dataclasses import dataclass

import httpx

from . import db, degeneration
from .config import OPENROUTER_QUANTIZATIONS

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_S = 60
# Потолок ответа: экран с мелким текстом в замере занимал до ~2k токенов.
MAX_TOKENS = 4000
CONTEXT_LABEL = "vision"

# Сигнатура первых байт -> формат. Тип, который назвал отправитель, не
# проверяется: Telegram передаёт его как есть.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
)
MIME = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}

# Промпт из замера: с ним модели находили обводку и узнавали картинку, не
# являющуюся экраном. Менять - только с повторным замером.
PROMPT = (
    "Это скриншот, который пользователь прислал в чат поддержки. "
    "Опиши его для другой модели, которая картинку не увидит.\n"
    "1. ТЕКСТ: весь видимый текст дословно, сохраняя строки. Идентификаторы, хэши, "
    "номера, uuid переписывай символ в символ, ничего не сокращай.\n"
    "2. ЭКРАН: одной-двумя фразами, что это за экран или окно.\n"
    "3. ВЫДЕЛЕНО: что пользователь выделил стрелками, рамками, обводкой, подчёркиванием; "
    "если ничего - напиши 'нет'.\n"
    "Не отвечай на вопросы и не делай выводов о причинах. Только описание."
)


@dataclass(frozen=True)
class Recognized:
    """Итог распознавания одной картинки. `text` пуст при сбое, причина в
    `reason`: error, timeout, empty, degenerate."""

    text: str | None
    model: str | None
    seconds: float
    reason: str | None = None


def detect_format(head: bytes) -> str | None:
    """PNG, JPEG или WEBP по первым байтам; всё остальное - None."""
    for signature, fmt in _SIGNATURES:
        if head.startswith(signature):
            return fmt
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def _payload(image: bytes, fmt: str, models: tuple[str, ...], provider: str = "openrouter") -> dict:
    url = f"data:{MIME[fmt]};base64,{base64.b64encode(image).decode()}"
    payload = {
        "model": models[0],
        "max_completion_tokens": MAX_TOKENS,
        "reasoning": {"effort": "minimal"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": url}},
        ]}],
    }
    if provider != "openrouter":
        # ponytail: у других шлюзов (OmniRoute) нет `models` и фильтра хостеров -
        # идёт одна основная модель; запасную делать combo на стороне шлюза.
        return payload
    payload["provider"] = {"quantizations": list(OPENROUTER_QUANTIZATIONS)}
    if len(models) > 1:
        # Дублёры - одним `models`: `route: "fallback"` OpenRouter объявил устаревшим.
        payload["models"] = list(models)
    return payload


async def _post(image: bytes, fmt: str, models: tuple[str, ...], api_key: str, url: str,
                provider: str) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        response = await client.post(
            url, json=_payload(image, fmt, models, provider),
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        return response.json()


async def recognize(
    image: bytes, fmt: str, *, models: tuple[str, ...], api_key: str,
    conn=None, user_id: int | None = None, judge_key: str | None = None, url: str = CHAT_URL,
    provider: str = "openrouter",
) -> Recognized:
    """Один вызов модели; запасная модель - через fallback OpenRouter.
    `url` - адрес chat completions (фейк LLM в тестах подменяет хост)."""
    started = time.monotonic()
    if conn is not None:
        # Картинка - размером и хешем: её байты в журнал не идут (full-traffic-logging).
        db.log_event(conn, user_id, "llm_request", {
            "provider": provider, "models": list(models), "context": CONTEXT_LABEL,
            "prompt": PROMPT,
            "image": {"bytes": len(image), "fmt": fmt, "sha256": hashlib.sha256(image).hexdigest()},
        })

    def done(text=None, model=None, reason=None, raw=None) -> Recognized:
        if conn is not None:
            db.log_event(conn, user_id, "llm_response", {
                "provider": provider, "context": CONTEXT_LABEL, "model": model,
                "text": raw, "reason": reason, "cached": False,
            })
        return Recognized(text, model, round(time.monotonic() - started, 1), reason)

    try:
        data = await asyncio.wait_for(_post(image, fmt, models, api_key, url, provider), TIMEOUT_S)
    except (TimeoutError, httpx.TimeoutException):
        return done(reason="timeout")
    except (httpx.HTTPError, ValueError):
        return done(reason="error")
    try:
        text = (data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return done(reason="error")
    model = data.get("model") or models[0]
    if not text:
        return done(model=model, reason="empty", raw=text)
    bad = await degeneration.is_degenerate(
        text, conn=conn, user_id=user_id, context_label=CONTEXT_LABEL, judge_key=judge_key,
    )
    if bad:
        return done(model=model, reason="degenerate", raw=text)
    return done(text=text, model=model, raw=text)
