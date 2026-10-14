"""Реестр режимов кроме Справки (Справка - режим по умолчанию, её здесь нет).

Новый режим - модуль `handlers/<режим>.py` со своим `SPEC` по образцу
`echo.py` и строка в `MODES`. Отсюда берут команду и кнопки режима
(`tgbot.bot`), его место в рабочем меню (`handlers.common`), метку
классификатора и текст при открытой сессии (`handlers.reference`) и вход по
кнопке перехода (`handlers.modes`)."""

from . import echo
from .deps import ModeSpec

MODES: tuple[ModeSpec, ...] = (
    echo.SPEC,
)


def by_mode() -> dict[str, ModeSpec]:
    return {spec.mode: spec for spec in MODES}


def intents() -> dict[str, str]:
    """Метка режима -> критерий классификатора Справки (`tgbot.intent`)."""
    return {spec.mode: spec.intent for spec in MODES}


def commands() -> tuple[str, ...]:
    return tuple(spec.command for spec in MODES)
