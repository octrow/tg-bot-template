"""Сбои простым языком, код инцидента, кнопка и пуш админам
(спека failure-messages)."""

import asyncio

import pytest

from tgbot import alerts, db
from tgbot.agent import failures, ladder
from tgbot.locales.en import STRINGS as EN
from tgbot.locales.ru import STRINGS as RU

from .conftest import ADMIN_ID, log_rows, make_context, make_update

USER = 4242
TECH_WORDS = ("ступень", "stage", "OpenRouter", "glm", "KeyError", "OPENROUTER_API_KEY", "502")


@pytest.fixture(autouse=True)
def _alerts_reset():
    alerts.reset()
    yield
    alerts.reset()


FAILURES = [
    (failures.StageFailure("openrouter", failures.FAILURE_TIMEOUT, exception=failures.LADDER_DEADLINE), "FAILURE_TIMEOUT"),
    (failures.StageFailure("openrouter", failures.FAILURE_REJECTED, detail="degenerate"), "FAILURE_BAD_ANSWER"),
    (failures.StageFailure("openrouter", failures.FAILURE_PROVIDER, detail="502 Bad Gateway"), "FAILURE_UNAVAILABLE"),
    (failures.StageFailure("openrouter", failures.FAILURE_UNKNOWN, exception="KeyError"), "FAILURE_UNAVAILABLE"),
    (failures.StageFailure("openrouter", failures.FAILURE_TIMEOUT, exception="ReadTimeout"), "FAILURE_UNAVAILABLE"),
    (failures.StageFailure("openrouter", failures.FAILURE_NO_KEY, detail="OPENROUTER_API_KEY"), "FAILURE_NOT_CONFIGURED"),
]


@pytest.mark.parametrize("failure, key", FAILURES)
@pytest.mark.parametrize("lang, catalog", [("ru", RU), ("en", EN)])
def test_failure_text_is_plain_and_carries_the_code(failure, key, lang, catalog):
    text = failures.failure_text(lang, failure, 48213)
    assert catalog[key].split("{wait}")[0] in text and "48213" in text
    assert "{" not in text  # плейсхолдер подставлен
    assert not any(word in text for word in TECH_WORDS)


def _down(monkeypatch, exc=RuntimeError("502 Bad Gateway")):
    async def fail(prompt, **kwargs):
        raise exc

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", fail)


def _config(**over):
    from types import SimpleNamespace

    return SimpleNamespace(**{
        "repo_path": None,
        "openrouter_api_key": "sk-or", "openrouter_models": ("m",),
        "llm_provider": "openrouter", "agent_cache_ttl_s": 0, **over,
    })


async def test_code_is_the_failure_record_and_button_points_to_it(conn, monkeypatch):
    _down(monkeypatch)
    message = make_update(USER).effective_message

    await ladder.call_agent(_config(), conn, "промпт", USER, "reference_question",
                           message=message, question="почему молчит агент?")

    record = log_rows(conn, "llm_failure")[0]
    code = conn.execute("SELECT id FROM log WHERE kind = 'llm_failure'").fetchone()["id"]
    assert record["stage"] == "openrouter" and record["error"] == "502 Bad Gateway"
    assert record["question"] == "почему молчит агент?" and record["models"] == ["m"]
    assert f"{RU['FAILURE_CODE'].format(code=code)}" in message.replies[-1]
    assert message.markups[-1].inline_keyboard[0][0].callback_data == f"fail:{code}"


# --- кнопка "Сообщить разработчикам" ---------------------------------------


async def _press(handlers, data, user_id=USER):
    update = make_update(user_id, callback_data=data)
    await handlers["failure_callback"](update, make_context())
    return update


async def _incident(conn, monkeypatch, user_id=USER) -> int:
    _down(monkeypatch)
    await ladder.call_agent(_config(), conn, "промпт", user_id, "reference_question",
                           message=make_update(user_id).effective_message, question="вопрос?")
    return conn.execute("SELECT max(id) AS id FROM log WHERE kind = 'llm_failure'").fetchone()["id"]


async def test_report_button_creates_feedback_once(conn, handlers, monkeypatch):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    code = await _incident(conn, monkeypatch)

    first = await _press(handlers, f"fail:{code}")
    second = await _press(handlers, f"fail:{code}")

    feedback = db.unread_feedback(conn)
    assert len(feedback) == 1 and str(code) in feedback[0]["text"] and "вопрос?" in feedback[0]["text"]
    assert RU["FAILURE_REPORTED"] in first.effective_message.replies
    assert first.effective_message.markup_edits  # кнопка снята
    assert RU["FAILURE_ALREADY_REPORTED"] in second.effective_message.replies


async def test_foreign_or_wrong_record_creates_no_feedback(conn, handlers, monkeypatch):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    db.add_user(conn, USER + 1, added_by=ADMIN_ID)
    code = await _incident(conn, monkeypatch, user_id=USER + 1)
    other = db.log_event(conn, USER, "reference_question", {"text": "x"})

    await _press(handlers, f"fail:{code}")  # чужой инцидент
    await _press(handlers, f"fail:{other}")  # своя запись, но не сбой
    await _press(handlers, "fail:abc")

    assert db.unread_feedback(conn) == []


async def test_report_does_not_touch_an_open_session(conn, handlers, monkeypatch):
    db.add_user(conn, USER, added_by=ADMIN_ID)
    code = await _incident(conn, monkeypatch)
    session = db.create_session(conn, USER, "title", {"fields": {"goal": "x"}})

    await _press(handlers, f"fail:{code}")

    after = db.active_session(conn, USER)
    assert after["id"] == session["id"] and after["data"] == session["data"]
    assert after["mode"] == "title" and after["status"] == session["status"]


async def test_intermediate_status_has_no_button():
    from tgbot import utils

    from .conftest import FakeMessage

    message = FakeMessage()
    async with utils.waiting(message, "Ищу ответ...") as wait:
        await wait.on_step(1, "openrouter")
    assert message.markups == [None]  # только статус, без кнопки
    assert "Код" not in message.sent[0].edited[0]


async def test_handler_error_without_user_has_no_button(conn):
    from tgbot.handlers.common import make_error_handler

    from .test_bot import FakeApplication, FakeBot, make_error_context, make_failing_update

    bot = FakeBot()
    update = make_failing_update(user_id=None, chat_id=77)
    await make_error_handler(conn)(update, make_error_context(RuntimeError("x"), bot, FakeApplication()))
    assert bot.markups == [None]


# --- пуш админам -------------------------------------------------------------


class _Admins:
    def __init__(self, fail_for=()):
        self.sent: list[tuple[int, str]] = []
        self.fail_for = set(fail_for)

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.fail_for:
            raise RuntimeError("blocked")
        self.sent.append((chat_id, text))


async def _drain():
    await asyncio.gather(*alerts._tasks)


async def test_push_window_counts_the_storm(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(alerts, "clock", lambda: now[0])
    bot = _Admins()
    alerts.configure(bot, [1, 2])

    alerts.notify("timeout", 10, mode="reference_question", model="glm", question="почему?")
    await _drain()
    assert [a for a, _ in bot.sent] == [1, 2] and "10" in bot.sent[0][1] and "почему?" in bot.sent[0][1]

    for code in range(11, 15):
        now[0] += 60
        alerts.notify("timeout", code)
    await _drain()
    assert len(bot.sent) == 2  # окно занято

    now[0] += alerts.WINDOW_S
    alerts.notify("timeout", 15)
    await _drain()
    assert RU["FAILURE_PUSH_MORE"].format(n=4) in bot.sent[-1][1]


@pytest.mark.parametrize("kind", ["llm_slow", "rejected_fixed", "judge"])
async def test_quiet_kinds_do_not_push(kind):
    bot = _Admins()
    alerts.configure(bot, [1])
    alerts.notify(kind, 1)
    await _drain()
    assert bot.sent == []


async def test_one_admin_down_does_not_stop_the_others_or_the_user(conn, monkeypatch):
    bot = _Admins(fail_for={1})
    alerts.configure(bot, [1, 2])
    _down(monkeypatch, failures.AnswerRejected("degenerate"))
    message = make_update(USER).effective_message

    await ladder.call_agent(_config(), conn, "промпт", USER, "reference_question", message=message)
    await _drain()

    assert [a for a, _ in bot.sent] == [2]
    assert RU["FAILURE_BAD_ANSWER"] in message.replies[-1]


async def test_ladder_fixed_defect_does_not_push(conn, monkeypatch):
    bot = _Admins()
    alerts.configure(bot, [1])

    async def ask(prompt, *, model, **kwargs):
        if model == "m":
            raise failures.AnswerRejected("degenerate")
        return "годный ответ"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)
    answer = await ladder.call_agent(
        _config(openrouter_models=("m", "m2")), conn, "промпт", USER, "reference_question",
        message=make_update(USER).effective_message,
    )
    await _drain()

    assert answer == "годный ответ" and bot.sent == []


@pytest.mark.parametrize("hard, ru, en", [(600.0, "10 мин", "10 min"), (60.0, "1 мин", "1 min"), (45.0, "45 с", "45 s")])
def test_timeout_text_names_the_real_deadline(hard, ru, en):
    """Жёсткий порог свой у класса вызова (лёгкий - 60 с, env) - текст называет
    его, а не всегда 10 минут."""
    failure = ladder._deadline_failure([("openrouter", "m")], hard)
    assert f"не ответила за {ru}:" in failures.failure_text("ru", failure)
    assert f"within {en}:" in failures.failure_text("en", failure)


async def test_light_call_deadline_says_seconds_not_ten_minutes(conn, monkeypatch):
    async def slow(prompt, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", slow)
    message = make_update(USER).effective_message
    config = _config(llm_steps={"light": (0.01, 0.02, 0.05)})

    await ladder.call_agent(config, conn, "промпт", USER, "тест", message=message, light=True)

    assert "0.05 с" in message.replies[-1] and "10 мин" not in message.replies[-1]
