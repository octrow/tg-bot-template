from pathlib import Path
from types import SimpleNamespace

from telegram.error import Conflict

from tgbot.handlers.common import make_error_handler
from tgbot.locales.ru import STRINGS as RU

from .conftest import log_rows


def bot_failure_text(code: int) -> str:
    return f"{RU['FAILURE_BOT']}\n\n{RU['FAILURE_CODE'].format(code=code)}"


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.markups: list = []
        self.fail_send = False

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if self.fail_send:
            raise RuntimeError("Bot API недоступен")
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)


class FakeApplication:
    def __init__(self):
        self.stopped = False
        self.bot_data: dict = {}

    def stop_running(self):
        self.stopped = True


def make_error_context(error: Exception, bot: FakeBot, app: FakeApplication):
    return SimpleNamespace(error=error, bot=bot, application=app)


def make_failing_update(user_id: int, chat_id: int, update_id: int = 1):
    return SimpleNamespace(
        update_id=update_id,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id, type="private"),
    )


async def test_handler_error_is_logged_and_user_notified(conn):
    handler = make_error_handler(conn)
    bot = FakeBot()
    app = FakeApplication()
    update = make_failing_update(user_id=42, chat_id=42)
    error = RuntimeError("boom")

    await handler(update, make_error_context(error, bot, app))

    entries = log_rows(conn, "handler_error")
    assert len(entries) == 1
    assert entries[0]["user_id"] == 42
    assert entries[0]["chat_id"] == 42
    assert "boom" in entries[0]["error"]

    # Простой язык и код инцидента вместо текста исключения
    # (спека failure-messages): код - номер записи журнала.
    code = conn.execute("SELECT id FROM log WHERE kind = 'handler_error'").fetchone()["id"]
    assert bot.sent == [(42, bot_failure_text(code))]
    assert "boom" not in bot.sent[0][1]
    button = bot.markups[0].inline_keyboard[0][0]
    assert button.callback_data == f"fail:{code}"
    assert not app.stopped


async def test_handler_error_without_chat_does_not_raise(conn):
    """Исключение вне чата (update=None) - только лог, обработчик не падает."""
    handler = make_error_handler(conn)
    bot = FakeBot()
    app = FakeApplication()
    error = RuntimeError("no chat here")

    # Не должно бросить исключение наружу.
    await handler(None, make_error_context(error, bot, app))

    entries = log_rows(conn, "handler_error")
    assert len(entries) == 1
    assert entries[0]["user_id"] is None
    assert entries[0]["chat_id"] is None
    assert bot.sent == []


async def test_handler_error_send_failure_does_not_raise(conn):
    """Не удалось отправить сообщение пользователю - лог уже записан, не падаем."""
    handler = make_error_handler(conn)
    bot = FakeBot()
    bot.fail_send = True
    app = FakeApplication()
    update = make_failing_update(user_id=7, chat_id=7)
    error = RuntimeError("boom")

    await handler(update, make_error_context(error, bot, app))

    entries = log_rows(conn, "handler_error")
    assert len(entries) == 1
    assert bot.sent == []


async def test_conflict_error_logs_and_stops_application(conn):
    handler = make_error_handler(conn)
    bot = FakeBot()
    app = FakeApplication()
    error = Conflict("terminated by other getUpdates request")

    await handler(None, make_error_context(error, bot, app))

    assert app.stopped is True
    # Флаг для main(): конфликт - это сбой, процесс должен выйти с кодом != 0,
    # иначе systemd посчитает завершение штатным.
    assert app.bot_data["instance_conflict"] is True
    entries = log_rows(conn, "instance_conflict")
    assert len(entries) == 1
    assert bot.sent == []
    # Обычный handler_error на Conflict не пишется - у него своя ветка/событие.
    assert log_rows(conn, "handler_error") == []


async def test_a_transient_telegram_error_is_not_shown_as_a_bot_failure(conn):
    """Сетевой сбой или повторное нажатие ("query is too old") - не поломка
    бота: пользователю не шлём FAILURE_BOT, в журнал не пишем handler_error."""
    from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

    handler = make_error_handler(conn)
    bot = FakeBot()
    update = make_failing_update(user_id=42, chat_id=42)
    for error in (
        RetryAfter(3),
        BadRequest("Message is not modified"), BadRequest("Query is too old"),
    ):
        await handler(update, make_error_context(error, bot, FakeApplication()))
    # Сбой опроса без апдейта - тоже не поломка.
    for error in (TimedOut(), NetworkError("reset")):
        await handler(None, make_error_context(error, bot, FakeApplication()))
    assert bot.sent == []
    assert log_rows(conn, "handler_error") == []


async def test_a_network_error_inside_a_handler_is_reported_as_network_not_bot(conn):
    """Ответ не ушёл из-за сети: молчание читалось бы как зависший бот, но и
    "Сбой в самом боте" с кодом и алертом - ложь (код 8241, 06.10.2026:
    ConnectTimeout до api.telegram.org через минуту после рестарта)."""
    from telegram.error import TimedOut

    handler = make_error_handler(conn)
    bot = FakeBot()
    update = make_failing_update(user_id=42, chat_id=42)
    await handler(update, make_error_context(TimedOut(), bot, FakeApplication()))
    assert log_rows(conn, "handler_error") == []
    assert log_rows(conn, "telegram_network")
    assert len(bot.sent) == 1
    assert "Telegram" in bot.sent[0][1]


async def test_reminder_loop_is_cancelled_on_stop_and_not_awaited_by_ptb(conn, monkeypatch):
    """PTB 22 `Application.stop()` ждёт все задачи `create_task`, а цикл
    напоминаний бесконечен: systemd ждал бы до SIGKILL. Цикл живёт вне
    `create_task` и снимается в `post_stop`."""
    import asyncio

    from tgbot import bot as bot_mod
    from tgbot.config import Config

    async def no_menu(*args, **kwargs):
        return None

    monkeypatch.setattr(bot_mod, "set_menu", no_menu)
    config = Config(telegram_bot_token="123:abc", admin_user_ids=frozenset({1}), db_path=Path("x.db"))
    app = bot_mod.build_application(config, conn)
    ptb_tasks = []
    monkeypatch.setattr(
        type(app), "create_task", lambda self, coro, **kw: ptb_tasks.append(coro)
    )

    await app.post_init(app)
    task = app.bot_data[bot_mod.REMINDER_TASK_KEY]
    await asyncio.sleep(0)
    assert not task.done()
    assert ptb_tasks == []

    await asyncio.wait_for(app.post_stop(app), timeout=1)

    assert task.cancelled()
