"""Выродившийся ответ модели: распознавание до отправки (спека answer-degeneration-check).

Подозрительные места считают регулярки: символы чужих письменностей и слова,
где кириллица склеена с латиницей. Явный брак (`REJECT_AT` мест и больше)
решается без судьи; 1-4 места - пограничный случай, его решает классификатор
Jev (typesafe.ai) по фрагментам вокруг мест. Судья fail-open: без ключа, при
ошибке или таймауте ответ считается нормальным.
"""

import re

from . import db, jev
from .jev import JEV_URL  # noqa: F401 - адрес судьи, его проверяют тесты

# Иврит и арабица, тайская, кана, CJK, хангыль: у бота языки только ru и en.
FOREIGN_SCRIPT = re.compile(
    "[֐-ࣿ฀-๿぀-ヿ㐀-鿿가-힯]"
)
# Слово, где кириллическая буква стоит вплотную к латинской (`дляcontracts`).
MIXED_WORD = re.compile(r"\w*(?:[а-яё][a-z]|[a-z][а-яё])\w*", re.IGNORECASE)
SENTENCE_END = re.compile(r"[.!?]\s+(?=[A-ZА-ЯЁ])")

# У инцидентов 21 и 22 270 мест, у нормальных ответов из логов не больше одного.
REJECT_AT = 5
SIDE_LIMIT = 300
TOTAL_LIMIT = 2000
GARBAGE_AT = 0.5

JEV_TIMEOUT_S = 30
JEV_QUESTION = {
    "type": "choice",
    "instructions": "Is this fragment of a chatbot answer coherent text or degenerate model output?",
    "criteria": {
        "normal": (
            "Coherent text in the language of the answer; Latin technical terms, "
            "identifiers and code are acceptable"
        ),
        "garbage": (
            "Broken word fragments, alphabets mixed inside words, "
            "incoherent sequence of tokens"
        ),
    },
}


def suspicious_spans(text: str) -> list[tuple[int, int]]:
    """Подозрительные места по порядку: каждый чужой символ и каждое склеенное слово."""
    spans = [m.span() for m in FOREIGN_SCRIPT.finditer(text)]
    spans += [m.span() for m in MIXED_WORD.finditer(text)]
    return sorted(spans)


def fragments(text: str, spans: list[tuple[int, int]]) -> str:
    """Предложение с местом и по одному соседнему с каждой стороны, не дальше
    `SIDE_LIMIT` от места (обрезка по пробелу); пересечения склеены, всего
    не больше `TOTAL_LIMIT` символов."""
    starts = [0, *(m.end() for m in SENTENCE_END.finditer(text))]
    ends = [*(s for s in starts[1:]), len(text)]
    windows: list[list[int]] = []
    for begin, end in spans:
        k = max(i for i, s in enumerate(starts) if s <= begin)
        left = starts[max(k - 1, 0)]
        right = ends[min(k + 1, len(ends) - 1)]
        if begin - left > SIDE_LIMIT:
            left = text.find(" ", begin - SIDE_LIMIT, begin) + 1 or begin - SIDE_LIMIT
        if right - end > SIDE_LIMIT:
            cut = text.rfind(" ", end, end + SIDE_LIMIT)
            right = cut if cut > 0 else end + SIDE_LIMIT
        if windows and left <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], right)
        else:
            windows.append([left, right])
    joined = " ... ".join(text[a:b].strip() for a, b in windows)
    return joined[:TOTAL_LIMIT]


async def judge(conn, user_id, context_label: str, api_key: str | None, sample: str) -> bool:
    """True - классификатор признал мусор. Любой сбой - False (fail-open) и
    `llm_judge` с причиной."""
    probs, reason = await jev.choice(api_key, sample, JEV_QUESTION, JEV_TIMEOUT_S)
    garbage = (probs or {}).get("garbage")
    if garbage is None:
        _log_judge(conn, user_id, context_label, sample, reason=reason or "error: no garbage label")
        return False
    verdict = garbage >= GARBAGE_AT
    _log_judge(
        conn, user_id, context_label, sample,
        verdict="garbage" if verdict else "normal", garbage=garbage,
    )
    return verdict


def _log_judge(conn, user_id, context_label: str, sample: str, **fields) -> None:
    if conn is not None:
        db.log_event(conn, user_id, "llm_judge", {
            "context": context_label, "input": sample, "question": JEV_QUESTION, **fields,
        })


async def is_degenerate(
    text: str, *, conn=None, user_id=None, context_label: str = "", judge_key: str | None = None
) -> int:
    """Число подозрительных мест, если ответ - брак; 0 - ответ нормальный."""
    spans = suspicious_spans(text or "")
    if not spans:
        return 0
    if len(spans) >= REJECT_AT:
        return len(spans)
    garbage = await judge(conn, user_id, context_label, judge_key, fragments(text, spans))
    return len(spans) if garbage else 0
