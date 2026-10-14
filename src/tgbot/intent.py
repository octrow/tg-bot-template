"""Быстрый классификатор намерения вопроса Справке (спека reference-mode):
вопрос, болтовня или просьба о режиме из реестра режимов (`handlers.registry`,
критерии приходят параметром `intents`).

Только уточняет уверенные случаи: болтовня идёт облегчённым путём, просьба о
режиме даёт кнопку перехода под ответом. Fail-open: без ключа, при ошибке,
таймауте или неуверенности трека нет, и вопрос идёт обычным путём.
"""

import asyncio
import time
from collections.abc import Mapping

from . import db, jev

# Выше, чем у `degeneration` (0,5): ложный трек человек видит. Пересматривать по журналу `reference_intent`.
INTENT_AT = 0.7
# Дольше это уже не "сразу", а ответ не должен ждать кнопку.
INTENT_TIMEOUT_S = 5
# В вопросах бывают телефоны и имена клиентов: классификатору хватает начала.
INTENT_MAX_CHARS = 500
# Болтовня и вопросы о самом боте: Справка отвечает без поиска по репозиторию
# (reference-language-and-grounding).
CHAT = "chat"
QUESTION = "question"
INSTRUCTIONS = "What does the user need with this message to the help bot?"
CRITERIA = {
    QUESTION: "A question about how the product works or how to use it",
    CHAT: (
        "A greeting, thanks or small talk, or a question about this help bot "
        "itself (its commands, language, access)"
    ),
}

# Чей вызов записан в `reference_intent`: пороги смотрятся отдельно.
REFERENCE_CONTEXT = "reference"


def reference_question(intents: Mapping[str, str]) -> dict:
    """Вопрос Jev: вопрос / болтовня / режим X для каждого режима реестра
    (`intents`: метка режима -> критерий)."""
    criteria = {**CRITERIA, **intents}
    return {"type": "choice", "instructions": INSTRUCTIONS, "criteria": criteria}


async def _choose(conn, user_id, api_key, text, question, context) -> str | None:
    """Метка из критериев `question` при уверенности не ниже `INTENT_AT`, иначе
    None. Исход - записью `reference_intent`."""
    started = time.monotonic()
    state = text[:INTENT_MAX_CHARS]
    try:
        probs, reason = await asyncio.wait_for(
            jev.choice(api_key, state, question, INTENT_TIMEOUT_S), INTENT_TIMEOUT_S,
        )
    except TimeoutError:
        probs, reason = None, "timeout"
    track = None
    if probs is None:
        outcome = f"unavailable:{reason}"
    else:
        best = max(question["criteria"], key=lambda t: probs.get(t, 0.0))
        track = best if probs.get(best, 0.0) >= INTENT_AT else None
        outcome = f"offered:{track}" if track else "below"
    db.log_event(conn, user_id, "reference_intent", {
        "probabilities": probs, "outcome": outcome, "context": context,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "input": state, "question": question,
    })
    return track


async def classify(
    conn, user_id, api_key: str | None, text: str, intents: Mapping[str, str]
) -> str | None:
    """`chat` или метка режима из `intents` при уверенности, иначе None:
    уверенный "вопрос" - тот же обычный путь."""
    question = reference_question(intents)
    track = await _choose(conn, user_id, api_key, text, question, REFERENCE_CONTEXT)
    return None if track == QUESTION else track
