"""Справка: чистая часть - память диалога, промпты.

Запуск: .venv/bin/python -m pytest tests/test_reference_mode.py -q
"""

import pytest

from tgbot import reference_mode as rm

# --- 1.2 Dialogs -------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_only_the_last_two_pairs_are_kept():
    dialogs = rm.Dialogs(clock=Clock())
    for n in range(3):
        dialogs.remember(1, f"q{n}", f"a{n}")
    assert dialogs.history(1) == [("q1", "a1"), ("q2", "a2")]


def test_the_history_expires_after_thirty_minutes():
    clock = Clock()
    dialogs = rm.Dialogs(clock=clock)
    dialogs.remember(1, "q", "a")
    clock.now += rm.DIALOG_TTL_S - 1
    assert dialogs.history(1) == [("q", "a")]
    clock.now += 2
    assert dialogs.history(1) == []
    assert dialogs.get(1) is None


def test_drop_forgets_the_user():
    dialogs = rm.Dialogs(clock=Clock())
    dialogs.remember(1, "q", "a")
    dialogs.drop(1)
    assert dialogs.get(1) is None
    assert dialogs.history(1) == []


def test_the_last_question_and_first_answer_are_kept_for_the_retry():
    dialogs = rm.Dialogs(clock=Clock())
    dialogs.remember(1, "q", "a1")
    state = dialogs.get(1)
    assert (state.question, state.first_answer, state.attempt) == ("q", "a1", 1)
    dialogs.remember_retry(1, "a2", state)
    state = dialogs.get(1)
    assert (state.first_answer, state.attempt) == ("a1", 2)
    # в истории - понятый ли, нет ли, но последний показанный ответ
    assert dialogs.history(1) == [("q", "a2")]


def test_users_do_not_share_a_dialog():
    dialogs = rm.Dialogs(clock=Clock())
    dialogs.remember(1, "q", "a")
    assert dialogs.history(2) == []


# --- 1.3 промпты --------------------------------------------------------------

def test_the_prompt_carries_the_question_history_and_howto(monkeypatch):
    monkeypatch.setattr(rm, "read_howto", lambda: "HOWTO-TEXT")
    prompt = rm.prompt("что такое нода?", [("q0", "a0")], lang="ru")
    assert "что такое нода?" in prompt
    assert "q0" in prompt and "a0" in prompt
    assert "HOWTO-TEXT" in prompt
    assert "OFFER" not in prompt


def test_the_prompt_without_howto_still_builds(monkeypatch):
    monkeypatch.setattr(rm, "read_howto", lambda: None)
    prompt = rm.prompt("вопрос", [], lang="en")
    assert "вопрос" in prompt


def test_the_retry_prompt_carries_the_first_answer():
    prompt = rm.retry_prompt("вопрос", "первый ответ", lang="ru")
    assert "вопрос" in prompt and "первый ответ" in prompt


def test_a_missing_howto_is_none(tmp_path):
    assert rm.read_howto(tmp_path / "nope.md") is None


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_the_chat_prompt_has_the_howto_history_and_the_return_marker(monkeypatch, lang):
    """reference-language-and-grounding: облегчённый ответ - без списка
    файлов, но с инструкцией бота, историей и маркером возврата."""
    monkeypatch.setattr(rm, "read_howto", lambda: "ИНСТРУКЦИЯ")
    text = rm.chat_prompt("привет", [("q1", "a1")], lang=lang)
    assert "привет" in text and "ИНСТРУКЦИЯ" in text and "a1" in text
    assert rm.NEED_SOURCES in text
    assert "OFFER" not in text
    assert "Доступные файлы" not in text and "Available files" not in text


@pytest.mark.parametrize("answer, expected", [
    ("NEED_SOURCES", True), ("  NEED_SOURCES\n", True), ("Привет! NEED_SOURCES", False),
])
def test_the_return_marker_is_the_whole_answer(answer, expected):
    assert rm.needs_sources(answer) is expected


@pytest.mark.parametrize("lang, default", [("ru", "этот продукт"), ("en", "this product")])
def test_every_prompt_names_the_product(monkeypatch, lang, default):
    """Имя продукта - из конфига (`TGBOT_PRODUCT_NAME`), без него - нейтральное."""
    monkeypatch.setattr(rm, "read_howto", lambda: None)
    builders = (
        lambda **kw: rm.prompt("вопрос", [], **kw),
        lambda **kw: rm.chat_prompt("вопрос", [], **kw),
        lambda **kw: rm.retry_prompt("вопрос", "ответ", **kw),
    )
    for build in builders:
        assert default in build(lang=lang)
        assert "Акме" in build(lang=lang, product="Акме")
