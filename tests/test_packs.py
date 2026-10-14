"""Пачка сообщений: сообщения подряд - один ответ
(спека message-packs).

Окно склейки - доли секунды, распознавание и Справка - заглушки.

Запуск: .venv/bin/python -m pytest tests/test_packs.py -q
"""

import asyncio
import dataclasses

import pytest

from tgbot.handlers import reference as reference_handlers
from tgbot import db, packs, reference_mode, screenshot_input, vision
from tgbot.agent.result import AgentResult
from tgbot.locales.ru import STRINGS as RU

from .conftest import make_handlers, ADMIN_ID, TEST_CONFIG, buttons, log_rows, make_context, make_update
from .test_screenshot_bot import photo_update

USER_ID = 42
WINDOW = 0.05
PACK_CONFIG = dataclasses.replace(TEST_CONFIG, text_debounce_s=WINDOW)
ANSWER = "Кампания добавляется в разделе Кампании."


@pytest.fixture
def handlers(access, conn, no_clone_check):
    db.add_user(conn, USER_ID, added_by=ADMIN_ID)
    return make_handlers(access, PACK_CONFIG)


@pytest.fixture
def agent(monkeypatch):
    """Справка: пишет промпты; `gate` (если задан) держит первый ответ."""
    state = dataclasses.make_dataclass("Agent", [("prompts", list), ("gate", object)])([], None)

    async def fake_call_agent(config, conn, prompt, user_id, context_label, **kwargs):
        if context_label == reference_mode.REFERENCE_CONTEXT:
            state.prompts.append(prompt)
            if state.gate is not None and len(state.prompts) == 1:
                await state.gate.wait()
        return AgentResult(ANSWER, files_read=["a.py"])

    monkeypatch.setattr(reference_handlers, "call_agent", fake_call_agent)
    return state


async def _text(handlers, text: str):
    update = make_update(USER_ID, text=text)
    await handlers["fallback"](update, make_context())
    return update


async def _until(check, timeout: float = 2.0) -> None:
    """Ждёт фоновое закрытие пачки, без угадывания задержек."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not check():
        assert loop.time() < deadline, "не дождались"
        await asyncio.sleep(0.01)


# --- 1. пачка -------------------------------------------------------------------

async def test_two_messages_in_the_window_get_one_answer(handlers, conn, agent):
    first = await _text(handlers, "как добавить кампанию?")
    second = await _text(handlers, "и где её найти потом")
    assert agent.prompts == []  # ещё копим
    await _until(lambda: agent.prompts)
    await asyncio.sleep(WINDOW * 3)
    assert len(agent.prompts) == 1
    assert "как добавить кампанию?\nи где её найти потом" in agent.prompts[0]
    assert first.effective_message.reactions and second.effective_message.reactions
    assert log_rows(conn, "pack_closed")[-1]["messages"] == 2


async def test_a_message_after_the_answer_starts_a_new_pack(handlers, agent):
    first = await _text(handlers, "как добавить кампанию?")
    await _until(lambda: any(ANSWER in r for r in first.effective_message.replies))
    await _text(handlers, "а удалить?")
    await _until(lambda: len(agent.prompts) == 2)
    assert "а удалить?" in agent.prompts[1]
    assert "как добавить кампанию?" in agent.prompts[1]  # прошлое - контекстом диалога


async def test_the_pack_waits_for_a_slow_screenshot(handlers, agent, monkeypatch):
    release = asyncio.Event()

    async def slow(image, fmt, **kwargs):
        await release.wait()
        return vision.Recognized("ЭКРАН: карточка звонка", "m", 1.0, None)

    monkeypatch.setattr(vision, "recognize", slow)
    await _text(handlers, "вот звонок")
    shot = asyncio.create_task(handlers["image"](photo_update(None), make_context()))
    await asyncio.sleep(WINDOW * 4)  # окно давно прошло, но скриншот ещё читается
    assert agent.prompts == []
    release.set()
    await shot
    await _until(lambda: agent.prompts)
    assert "вот звонок" in agent.prompts[0] and "карточка звонка" in agent.prompts[0]
    assert len(agent.prompts) == 1


# --- 3. поздние сообщения -----------------------------------------------------------

async def _late_offer(handlers, agent):
    """Ответ на первую пачку задержан, пока не пришло позднее сообщение."""
    agent.gate = asyncio.Event()
    first = await _text(handlers, "как добавить кампанию?")
    await _until(lambda: agent.prompts)
    late = await _text(handlers, "и почему она не звонит?")
    agent.gate.set()
    await _until(lambda: buttons(first) and buttons(first)[0][1].startswith(packs.REDO_PREFIX))
    return first, late


async def test_a_late_message_does_not_hold_the_answer(handlers, agent):
    first, late = await _late_offer(handlers, agent)
    replies = "\n".join(first.effective_message.replies)
    assert ANSWER in replies
    assert RU["PACK_LATE_NOTE"].format(k=1, m=1) in replies
    assert buttons(first)[0][0] == RU["PACK_REDO_BUTTON"]
    assert late.effective_message.reactions == []
    assert len(agent.prompts) == 1


async def test_redo_answers_by_the_pack_the_late_message_and_the_first_answer(
    handlers, agent, conn
):
    first, _late = await _late_offer(handlers, agent)
    click = make_update(USER_ID, callback_data=buttons(first)[0][1])
    await handlers["pack_callback"](click, make_context())
    assert len(agent.prompts) == 2
    prompt = agent.prompts[1]
    assert "как добавить кампанию?" in prompt and "и почему она не звонит?" in prompt
    assert ANSWER in prompt  # первый ответ - в контексте диалога
    assert handlers["dialogs"].get(USER_ID).attempt == reference_mode.FIRST_ATTEMPT
    assert log_rows(conn, "pack_redo")


async def test_without_the_button_the_late_message_joins_the_next_pack(handlers, agent):
    first, _late = await _late_offer(handlers, agent)
    await _text(handlers, "ещё вопрос")
    await _until(lambda: len(agent.prompts) == 2)
    assert "и почему она не звонит?\nещё вопрос" in agent.prompts[1]
    click = make_update(USER_ID, callback_data=buttons(first)[0][1])
    await handlers["pack_callback"](click, make_context())
    assert len(agent.prompts) == 2  # старая кнопка больше не пересчитывает


@pytest.mark.parametrize("echo_session, mode", [(False, "reference"), (True, "echo")])
async def test_a_late_message_is_logged_with_the_mode_of_the_session(conn, echo_session, mode):
    """Позднее сообщение в `/echo` - событие режима `echo`, а не Справки."""
    db.add_user(conn, USER_ID, added_by=ADMIN_ID)
    if echo_session:
        db.open_mode_session(conn, USER_ID, "echo")
    gate, answered = asyncio.Event(), []

    async def answer(update, context):
        answered.append(update)
        await gate.wait()

    pack = packs.Packs(conn, WINDOW, answer, lambda update: "ru")
    await pack.add(make_update(USER_ID, text="первое"), make_context())
    await _until(lambda: answered)
    await pack.add(make_update(USER_ID, text="позднее"), make_context())
    gate.set()
    await _until(lambda: pack._offers)  # ответ дописан, фоновых задач не осталось
    assert log_rows(conn, "pack_late")[-1]["mode"] == mode


# --- 5. команды -------------------------------------------------------------------

async def test_a_command_drops_the_open_pack_and_says_so(handlers, agent, conn):
    await _text(handlers, "как добавить кампанию?")
    command = make_update(USER_ID, text="/help")
    await handlers["command_reset"](command, make_context())
    assert command.effective_message.replies == [RU["PACK_DROPPED"]]
    await asyncio.sleep(WINDOW * 4)
    assert agent.prompts == []
    assert log_rows(conn, "pack_dropped")


# --- 2. лимит скриншотов на пачку -------------------------------------------------------

async def test_the_image_limit_is_per_pack(handlers, agent, monkeypatch):
    monkeypatch.setattr(screenshot_input, "ALBUM_QUIET_S", 0.01)
    calls: list[str] = []

    async def fake(image, fmt, **kwargs):
        calls.append(fmt)
        return vision.Recognized("экран", "m", 1.0, None)

    monkeypatch.setattr(vision, "recognize", fake)
    first = [photo_update("почему?" if i == 0 else None, group="a") for i in range(8)]
    await asyncio.gather(*(handlers["image"](p, make_context()) for p in first))
    second = [photo_update(None, group="b") for _ in range(4)]
    await asyncio.gather(*(handlers["image"](p, make_context()) for p in second))
    assert len(calls) == 10
    assert RU["IMAGE_FIRST_OF"].format(k=2, n=4) in second[0].effective_message.replies
    await _until(lambda: agent.prompts)
    assert len(agent.prompts) == 1


async def test_an_unread_screenshot_does_not_eat_the_pack_limit(handlers, agent, monkeypatch):
    async def fail(image, fmt, **kwargs):
        return vision.Recognized(None, "m", 1.0, "error")

    monkeypatch.setattr(vision, "recognize", fail)
    for _ in range(11):
        await handlers["image"](photo_update(None), make_context())
    shot = photo_update(None)
    await handlers["image"](shot, make_context())
    assert RU["IMAGE_PACK_FULL"].format(n=1) not in shot.effective_message.replies


async def test_a_packed_question_gets_one_reaction(handlers, agent):
    """session-log-completeness: пачка ставит реакцию при входе, Справка её не повторяет."""
    update = await _text(handlers, "как добавить кампанию?")
    await _until(lambda: any(ANSWER in r for r in update.effective_message.replies))
    assert len(update.effective_message.reactions) == 1


async def test_a_pack_opens_one_reference_session(handlers, conn, agent):
    """session-log-completeness: сессия открывается на входе первого сообщения
    и не открывается заново ни вторым сообщением пачки, ни ответом."""
    updates = []
    for text in ("как добавить кампанию?", "и где её найти потом"):
        update = make_update(USER_ID, text=text)
        update.to_dict = lambda: {}
        await handlers["log_update"](update, make_context())
        await handlers["fallback"](update, make_context())
        updates.append(update)
    await _until(lambda: any(
        ANSWER in r for u in updates for r in u.effective_message.replies
    ))
    statuses = [r["status"] for r in conn.execute("SELECT status FROM sessions")]
    assert statuses == [db.REFERENCE_STATUS]
