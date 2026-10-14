"""Режим и этап текущего апдейта - для шапки 'режим · этап' (bot-modes).

Этап ставят точки, которые его знают (`enter`), а шапку подставляет
`utils.SplittingBot` при отправке и правке (`with_header`). Каждый апдейт идёт в
своей asyncio-задаче (`concurrent_updates`), фоновые задачи копируют контекст -
поэтому этап не протекает между пользователями и служебные команды идут без шапки.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum

from . import i18n


class Mode(StrEnum):
    """Справка - режим по умолчанию. Остальные режимы называют себя строкой
    (`MODE`) в своём модуле и записаны в реестре `handlers.registry`."""

    REFERENCE = "reference"


@dataclass(frozen=True)
class Stage:
    mode: str  # `Mode.REFERENCE` или `MODE` режима из реестра
    step: str | None = None  # ключ в `MODE_STAGES`; None - этап не называется
    params: dict = field(default_factory=dict)


current: ContextVar[Stage | None] = ContextVar("mode_stage", default=None)
# Язык интерфейса апдейта, если обработчик его определил по профилю Telegram:
# сохранённого выбора у нового пользователя ещё нет, а шапка идёт на языке текста.
ui_lang: ContextVar[str | None] = ContextVar("mode_ui_lang", default=None)


def enter(mode: str, step: str | None = None, **params) -> None:
    current.set(Stage(mode, step, params))


def get() -> Stage | None:
    return current.get()


def header(lang: str | None, stage: Stage) -> str:
    T = i18n.texts(lang)
    name = T.MODE_HEADERS[stage.mode]
    if stage.step is None:
        return name
    return f"{name} · {T.MODE_STAGES[stage.step].format(**stage.params)}"


def _prefixes() -> tuple[str, ...]:
    """Начала шапок на всех языках: текст с ними шапку уже несёт (правка)."""
    out = []
    for lang in ("ru", "en"):
        for name in i18n.texts(lang).MODE_HEADERS.values():
            out.append(name.split("{")[0].strip())
    return tuple(out)


def with_header(text: str, lang: str | None) -> str:
    stage = current.get()
    if stage is None or not text or text.startswith(_prefixes()):
        return text
    # ponytail: шапка не экранируется - параметры этапа ставит код бота, а не
    # пользователь; режим с параметром из ввода обязан проверить его формат.
    return f"{header(lang, stage)}\n\n{text}"
