"""Скриншот как вход бота: распознанный текст идёт тем же путём, что текст
(спека screenshot-input).

Модель распознавания - заглушка `vision.recognize`; Справка - заглушка
`call_agent`. Тесты проверяют приём, лимиты, альбом, сбои, журнал и маршрут.

Запуск: .venv/bin/python -m pytest tests/test_screenshot_bot.py -q
"""

import asyncio
from types import SimpleNamespace

import pytest

from tgbot.handlers import reference as reference_handlers
from tgbot import db, reference_mode, screenshot_input, vision
from tgbot.agent.result import AgentResult
from tgbot.locales.ru import STRINGS as RU

from .conftest import ADMIN_ID, FakeMessage, buttons, log_rows, make_context, make_update

USER_ID = 42
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
HEIC = b"\0\0\0\x18ftypheic" + b"\0" * 32
CALL_ID = "call-out-6a7eef9ec3b74c24"


class FakeFile:
    def __init__(self, data: bytes, size: int | None = None):
        self.data = data
        self.file_size = len(data) if size is None else size
        self.downloads = 0

    async def get_file(self):
        return self

    async def download_as_bytearray(self):
        self.downloads += 1
        return bytearray(self.data)


def photo_update(caption=None, data=PNG, *, group=None, user_id=USER_ID, document=False, size=None):
    update = make_update(user_id)
    message: FakeMessage = update.effective_message
    file = FakeFile(data, size)
    message.photo = () if document else (FakeFile(b"thumb"), file)
    message.document = file if document else None
    file.mime_type = "image/png"
    message.caption = caption
    message.media_group_id = group
    message.file = file
    return update


@pytest.fixture
def recognized(monkeypatch):
    """Заглушка распознавания: отвечает по очереди из `answers` (последний
    повторяется), пишет вызовы."""
    state = SimpleNamespace(calls=[], answers=[f"1. ТЕКСТ: {CALL_ID}\n3. ВЫДЕЛЕНО: нет"])

    async def fake(image, fmt, **kwargs):
        state.calls.append(fmt)
        text = state.answers[min(len(state.calls) - 1, len(state.answers) - 1)]
        return vision.Recognized(text, "google/gemini-3.1-flash-lite", 1.2,
                                 None if text else "error")

    monkeypatch.setattr(vision, "recognize", fake)
    return state


@pytest.fixture
def agent(monkeypatch):
    calls: list[dict] = []

    async def fake_call_agent(config, conn, prompt, user_id, context_label, **kwargs):
        calls.append({"prompt": prompt, "context": context_label})
        return AgentResult("На экране ошибка запуска.", files_read=["a.py"])

    monkeypatch.setattr(reference_handlers, "call_agent", fake_call_agent)
    return calls


@pytest.fixture(autouse=True)
def _fast_album(monkeypatch):
    monkeypatch.setattr(screenshot_input, "ALBUM_QUIET_S", 0.01)


def _allow(conn, user_id=USER_ID):
    db.add_user(conn, user_id, added_by=ADMIN_ID)


async def _send(handlers, update):
    await handlers["image"](update, make_context())
    return update


def _replies(update) -> str:
    return "\n".join(update.effective_message.replies)


def _reference_prompts(agent) -> list[str]:
    return [c["prompt"] for c in agent if c["context"] == reference_mode.REFERENCE_CONTEXT]


# --- 3.1 обёртка ----------------------------------------------------------------

async def test_the_wrapper_answers_into_the_original_message():
    original = FakeMessage("старый")
    wrapped = screenshot_input.Override(original, text="новый")
    await wrapped.reply_text("ответ")
    assert wrapped.text == "новый" and original.text == "старый"
    assert original.replies == ["ответ"]
    assert wrapped.message_id == original.message_id


def test_compose_marks_each_screenshot_and_uses_the_default_question():
    assert screenshot_input.compose("", ["a"], "ru") == (
        f"{RU['IMAGE_DEFAULT_QUESTION']}\n\n{RU['IMAGE_MARK']}\na"
    )
    two = screenshot_input.compose("почему?", ["a", "b"], "ru")
    assert RU["IMAGE_MARK_OF"].format(k=2, n=2) in two and two.startswith("почему?")


# --- 3.2 приём, доступ, формат, размер -------------------------------------------

async def test_a_photo_with_a_caption_reaches_reference_with_the_recognized_text(
    handlers, conn, agent, recognized
):
    _allow(conn)
    recognized.answers = ["1. ТЕКСТ: Start date is in the past"]
    update = await _send(handlers, photo_update("почему не запускается?"))
    prompt = _reference_prompts(agent)[0]
    assert "почему не запускается?" in prompt
    assert RU["IMAGE_MARK"] in prompt and "Start date is in the past" in prompt
    assert "ошибка запуска" in _replies(update)
    assert "typing" in update.effective_message.chat_actions


async def test_a_png_file_is_read_like_a_photo(handlers, conn, agent, recognized):
    _allow(conn)
    recognized.answers = ["1. ТЕКСТ: Hang up"]
    await _send(handlers, photo_update("что это?", document=True))
    assert recognized.calls == ["png"]
    assert _reference_prompts(agent)


async def test_a_stranger_photo_is_not_recognized(handlers, conn, agent, recognized):
    await _send(handlers, photo_update("почему?", user_id=777))
    assert recognized.calls == []


async def test_a_heic_file_is_refused_without_the_model(handlers, conn, agent, recognized):
    _allow(conn)
    update = await _send(handlers, photo_update(None, HEIC, document=True))
    assert recognized.calls == []
    assert RU["IMAGE_BAD_FORMAT"] in _replies(update)
    assert agent == []


async def test_a_big_file_is_refused_before_download(handlers, conn, agent, recognized):
    _allow(conn)
    update = photo_update(None, document=True, size=15 * 1024 * 1024)
    await _send(handlers, update)
    assert update.effective_message.file.downloads == 0
    assert RU["IMAGE_TOO_BIG"].format(mb=10) in _replies(update)


async def test_a_text_update_passes_to_the_fallback_untouched(handlers, conn, agent, recognized):
    _allow(conn)
    update = make_update(USER_ID, text="что такое нода?")
    await handlers["image"](update, make_context())
    assert recognized.calls == []
    assert "что такое нода?" in _reference_prompts(agent)[0]


@pytest.mark.parametrize("mime", ["audio/ogg", "text/plain", "application/zip"])
async def test_a_non_image_file_is_not_taken_for_an_image(handlers, conn, agent, recognized, mime):
    _allow(conn)
    update = photo_update(None, document=True)
    update.effective_message.document.mime_type = mime
    await _send(handlers, update)
    assert recognized.calls == []
    assert RU["IMAGE_BAD_FORMAT"] not in _replies(update)


async def test_a_png_named_file_without_a_type_is_read(handlers, conn, agent, recognized):
    _allow(conn)
    update = photo_update("что это?", document=True)
    update.effective_message.document.mime_type = None
    update.effective_message.document.file_name = "screen.PNG"
    await _send(handlers, update)
    assert recognized.calls == ["png"]


# --- 3.3 альбом ---------------------------------------------------------------------

async def test_an_album_of_two_is_one_question(handlers, conn, agent, recognized):
    _allow(conn)
    recognized.answers = ["экран один", "экран два"]
    first, second = photo_update("почему?", group="g1"), photo_update(None, group="g1")
    await asyncio.gather(_send(handlers, first), _send(handlers, second))
    prompts = _reference_prompts(agent)
    assert len(prompts) == 1
    assert "экран один" in prompts[0] and "экран два" in prompts[0]
    assert RU["IMAGE_MARK_OF"].format(k=2, n=2) in prompts[0]


async def test_an_album_of_five_reads_all_five(handlers, conn, agent, recognized):
    _allow(conn)
    parts = [photo_update("почему?" if i == 0 else None, group="g5") for i in range(5)]
    await asyncio.gather(*(_send(handlers, p) for p in parts))
    assert len(recognized.calls) == 5
    assert "Посмотрел первые" not in _replies(parts[0])


async def test_an_album_of_twelve_reads_ten_and_says_so(handlers, conn, agent, recognized):
    _allow(conn)
    parts = [photo_update("почему?" if i == 0 else None, group="g12") for i in range(12)]
    await asyncio.gather(*(_send(handlers, p) for p in parts))
    assert len(recognized.calls) == 10
    assert RU["IMAGE_FIRST_OF"].format(k=10, n=12) in _replies(parts[0])


# --- 3.4 сбои -------------------------------------------------------------------------

async def test_a_failure_without_a_caption_gets_an_incident_code(handlers, conn, agent, recognized):
    _allow(conn)
    recognized.answers = [None]
    update = await _send(handlers, photo_update(None))
    code = log_rows(conn, "image_failed")[0]
    text = _replies(update)
    assert RU["IMAGE_FAILED"] in text
    row_id = conn.execute("SELECT max(id) FROM log WHERE kind='image_failed'").fetchone()[0]
    assert RU["FAILURE_CODE"].format(code=row_id) in text
    assert (RU["FAILURE_REPORT_BUTTON"], f"fail:{row_id}") in buttons(update)
    assert code["reasons"] == ["error"]
    assert agent == []


async def test_the_failure_button_files_a_report(handlers, conn, agent, recognized):
    _allow(conn)
    recognized.answers = [None]
    await _send(handlers, photo_update(None))
    row_id = conn.execute("SELECT max(id) FROM log WHERE kind='image_failed'").fetchone()[0]
    click = make_update(USER_ID, callback_data=f"fail:{row_id}")
    await handlers["failure_callback"](click, make_context())
    assert RU["FAILURE_REPORTED"] in click.effective_message.replies


async def test_a_failure_with_a_caption_answers_by_the_caption(handlers, conn, agent, recognized):
    _allow(conn)
    recognized.answers = [None]
    update = await _send(handlers, photo_update("почему так?"))
    assert update.effective_message.replies[0] == RU["IMAGE_FAILED_WITH_TEXT"]
    prompt = _reference_prompts(agent)[0]
    assert "почему так?" in prompt and RU["IMAGE_MARK"] not in prompt


async def test_a_partly_read_album_says_how_many(handlers, conn, agent, recognized):
    _allow(conn)
    recognized.answers = ["экран один", None, "экран три"]
    parts = [photo_update("почему?" if i == 0 else None, group="g3") for i in range(3)]
    await asyncio.gather(*(_send(handlers, p) for p in parts))
    assert RU["IMAGE_READ_OF"].format(k=2, n=3) in _replies(parts[0])
    assert "экран три" in _reference_prompts(agent)[0]


# --- 3.5 журнал -----------------------------------------------------------------------

async def test_the_log_keeps_the_recognized_text_but_not_the_image(handlers, conn, agent, recognized):
    _allow(conn)
    await _send(handlers, photo_update("почему?"))
    row = log_rows(conn, "image_attached")[0]
    item = row["items"][0]
    assert item["format"] == "png" and item["model"] and item["seconds"] == 1.2
    assert CALL_ID in item["text"] and item["size"] == len(PNG)
    raw = conn.execute("SELECT payload FROM log").fetchall()
    assert not any("iVBOR" in r[0] or "\\u0089PNG" in r[0] for r in raw)


# --- 4. маршрут ------------------------------------------------------------------------

async def test_a_screenshot_without_a_caption_asks_the_default_question(
    handlers, conn, agent, recognized
):
    _allow(conn)
    recognized.answers = ["1. ТЕКСТ: Настроить колонки\n3. ВЫДЕЛЕНО: обведена кнопка"]
    await _send(handlers, photo_update(None))
    assert _reference_prompts(agent)[0].count(RU["IMAGE_DEFAULT_QUESTION"]) == 1


# --- после ревью: гонки альбома, размер после скачивания, сбой пометки -------------

async def test_a_late_album_part_does_not_start_a_second_answer(
    handlers, conn, agent, recognized, monkeypatch
):
    _allow(conn)
    recognized.answers = ["1. ТЕКСТ: Hang up"]
    await _send(handlers, photo_update("почему?", group="late"))
    await _send(handlers, photo_update(None, group="late"))
    assert len(_reference_prompts(agent)) == 1
    assert len(recognized.calls) == 1
    assert log_rows(conn, "image_late_part")


async def test_albums_of_two_users_with_the_same_id_do_not_mix(handlers, conn, agent, recognized):
    _allow(conn)
    _allow(conn, 43)
    recognized.answers = ["1. ТЕКСТ: Hang up"]
    await asyncio.gather(
        _send(handlers, photo_update("первый?", group="same")),
        _send(handlers, photo_update("второй?", group="same", user_id=43)),
    )
    prompts = _reference_prompts(agent)
    assert len(prompts) == 2
    assert not any(RU["IMAGE_MARK_OF"].format(k=2, n=2) in p for p in prompts)


async def test_a_photo_without_a_size_is_checked_after_download(handlers, conn, agent, recognized):
    _allow(conn)
    update = photo_update(None, PNG + b"\0" * (10 * 1024 * 1024))
    update.effective_message.file.file_size = None
    await _send(handlers, update)
    assert recognized.calls == []
    assert RU["IMAGE_TOO_BIG"].format(mb=10) in _replies(update)


async def test_a_failed_note_does_not_lose_the_answer(handlers, conn, agent, recognized):
    _allow(conn)
    recognized.answers = [None]
    update = photo_update("почему так?")
    message = update.effective_message
    real_reply = message.reply_text
    calls = {"n": 0}

    async def flaky(text, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("telegram blinked")
        return await real_reply(text, **kwargs)

    message.reply_text = flaky
    await _send(handlers, update)
    assert "почему так?" in _reference_prompts(agent)[0]
