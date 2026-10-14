"""Шапка 'режим · этап' (спека bot-modes).

Запуск: uv run pytest tests/test_modes.py -q
"""

import contextvars

import pytest

from tgbot import modes
from tgbot.handlers import echo
from tgbot.modes import Mode

CASES = [
    ((Mode.REFERENCE, None, {}), "💬 Справка", "💬 Help"),
    ((echo.MODE, None, {}), "🔁 Эхо", "🔁 Echo"),
    ((echo.MODE, "echo_step", {"n": 3}), "🔁 Эхо · шаг 3", "🔁 Echo · step 3"),
]


def _in_fresh_context(fn):
    return contextvars.copy_context().run(fn)


@pytest.mark.parametrize(("stage", "ru", "en"), CASES)
def test_every_stage_has_its_header(stage, ru, en) -> None:
    mode, key, params = stage

    def run():
        modes.enter(mode, key, **params)
        return modes.with_header("Текст", "ru"), modes.with_header("Text", "en")

    assert _in_fresh_context(run) == (f"{ru}\n\nТекст", f"{en}\n\nText")


def test_no_stage_no_header() -> None:
    assert _in_fresh_context(lambda: modes.with_header("Справка /help", "ru")) == "Справка /help"


@pytest.mark.parametrize("already", [ru for _, ru, _ in CASES] + [en for _, _, en in CASES])
def test_a_text_with_any_mode_prefix_gets_no_second_header(already) -> None:
    def run():
        modes.enter(echo.MODE, "echo_step", n=1)
        return modes.with_header(f"{already}\n\nВопрос", "ru")

    assert _in_fresh_context(run) == f"{already}\n\nВопрос"
