"""Вызов классификатора Jev (typesafe.ai), тип `choice`: общий для судьи
выродившихся ответов (`degeneration`) и классификатора намерения (`intent`).
Fail-open: любой сбой - вероятностей нет, причина для журнала."""

import logging

import httpx

log = logging.getLogger(__name__)

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
# Имя вопроса в запросе и ответе у вызова с одним вопросом (`choice`).
ANSWER_KEY = "verdict"


async def choices(
    api_key: str | None, text: str, questions: dict[str, dict], timeout: float
) -> tuple[dict[str, dict[str, float]] | None, str | None]:
    """Несколько вопросов о тексте одним запросом: ({имя: вероятности меток},
    None) или (None, причина: `no_key`, `timeout`, `error: ...`)."""
    if not api_key:
        return None, "no_key"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                JEV_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"state": text, "model": JEV_MODEL, "questions": questions},
            )
            resp.raise_for_status()
            answers = resp.json()["answers"]
            return {
                name: {label: float(p) for label, p in answers[name]["probabilities"].items()}
                for name in questions
            }, None
    except httpx.TimeoutException:
        return None, "timeout"
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        log.warning("Jev не ответил: %s", exc)
        return None, f"error: {exc}"[:200]


async def choice(
    api_key: str | None, text: str, question: dict, timeout: float
) -> tuple[dict[str, float] | None, str | None]:
    """Один вопрос: (вероятности меток, None) или (None, причина)."""
    answers, reason = await choices(api_key, text, {ANSWER_KEY: question}, timeout)
    return (answers[ANSWER_KEY] if answers else None), reason
