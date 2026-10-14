"""Справка в боте: развилка свободного текста, оценка и передача
людям (спека reference-mode).

Агент - заглушка: тесты проверяют развилку, промпт и обработку ответа, а не LLM.

Запуск: .venv/bin/python -m pytest tests/test_reference_bot.py -q
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from tgbot.handlers import reference as reference_handlers
from tgbot import db, reference_mode, utils
from tgbot.agent.result import AgentResult
from tgbot.locales.en import STRINGS as EN
from tgbot.locales.ru import STRINGS as RU

from .conftest import make_handlers, ADMIN_ID, buttons, log_rows, make_context, make_update

USER_ID = 42
QUESTION = "что такое нода в сценарии?"


@pytest.fixture
def agent(monkeypatch):
    """Заглушка `call_agent` в bot.py: пишет вызовы, отвечает по очереди из
    `answers` (последний ответ повторяется)."""
    calls: list[dict] = []
    answers: list = [AgentResult("Нода - это шаг сценария.", files_read=["a.py"])]

    async def fake_call_agent(config, conn, prompt, user_id, context_label, **kwargs):
        calls.append({"prompt": prompt, "context": context_label, "light": kwargs.get("light")})
        return answers[min(len(calls) - 1, len(answers) - 1)]

    monkeypatch.setattr(reference_handlers, "call_agent", fake_call_agent)
    return SimpleNamespace(calls=calls, answers=answers)


def _allow(conn):
    db.add_user(conn, USER_ID, added_by=ADMIN_ID)


async def _ask(handlers, text=QUESTION, user_id=USER_ID):
    update = make_update(user_id, text=text)
    await handlers["fallback"](update, make_context())
    return update


async def _click(handlers, data, user_id=USER_ID):
    update = make_update(user_id, callback_data=data)
    await handlers["reference_callback"](update, make_context())
    return update


def _token(handlers, user_id=USER_ID) -> str:
    return handlers["dialogs"].get(user_id).token


def _bad(handlers, attempt=1) -> str:
    """callback_data "Не понятно" под ответом текущего диалога."""
    return f"ref:bad:{attempt}:{_token(handlers)}"


def _answers(update) -> str:
    return " ".join(update.effective_message.replies)


# --- 3.1 развилка свободного текста ------------------------------------------

async def test_free_text_gets_a_reference_answer(handlers, conn, agent):
    _allow(conn)
    update = await _ask(handlers)
    assert "шаг сценария" in _answers(update)
    assert agent.calls[0]["context"] == reference_mode.REFERENCE_CONTEXT


async def test_the_question_and_howto_reach_the_prompt(handlers, conn, agent, monkeypatch):
    monkeypatch.setattr(reference_mode, "read_howto", lambda: "HOWTO-TEXT")
    _allow(conn)
    await _ask(handlers)
    prompt = agent.calls[0]["prompt"]
    assert QUESTION in prompt
    assert "HOWTO-TEXT" in prompt  # инструкция бота целиком


async def test_the_product_name_from_the_config_reaches_the_prompt(access, conn, agent, no_clone_check):
    import dataclasses


    from .conftest import TEST_CONFIG

    handlers = make_handlers(access, dataclasses.replace(TEST_CONFIG, product_name="Акме"))
    _allow(conn)
    await _ask(handlers)
    assert "Акме" in agent.calls[0]["prompt"]


async def test_a_user_outside_the_allowlist_gets_no_reference(handlers, conn, agent):
    update = await _ask(handlers)
    assert agent.calls == []
    assert "шаг сценария" not in _answers(update)


async def test_a_mistyped_command_is_not_a_question(handlers, conn, agent):
    _allow(conn)
    update = await _ask(handlers, "/whomis")
    assert agent.calls == []
    assert RU["UNKNOWN_COMMAND"] in update.effective_message.replies


async def test_an_unavailable_model_is_said_plainly(handlers, conn, monkeypatch):
    """Модель недоступна - простой текст, код инцидента и кнопка; в записи о
    сбое вопрос и подробности (спека failure-messages)."""
    _allow(conn)

    async def down(prompt, **kwargs):
        raise RuntimeError("502 Bad Gateway")

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", down)
    update = await _ask(handlers)

    failure = log_rows(conn, "llm_failure")[0]
    assert failure["question"] == QUESTION and failure["error"] == "502 Bad Gateway"
    code = conn.execute("SELECT id FROM log WHERE kind = 'llm_failure'").fetchone()["id"]
    text = update.effective_message.replies[-1]
    assert RU["FAILURE_UNAVAILABLE"] in text and str(code) in text
    assert "502" not in text and "OpenRouter" not in text


async def test_an_answer_without_files_is_marked(handlers, conn, agent):
    _allow(conn)
    agent.answers[:] = [AgentResult("Догадка.", files_read=[])]
    update = await _ask(handlers)
    assert RU["REFERENCE_UNVERIFIED"] in _answers(update)


async def test_the_question_is_logged_with_its_text(handlers, conn, agent):
    _allow(conn)
    await _ask(handlers)
    row = log_rows(conn, "reference_question")[0]
    assert row["length"] == len(QUESTION)
    assert row["text"] == QUESTION


# --- кнопки под ответом -------------------------------------------------------

def _rating_rows(handlers, attempt=1):
    # clearer-bot-output: "Понятно" ничего не меняло - под ответом только "Не понятно".
    return [(RU["REFERENCE_UNCLEAR"], _bad(handlers, attempt))]


async def test_a_plain_answer_has_only_the_rating(handlers, conn, agent):
    _allow(conn)
    update = await _ask(handlers)
    assert buttons(update) == _rating_rows(handlers)


# --- 5.1-5.2 оценка, повтор, передача людям ----------------------------------

async def test_an_answer_has_no_clear_button(handlers, conn, agent):
    _allow(conn)
    update = await _ask(handlers)
    labels = [label for label, _ in buttons(update)]
    assert RU["REFERENCE_CLEAR"] not in labels
    assert RU["REFERENCE_UNCLEAR"] in labels


async def test_an_old_clear_button_still_works(handlers, conn, agent):
    """Кнопка "Понятно" в сообщениях до clearer-bot-output не падает."""
    _allow(conn)
    await _ask(handlers)
    update = await _click(handlers, "ref:ok:1")
    rating = log_rows(conn, "reference_rated")[0]
    assert (rating["attempt"], rating["clear"]) == (1, True)
    assert update.effective_message.edit_markups[-1] is None


async def test_unclear_first_answer_gets_one_simpler_retry(handlers, conn, agent):
    _allow(conn)
    agent.answers[:] = [
        AgentResult("Сложный ответ.", files_read=["a.py"]),
        AgentResult("Простой ответ.", files_read=["a.py"]),
    ]
    await _ask(handlers)
    update = await _click(handlers, _bad(handlers, 1))
    assert "Простой ответ." in _answers(update)
    retry = agent.calls[-1]["prompt"]
    assert "Сложный ответ." in retry and QUESTION in retry
    assert buttons(update) == _rating_rows(handlers, 2)


async def test_an_unverified_retry_says_so(handlers, conn, agent):
    _allow(conn)
    agent.answers[:] = [
        AgentResult("Ответ.", files_read=["a.py"]),
        AgentResult("Повтор.", files_read=[]),
    ]
    await _ask(handlers)
    update = await _click(handlers, _bad(handlers, 1))
    assert RU["REFERENCE_UNVERIFIED"] in _answers(update)


async def test_unclear_retry_offers_people_not_a_third_answer(handlers, conn, agent):
    _allow(conn)
    await _ask(handlers)
    await _click(handlers, _bad(handlers, 1))
    calls_before = len(agent.calls)
    update = await _click(handlers, _bad(handlers, 2))
    assert len(agent.calls) == calls_before
    assert RU["REFERENCE_HUMAN_PROMPT"] in update.effective_message.replies
    assert buttons(update) == [(RU["REFERENCE_HUMAN"], f"ref:human:{_token(handlers)}")]


async def test_passing_to_people_saves_a_marked_feedback(handlers, conn, agent):
    _allow(conn)
    agent.answers[:] = [
        AgentResult("Ответ один.", files_read=["a.py"]),
        AgentResult("Ответ два.", files_read=["a.py"]),
    ]
    await _ask(handlers)
    await _click(handlers, _bad(handlers, 1))
    await _click(handlers, _bad(handlers, 2))
    update = await _click(handlers, f"ref:human:{_token(handlers)}")
    text = conn.execute("SELECT text FROM feedback").fetchone()["text"]
    assert QUESTION in text and "Ответ один." in text and "Ответ два." in text
    assert text.startswith("[Вопрос из Справки")
    assert RU["REFERENCE_HUMAN_SENT"] in update.effective_message.replies
    assert log_rows(conn, "reference_handoff")
    ratings = [(r["attempt"], r["clear"]) for r in log_rows(conn, "reference_rated")]
    assert ratings == [(1, False), (2, False)]


async def test_a_button_of_an_older_dialog_is_stale(handlers, conn, agent):
    """"Не понятно" под ответом прошлого диалога не повторяет новый вопрос."""
    _allow(conn)
    await _ask(handlers)
    old = _bad(handlers, 1)
    await _ask(handlers, "а если кампания на паузе?")
    calls_before = len(agent.calls)

    update = await _click(handlers, old)

    assert len(agent.calls) == calls_before
    assert update.effective_message.replies == []
    assert handlers["dialogs"].get(USER_ID).attempt == reference_mode.FIRST_ATTEMPT


async def test_a_button_without_dialog_token_is_stale(handlers, conn, agent):
    """Кнопки до метки диалога (`ref:bad:1`, `ref:human`) ничего не делают."""
    _allow(conn)
    await _ask(handlers)

    await _click(handlers, "ref:bad:1")
    update = await _click(handlers, "ref:human")

    assert len(agent.calls) == 1
    assert update.effective_message.replies == []
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0


async def test_a_late_retry_does_not_overwrite_a_newer_dialog(
    handlers, conn, agent, monkeypatch
):
    """Повтор готовился, пока пришёл новый вопрос: ответ повтора уходит
    пользователю без кнопки оценки, а новый диалог остаётся как был."""
    _allow(conn)
    agent.answers[:] = [AgentResult("Первый ответ.", files_read=["a.py"])]
    await _ask(handlers)
    release = asyncio.Event()

    async def call_agent(config, conn, prompt, user_id, context_label, **kwargs):
        if "новый вопрос" in prompt:
            return AgentResult("Ответ на новый вопрос.", files_read=["a.py"])
        await release.wait()  # повтор первого вопроса держим до нового вопроса
        return AgentResult("Ответ повтора.", files_read=["a.py"])

    monkeypatch.setattr(reference_handlers, "call_agent", call_agent)
    retry = asyncio.create_task(_click(handlers, _bad(handlers, 1)))
    for _ in range(20):
        await asyncio.sleep(0)
    await _ask(handlers, "новый вопрос")
    newer = handlers["dialogs"].get(USER_ID)
    release.set()
    update = await asyncio.wait_for(retry, timeout=5)

    assert "Ответ повтора." in _answers(update)
    assert buttons(update) == []
    dialog = handlers["dialogs"].get(USER_ID)
    assert dialog is newer
    assert dialog.question == "новый вопрос"
    assert dialog.attempt == reference_mode.FIRST_ATTEMPT
    assert dialog.pairs[-1] == ("новый вопрос", "Ответ на новый вопрос.")


# --- 6.1 контекст диалога -----------------------------------------------------

async def test_a_follow_up_carries_the_previous_pair(handlers, conn, agent):
    _allow(conn)
    await _ask(handlers)
    await _ask(handlers, "а если кампания на паузе?")
    follow_up = agent.calls[-1]["prompt"]
    assert QUESTION in follow_up and "шаг сценария" in follow_up


async def test_an_old_dialog_is_forgotten(handlers, conn, agent):
    _allow(conn)
    clock = [1000.0]
    handlers["dialogs"]._clock = lambda: clock[0]
    await _ask(handlers)
    clock[0] += reference_mode.DIALOG_TTL_S + 1
    await _ask(handlers, "а если кампания на паузе?")
    assert "шаг сценария" not in agent.calls[-1]["prompt"]


async def test_reference_questions_are_not_tasks(handlers, conn, agent):
    _allow(conn)
    await _ask(handlers)
    await _ask(handlers, "и ещё вопрос")
    # Сессия у Справки есть, но только журнальная: сессией режима она не становится.
    assert db.active_session(conn, USER_ID) is None


# --- гарды, найденные мутационной проверкой ----------------------------------

async def test_clear_does_not_call_the_model_again(handlers, conn, agent):
    _allow(conn)
    await _ask(handlers)
    await _click(handlers, "ref:ok:1")
    assert len(agent.calls) == 1


async def test_a_repeated_unclear_on_the_first_answer_retries_once(handlers, conn, agent):
    """Двойное нажатие "Не понятно" под первым ответом - один повтор, не два."""
    _allow(conn)
    await _ask(handlers)
    await _click(handlers, _bad(handlers, 1))
    await _click(handlers, _bad(handlers, 1))
    assert len(agent.calls) == 2


async def test_two_unclear_clicks_while_the_retry_is_prepared_give_one_retry(
    handlers, conn, agent, monkeypatch
):
    """Второе "Не понятно", пока первый повтор ещё готовится (медленная
    модель), повтор не запускает и молчит."""
    _allow(conn)
    await _ask(handlers)
    release = asyncio.Event()
    fast_call = reference_handlers.call_agent

    async def slow_call_agent(*args, **kwargs):
        await release.wait()
        return await fast_call(*args, **kwargs)

    monkeypatch.setattr(reference_handlers, "call_agent", slow_call_agent)
    first = asyncio.create_task(_click(handlers, _bad(handlers, 1)))
    second = asyncio.create_task(_click(handlers, _bad(handlers, 1)))
    for _ in range(20):  # оба нажатия доходят до модели или до отказа
        await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert len(agent.calls) == 2  # первый ответ и один повтор
    assert second.result().effective_message.replies == []


# --- /start --------------------------------------------------------------------

async def test_start_then_text_goes_to_reference(conn, handlers, agent):
    _allow(conn)
    update = make_update(USER_ID, text="/start")
    await handlers["start"](update, make_context())
    assert update.effective_message.replies == [RU["START"]]
    assert update.effective_message.parse_modes[-1] == "HTML"  # жирная суть
    await _ask(handlers)
    assert agent.calls


# --- таймер ожидания (show-wait-timer) ----------------------------------------

# Сколько оборотов цикла "думает" медленная модель: с мгновенным сном тикера
# этого хватает на несколько тиков таймера.
SLOW_ANSWER_TURNS = 20


async def _instant(_seconds):
    await asyncio.sleep(0)


@pytest.fixture
def slow_agent(monkeypatch):
    """`call_agent`, который отвечает не сразу: успевает пройти несколько
    тиков таймера (сон тикера подменён на мгновенный)."""
    monkeypatch.setattr(utils, "wait_sleep", _instant)

    async def fake_call_agent(config, conn, prompt, user_id, context_label, **kwargs):
        for _ in range(SLOW_ANSWER_TURNS):
            await asyncio.sleep(0)
        return AgentResult("Нода - это шаг сценария.", files_read=["a.py"])

    monkeypatch.setattr(reference_handlers, "call_agent", fake_call_agent)


async def test_a_long_reference_answer_shows_the_elapsed_time(conn, handlers, slow_agent):
    _allow(conn)
    update = await _ask(handlers)
    message = update.effective_message
    status = message.sent[message.replies.index(RU["STATUS_REFERENCE"])]
    elapsed = RU["STATUS_ELAPSED"].format(clock="0:10")
    assert status.edited[0] == f"{RU['STATUS_REFERENCE']}\n{elapsed}"
    assert status._text in message.deleted


async def test_the_retry_has_its_own_waiting_status(conn, handlers, agent):
    _allow(conn)
    await _ask(handlers)
    update = await _click(handlers, _bad(handlers, 1))
    replies = update.effective_message.replies
    assert RU["STATUS_REFERENCE_RETRY"] in replies
    assert RU["STATUS_REFERENCE"] not in replies
    assert RU["STATUS_REFERENCE_RETRY"] in update.effective_message.deleted


# --- журнал Справки -------------------------------------------------------------

def _reference_log_file(tmp_path):
    files = [p for p in (tmp_path / "logs").glob("*.jsonl") if p.name != "bot.jsonl"]
    assert len(files) == 1
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]


async def test_a_reference_dialog_gets_its_own_log_file(handlers, conn, agent, tmp_path):
    _allow(conn)
    await _ask(handlers)
    records = _reference_log_file(tmp_path)
    kinds = [r["kind"] for r in records]
    assert "reference_question" in kinds and "reference_answer" in kinds
    question = next(r for r in records if r["kind"] == "reference_question")
    assert question["payload"]["text"] == QUESTION
    answer = next(r for r in records if r["kind"] == "reference_answer")
    assert "шаг сценария" in answer["payload"]["text"]
    statuses = [r["status"] for r in conn.execute("SELECT status FROM sessions")]
    assert statuses == [db.REFERENCE_STATUS]


async def test_follow_ups_share_the_reference_session(handlers, conn, agent, tmp_path):
    _allow(conn)
    await _ask(handlers)
    await _ask(handlers, "а зачем она нужна?")
    assert conn.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"] == 1
    assert db.active_session(conn, USER_ID) is None  # Справка - не сессия режима


# --- спека reference-mode: классификатор намерения ---------------------------

def _patch_classify(monkeypatch, classify):
    """Трек Справки от теста."""
    monkeypatch.setattr(reference_handlers.intent, "classify", classify)


@pytest.fixture
def fast_intent(monkeypatch):
    """Классификатор намерения отвечает сразу; трек задаёт тест."""
    result = SimpleNamespace(track=None)

    async def classify(conn, user_id, api_key, text, intents):
        return result.track

    _patch_classify(monkeypatch, classify)
    return result


async def test_a_slow_classifier_does_not_hold_the_answer(
    access, conn, agent, no_clone_check, monkeypatch
):
    import dataclasses
    import time

    from tgbot import intent

    from .conftest import TEST_CONFIG

    async def slow(*args):
        await asyncio.sleep(10)

    monkeypatch.setattr(intent, "INTENT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(intent.jev, "choices", slow)
    config = dataclasses.replace(TEST_CONFIG, typesafe_api_key="key")
    handlers = make_handlers(access, config)
    _allow(conn)
    started = time.monotonic()
    update = await _ask(handlers)
    assert time.monotonic() - started < 2
    assert "шаг сценария" in _answers(update)
    assert log_rows(conn, "reference_intent")[0]["outcome"] == "unavailable:timeout"
    assert buttons(update) == _rating_rows(handlers)


async def test_a_failing_classifier_does_not_lose_the_answer(
    handlers, conn, agent, monkeypatch
):
    """Классификатор упал: ответ, который шёл минуту, доходит."""

    async def broken(conn, user_id, api_key, text, intents):
        raise RuntimeError("boom")

    _patch_classify(monkeypatch, broken)
    _allow(conn)
    agent.answers[:] = [AgentResult("Ответ.", files_read=["a.py"])]
    update = await _ask(handlers, "у нас кампания перестала звонить")
    assert "Ответ." in _answers(update)
    assert buttons(update) == _rating_rows(handlers)


# --- ответ, который думает долго ----------------------------------------------

@pytest.fixture
def hanging_agent(monkeypatch):
    """Справка думает, пока тест не отпустит; остальные вызовы отвечают сразу."""
    release = asyncio.Event()
    calls = SimpleNamespace(reference=0, cancelled=0)

    async def fake_call_agent(config, conn, prompt, user_id, context_label, **kwargs):
        if context_label != reference_mode.REFERENCE_CONTEXT:
            return AgentResult("Другой вызов.", files_read=["b.py"])
        calls.reference += 1
        try:
            await release.wait()
        except asyncio.CancelledError:
            calls.cancelled += 1
            raise
        return AgentResult("Ответ Справки.", files_read=["a.py"])

    monkeypatch.setattr(reference_handlers, "call_agent", fake_call_agent)
    calls.release = release
    return calls


async def _settle(turns=20):
    for _ in range(turns):
        await asyncio.sleep(0)


async def test_an_outside_cancel_of_the_answer_is_not_swallowed(
    handlers, conn, hanging_agent, fast_intent
):
    """Остановка бота: отмена должна дойти до обработчика."""
    _allow(conn)
    asked = asyncio.create_task(_ask(handlers))
    await _settle()
    asked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asked


# --- спека chat-text-formatting: простыня Справки приходит абзацами --------------------

async def test_a_long_reference_answer_reaches_the_chat_in_paragraphs(handlers, conn, agent):
    _allow(conn)
    sentence = "Кампания звонит только в рабочие часы, которые заданы в её настройках."
    agent.answers[:] = [
        AgentResult(" ".join([sentence] * 9) + "", files_read=["a.py"])
    ]
    update = await _ask(handlers)
    reply = _answers(update)
    assert "\n\n" in reply
    assert all(len(part) <= utils.PARAGRAPH_MAX_CHARS for part in reply.split("\n\n"))


# --- спека chat-text-formatting: длинный блок заметен в логе ------------------------

async def test_a_six_line_block_is_logged_and_the_answer_is_unchanged(
    handlers, conn, agent, caplog
):
    _allow(conn)
    lines = "\n".join(f"Шаг {i}." for i in range(1, 7))
    agent.answers[:] = [AgentResult(lines + "", files_read=["a.py"])]
    with caplog.at_level("INFO", logger="tgbot.utils"):
        update = await _ask(handlers)
    assert "answer block too long: 6 lines" in caplog.text
    assert "Шаг 1.\nШаг 2." in _answers(update)


async def test_short_blocks_leave_no_log_record(handlers, conn, agent, caplog):
    _allow(conn)
    text = "**Суть.**\n\nШаг 1.\nШаг 2.\n\n" + "\n".join(["> подробно"] * 8)
    agent.answers[:] = [AgentResult(text + "", files_read=["a.py"])]
    with caplog.at_level("INFO", logger="tgbot.utils"):
        await _ask(handlers)
    assert "answer block too long" not in caplog.text


# --- session-log-completeness: начало диалога в файле сессии Справки ----------


def _logged_update(user_id=USER_ID, text=None, **kwargs):
    """Апдейт, который можно записать в журнал: `log_update` берёт `to_dict`."""
    update = make_update(user_id, text=text, **kwargs)
    update.to_dict = lambda: {"message": {"text": text}}
    return update


async def _incoming(handlers, update):
    """Как в приложении: сначала журнал апдейта (group=-3), потом обработчик."""
    await handlers["log_update"](update, make_context())
    await handlers["fallback"](update, make_context())


def _common_log(tmp_path) -> str:
    path = tmp_path / "logs" / "bot.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def test_the_reference_file_starts_with_the_incoming_update(handlers, conn, agent, tmp_path):
    _allow(conn)
    # По-английски при русском интерфейсе - с подсказкой о смене языка.
    await _incoming(handlers, _logged_update(text="how do I add a new campaign to the product?"))
    kinds = [r["kind"] for r in _reference_log_file(tmp_path)]
    assert kinds.index("tg_update") < kinds.index("language_offer") < kinds.index("reference_question")
    common = _common_log(tmp_path)
    assert '"tg_update"' not in common and '"language_offer"' not in common


async def test_a_command_or_a_button_does_not_open_a_reference_session(handlers, conn, tmp_path):
    _allow(conn)
    await handlers["log_update"](_logged_update(text="/logs"), make_context())
    await handlers["log_update"](_logged_update(callback_data="lang:en"), make_context())
    assert conn.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"] == 0
    assert _common_log(tmp_path).count('"tg_update"') == 2


# --- reference-language-and-grounding: язык ответа - язык вопроса -------------

EN_QUESTION = "how do I switch the bot to English?"


async def test_an_english_question_is_answered_in_english(handlers, conn, agent):
    _allow(conn)
    db.set_lang(conn, USER_ID, "ru")
    update = await _ask(handlers, EN_QUESTION)
    assert EN["STATUS_REFERENCE"] in update.effective_message.replies
    assert "Answer in the user's language (en)" in agent.calls[0]["prompt"]
    assert (EN["REFERENCE_UNCLEAR"], _bad(handlers, 1)) in buttons(update)
    assert db.get_lang(conn, USER_ID) == "ru"


async def test_the_retry_of_an_english_question_is_in_english(handlers, conn, agent):
    _allow(conn)
    db.set_lang(conn, USER_ID, "ru")
    await _ask(handlers, EN_QUESTION)
    update = await _click(handlers, _bad(handlers, 1))
    assert EN["STATUS_REFERENCE_RETRY"] in update.effective_message.replies
    assert "(en)" in agent.calls[1]["prompt"]


async def test_a_short_message_is_answered_in_the_user_language(handlers, conn, agent):
    _allow(conn)
    db.set_lang(conn, USER_ID, "en")
    await _ask(handlers, "привет")
    assert "Answer in the user's language (en)" in agent.calls[0]["prompt"]


async def test_switching_the_language_during_the_answer_keeps_the_question_language(
    handlers, conn, hanging_agent
):
    """s186: "English" нажата в подсказке, пока Справка думает, - ответ всё
    равно на языке вопроса, а не на прежнем языке интерфейса."""
    _allow(conn)
    db.set_lang(conn, USER_ID, "ru")
    asked = asyncio.create_task(_ask(handlers, EN_QUESTION))
    await _settle()
    await handlers["language_callback"](
        make_update(USER_ID, callback_data="lang:en"), make_context()
    )
    hanging_agent.release.set()
    update = await asyncio.wait_for(asked, 2)
    assert (EN["REFERENCE_UNCLEAR"], _bad(handlers, 1)) in buttons(update)



# --- reference-language-and-grounding: облегчённый путь через Jev -------------

def _route(conn) -> list[str]:
    return [r["route"] for r in log_rows(conn, "reference_route")]


async def test_small_talk_is_answered_without_a_repository_search(
    handlers, conn, agent, fast_intent
):
    _allow(conn)
    fast_intent.track = "chat"
    agent.answers[:] = [AgentResult("Здравствуйте! Спросите про продукт.", files_read=[])]
    update = await _ask(handlers, "привет")
    assert [(c["context"], c["light"]) for c in agent.calls] == [
        (reference_mode.CHAT_CONTEXT, True)
    ]
    replies = update.effective_message.replies
    assert RU["STATUS_REFERENCE"] not in replies
    assert "Здравствуйте!" in _answers(update)
    assert RU["REFERENCE_UNVERIFIED"] not in _answers(update)
    assert buttons(update) == _rating_rows(handlers)
    assert _route(conn) == ["chat"]


async def test_a_product_question_returned_by_the_light_call_takes_the_full_path(
    handlers, conn, agent, fast_intent
):
    _allow(conn)
    fast_intent.track = "chat"
    agent.answers[:] = [
        AgentResult(reference_mode.NEED_SOURCES, files_read=[]),
        AgentResult("Пауза задаётся в кампании.", files_read=["a.py"]),
    ]
    update = await _ask(handlers, "как работает пауза между звонками в кампании?")
    assert [c["context"] for c in agent.calls] == [
        reference_mode.CHAT_CONTEXT, reference_mode.REFERENCE_CONTEXT
    ]
    assert RU["STATUS_REFERENCE"] in update.effective_message.replies
    assert "Пауза задаётся" in _answers(update)
    assert reference_mode.NEED_SOURCES not in _answers(update)
    assert _route(conn) == ["chat_fallback"]


async def test_without_the_classifier_small_talk_takes_the_full_path(handlers, conn, agent):
    """В тестовом конфиге ключа Jev нет - как на сервере без TYPESAFE_API_KEY."""
    _allow(conn)
    update = await _ask(handlers, "привет")
    assert [c["context"] for c in agent.calls] == [reference_mode.REFERENCE_CONTEXT]
    assert RU["STATUS_REFERENCE"] in update.effective_message.replies
    assert _route(conn) == ["full"]
