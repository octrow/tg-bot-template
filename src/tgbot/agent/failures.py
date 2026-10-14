"""Отказы ступени и брак ответа: род отказа (`StageFailure`), текст
пользователю простым языком, кнопка "Сообщить разработчикам" и сообщение,
когда не ответила ни одна ступень."""

from dataclasses import dataclass

import httpx

from .. import alerts, db, i18n

# Род отказа ступени. Пустой `str(exc)` приходит из четырёх разных ситуаций
# (нет ключа, таймаут, обрыв, исключение без текста), и раньше все четыре
# схлопывались в один текст про подписку Claude - провайдера, которого в вызове
# не было (спека llm-provider-selection).
FAILURE_NO_KEY = "no_key"
FAILURE_TIMEOUT = "timeout"
FAILURE_PROVIDER = "provider_error"
FAILURE_UNKNOWN = "unknown"
FAILURE_REJECTED = "rejected"
# `StageFailure.exception` отказа по жёсткому порогу лестницы моделей.
LADDER_DEADLINE = "LadderDeadline"

# Роды брака: обрезан потолком выходных токенов; выродился в бессвязный текст.
DEFECT_TRUNCATED = "truncated"
DEFECT_DEGENERATE = "degenerate"
DEFECT_TOO_LONG = "too_long"


class AnswerRejected(Exception):
    """Брак: ответ модели отвергнут до отправки пользователю.

    `defect` - род брака. Брак не отправляется и не кэшируется: исключение
    обрывает вызов до `cache_store` и превращается в отказ ступени
    (`failure_of`). Новые роды брака добавляются сюда же.
    """

    def __init__(self, defect: str):
        super().__init__(defect)
        self.defect = defect


@dataclass(frozen=True)
class StageFailure:
    """Почему ступень не ответила: этого не восстановить по одной строке.

    `stage` - ступень (ключ `STAGES`), `kind` - род отказа, `detail` - текст
    провайдера либо имя переменной окружения (для `no_key`), `exception` - класс
    исключения. Род определяет и текст пользователю, и совет: править ключ,
    подождать или смотреть журнал.
    """

    stage: str
    kind: str
    detail: str = ""
    exception: str = ""

    def as_log(self) -> dict:
        """Поля для журнала. `error` оставлен прежним - его читают существующие
        запросы; ступень, род и класс добавлены рядом, чтобы отказ с пустым
        текстом разбирался по одной записи."""
        return {
            "stage": self.stage,
            "kind": self.kind,
            "exception": self.exception,
            "error": self.detail,
        }


def failure_of(stage: str, exc: BaseException) -> StageFailure:
    """Род отказа по исключению ступени.

    У таймаута HTTP-клиента текст пустой (`httpx.ReadTimeout` строится из
    `str(TimeoutError())`), поэтому род определяется по типу, а не по
    тексту. Исключение без текста - `unknown` с классом: пользователь пересылает
    сообщение, и по нему инцидент разбирается без доступа к серверу.
    """
    if isinstance(exc, httpx.TimeoutException):
        return StageFailure(stage, FAILURE_TIMEOUT, exception=type(exc).__name__)
    if isinstance(exc, AnswerRejected):
        return StageFailure(stage, FAILURE_REJECTED, detail=exc.defect, exception=type(exc).__name__)
    text = str(exc)
    kind = FAILURE_PROVIDER if text else FAILURE_UNKNOWN
    return StageFailure(stage, kind, detail=text, exception=type(exc).__name__)


def failure_text(lang: str, failure: StageFailure, code: int | None = None) -> str:
    """Текст отказа простым языком: что случилось, поможет ли повтор. Ступень,
    модель, класс исключения и текст провайдера - только в журнале, под кодом
    инцидента (спека failure-messages)."""
    key = {
        FAILURE_NO_KEY: "FAILURE_NOT_CONFIGURED",
        FAILURE_REJECTED: "FAILURE_BAD_ANSWER",
    }.get(failure.kind, "FAILURE_UNAVAILABLE")
    text = i18n.t(lang, key)
    if is_deadline(failure):
        text = i18n.t(lang, "FAILURE_TIMEOUT", wait=_wait_text(lang, failure.detail))
    return f"{text}\n\n{i18n.t(lang, 'FAILURE_CODE', code=code)}" if code is not None else text


def _wait_text(lang: str, seconds: str) -> str:
    """Порог словами: ровные минуты - минутами, иначе секундами."""
    try:
        value = float(seconds)
    except ValueError:  # запись без порога
        value = 600.0
    if value >= 60 and value % 60 == 0:
        return i18n.t(lang, "FAILURE_WAIT_MIN", n=int(value // 60))
    return i18n.t(lang, "FAILURE_WAIT_SEC", n=f"{value:g}")


def is_deadline(failure: StageFailure) -> bool:
    """Отказ по жёсткому порогу лестницы, а не таймаут одного чтения HTTP."""
    return failure.kind == FAILURE_TIMEOUT and failure.exception == LADDER_DEADLINE


def failure_markup(lang: str, code: int):
    """Кнопка "Сообщить разработчикам": Отзыв с этим кодом инцидента."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(i18n.t(lang, "FAILURE_REPORT_BUTTON"), callback_data=f"fail:{code}")]]
    )


async def notify_agent_down(
    conn, message, failure: StageFailure | None = None,
    lang: str = i18n.DEFAULT_LANG, *, user_id: int | None = None, context_label: str = "",
    question: str = "", models=(),
) -> None:
    """Ни одна ступень не ответила: текст сбоя простым языком, код инцидента,
    кнопка "Сообщить разработчикам".

    Код инцидента - `id` записи `llm_failure`: она пишется ДО отправки, и в
    ней все подробности (ступень, род, класс исключения, текст провайдера,
    модели). Пользователь видит только род простыми словами
    (спека failure-messages). `lang` - язык получателя (`message`).
    """
    if message is None:
        return
    failure = failure or StageFailure("", FAILURE_UNKNOWN)
    code = db.log_event(conn, user_id, "llm_failure", {
        **failure.as_log(), "context": context_label, "question": question[:alerts.QUESTION_CHARS],
        "models": list(models),
    })
    if is_deadline(failure):
        alerts.notify(alerts.KIND_TIMEOUT, code, mode=context_label,
                      model=", ".join(models), question=question)
    elif failure.kind == FAILURE_REJECTED:
        alerts.notify(alerts.KIND_REJECTED, code, mode=context_label,
                      model=", ".join(models), question=question)
    await message.reply_text(failure_text(lang, failure, code), reply_markup=failure_markup(lang, code))
