"""Тесты user-feedback: /support, /feedback, кнопки и напоминание админам."""

from datetime import datetime, UTC
from zoneinfo import ZoneInfo

from tgbot import db, journal
from tgbot.handlers.support import (
    REMINDER_TZ,
    next_reminder_at,
    send_feedback_reminder,
)
from tgbot.locales.ru import STRINGS as RU

from .conftest import ADMIN_ID, buttons, log_rows, make_context, make_update

ADMIN_REFUSAL = RU["ADMIN_REFUSAL"]
REFUSAL = RU["REFUSAL"]
FEEDBACK_ALREADY_READ = RU["FEEDBACK_ALREADY_READ"]
FEEDBACK_LOG_BUTTON = RU["FEEDBACK_LOG_BUTTON"]
FEEDBACK_MARKED = RU["FEEDBACK_MARKED"]
FEEDBACK_NONE = RU["FEEDBACK_NONE"]
FEEDBACK_NOT_FOUND = RU["FEEDBACK_NOT_FOUND"]
FEEDBACK_READ_BUTTON = RU["FEEDBACK_READ_BUTTON"]
FEEDBACK_REMINDER = RU["FEEDBACK_REMINDER"]
LOGS_FILE_GONE = RU["LOGS_FILE_GONE"]
SUPPORT_SAVED = RU["SUPPORT_SAVED"]
SUPPORT_USAGE = RU["SUPPORT_USAGE"]

USER_ID = 42


def allow_user(conn, user_id=USER_ID):
    db.add_user(conn, user_id, added_by=ADMIN_ID)


def feedback_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM feedback ORDER BY id")]


# --- слой данных (задача 1.3) ---------------------------------------------


def test_add_feedback_with_and_without_session(conn):
    session = db.create_session(conn, USER_ID, "echo")
    with_session = db.add_feedback(conn, USER_ID, session["id"], "с сессией")
    without_session = db.add_feedback(conn, 43, None, "без сессии")

    rows = feedback_rows(conn)
    assert [r["id"] for r in rows] == [with_session, without_session]
    assert rows[0]["session_id"] == session["id"]
    assert rows[1]["session_id"] is None
    assert all(r["read_at"] is None and r["read_by"] is None for r in rows)


def test_mark_read_is_idempotent(conn):
    fid = db.add_feedback(conn, USER_ID, None, "текст")
    assert db.mark_feedback_read(conn, fid, ADMIN_ID) is True
    # Второй клик - того же или другого админа - ничего не перезаписывает.
    assert db.mark_feedback_read(conn, fid, ADMIN_ID + 1) is False
    row = db.get_feedback(conn, fid)
    assert row["read_by"] == ADMIN_ID
    assert row["read_at"]


def test_unread_and_recent_selections(conn):
    first = db.add_feedback(conn, USER_ID, None, "первый")
    second = db.add_feedback(conn, USER_ID, None, "второй")
    db.mark_feedback_read(conn, first, ADMIN_ID)

    assert [r["id"] for r in db.unread_feedback(conn)] == [second]
    assert db.unread_feedback_count(conn) == 1
    # recent - свежие первыми и с лимитом.
    assert [r["id"] for r in db.recent_feedback(conn)] == [second, first]
    assert [r["id"] for r in db.recent_feedback(conn, limit=1)] == [second]
    assert db.get_feedback(conn, 999) is None


# --- /support (задача 2.3) --------------------------------------------------


async def test_support_saves_with_last_session(conn, handlers):
    allow_user(conn)
    session = db.create_session(conn, USER_ID, "echo")
    update = make_update(USER_ID, text="/support бот завис")
    await handlers["support"](update, make_context("бот", "завис"))

    rows = feedback_rows(conn)
    assert len(rows) == 1
    assert rows[0]["text"] == "бот завис"
    assert rows[0]["session_id"] == session["id"]
    assert update.effective_message.replies == [SUPPORT_SAVED]
    assert log_rows(conn, "feedback_received")[0]["feedback_id"] == rows[0]["id"]


async def test_support_without_text_saves_nothing(conn, handlers):
    allow_user(conn)
    update = make_update(USER_ID, text="/support")
    await handlers["support"](update, make_context())
    assert feedback_rows(conn) == []
    assert update.effective_message.replies == [SUPPORT_USAGE]


async def test_support_without_sessions_saves_null_session(conn, handlers):
    allow_user(conn)
    update = make_update(USER_ID, text="/support нет сессий")
    await handlers["support"](update, make_context("нет", "сессий"))
    rows = feedback_rows(conn)
    assert rows[0]["session_id"] is None
    assert update.effective_message.replies == [SUPPORT_SAVED]


async def test_support_does_not_touch_an_open_session(conn, handlers):
    allow_user(conn)
    session = db.create_session(conn, USER_ID, "echo", data={"a": 1})
    await handlers["support"](
        make_update(USER_ID, text="/support жалоба"), make_context("жалоба")
    )
    after = db.get_session(conn, session["id"])
    assert after["status"] == "active"
    assert after["mode"] == "echo"
    assert after["data"] == {"a": 1}


async def test_support_refused_outside_allowlist(conn, handlers):
    update = make_update(77, text="/support чужой")
    await handlers["support"](update, make_context("чужой"))
    assert feedback_rows(conn) == []
    assert update.effective_message.replies == [REFUSAL]


# --- /feedback и кнопки (задача 3.5) ----------------------------------------


async def test_feedback_lists_unread_with_buttons(conn, handlers):
    session = db.create_session(conn, USER_ID, "echo")
    db.remember_user(conn, USER_ID, "reporter")
    first = db.add_feedback(conn, USER_ID, session["id"], "с сессией")
    second = db.add_feedback(conn, 43, None, "без сессии")
    read = db.add_feedback(conn, USER_ID, None, "прочитан")
    db.mark_feedback_read(conn, read, ADMIN_ID)

    update = make_update(ADMIN_ID)
    await handlers["feedback"](update, make_context())
    message = update.effective_message

    assert len(message.replies) == 2  # прочитанный не показан
    assert "с сессией" in message.replies[0]
    assert "@reporter" in message.replies[0]
    assert f"сессия {session['id']}" in message.replies[0]
    assert buttons(update, 0) == [
        (FEEDBACK_READ_BUTTON, f"fb_read:{first}"),
        (FEEDBACK_LOG_BUTTON, f"fb_log:{first}"),
    ]
    # Отзыв без сессии - без кнопки лога.
    assert buttons(update, 1) == [(FEEDBACK_READ_BUTTON, f"fb_read:{second}")]
    # Просмотр не пометил прочитанным.
    assert db.unread_feedback_count(conn) == 2


async def test_feedback_empty_and_non_admin(conn, handlers):
    update = make_update(ADMIN_ID)
    await handlers["feedback"](update, make_context())
    assert update.effective_message.replies == [FEEDBACK_NONE]

    allow_user(conn, 5)
    stranger = make_update(5)
    await handlers["feedback"](stranger, make_context())
    assert stranger.effective_message.replies == [ADMIN_REFUSAL]


async def test_feedback_all_shows_last_ten_with_status(conn, handlers):
    ids = [db.add_feedback(conn, USER_ID, None, f"отзыв {i}") for i in range(12)]
    db.mark_feedback_read(conn, ids[-1], ADMIN_ID)

    update = make_update(ADMIN_ID)
    await handlers["feedback"](update, make_context("all"))
    reply = update.effective_message.replies[0]

    assert "10 из 12" in reply
    assert "отзыв 11" in reply and "отзыв 2" in reply
    assert "отзыв 1\n" not in reply  # старше лимита
    assert "OK" in reply and RU["FEEDBACK_STATUS_UNREAD"] in reply


async def test_read_button_marks_and_keeps_log_button(conn, handlers):
    session = db.create_session(conn, USER_ID, "echo")
    fid = db.add_feedback(conn, USER_ID, session["id"], "текст")
    update = make_update(ADMIN_ID, callback_data=f"fb_read:{fid}")
    await handlers["feedback_callback"](update, make_context())

    assert db.get_feedback(conn, fid)["read_by"] == ADMIN_ID
    assert update.callback_query.toasts == [FEEDBACK_MARKED]
    # Разметка перерисована: осталась только кнопка лога.
    assert buttons(update) == [(FEEDBACK_LOG_BUTTON, f"fb_log:{fid}")]
    assert log_rows(conn, "feedback_read")[0]["feedback_id"] == fid


async def test_read_button_twice_says_already_read(conn, handlers):
    fid = db.add_feedback(conn, USER_ID, None, "текст")
    db.mark_feedback_read(conn, fid, ADMIN_ID)
    update = make_update(ADMIN_ID, callback_data=f"fb_read:{fid}")
    await handlers["feedback_callback"](update, make_context())
    assert update.callback_query.toasts == [FEEDBACK_ALREADY_READ]
    assert db.get_feedback(conn, fid)["read_by"] == ADMIN_ID


async def test_read_button_on_deleted_feedback(handlers):
    update = make_update(ADMIN_ID, callback_data="fb_read:999")
    await handlers["feedback_callback"](update, make_context())
    assert update.callback_query.toasts == [FEEDBACK_NOT_FOUND]


async def test_log_button_sends_session_log(conn, handlers):
    session = db.create_session(conn, USER_ID, "echo")
    db.log_event(conn, USER_ID, "answer_received", {"text": "привет"}, session["id"])
    fid = db.add_feedback(conn, USER_ID, session["id"], "текст")

    update = make_update(ADMIN_ID, callback_data=f"fb_log:{fid}")
    await handlers["feedback_callback"](update, make_context())

    documents = update.effective_message.documents
    assert len(documents) == 1
    content = documents[0][0].decode("utf-8")
    assert "answer_received" in content
    assert log_rows(conn, "log_file_sent")


async def test_log_button_when_file_rotated_away(conn, handlers):
    session = db.create_session(conn, USER_ID, "echo")
    fid = db.add_feedback(conn, USER_ID, session["id"], "текст")
    name = db.session_log_filename(conn, session["id"])
    assert name is not None
    path = journal.logs_dir() / name
    if path.is_file():
        path.unlink()

    update = make_update(ADMIN_ID, callback_data=f"fb_log:{fid}")
    await handlers["feedback_callback"](update, make_context())

    assert update.effective_message.documents == []
    assert update.effective_message.replies == [LOGS_FILE_GONE.format(name=name)]


async def test_feedback_callback_refused_for_non_admin(conn, handlers):
    allow_user(conn, 5)
    fid = db.add_feedback(conn, USER_ID, None, "текст")
    update = make_update(5, callback_data=f"fb_read:{fid}")
    await handlers["feedback_callback"](update, make_context())
    assert db.get_feedback(conn, fid)["read_at"] is None
    assert update.effective_message.replies == [ADMIN_REFUSAL]


# --- напоминание (задача 4.4) -----------------------------------------------


def almaty(*args):
    return datetime(*args, tzinfo=REMINDER_TZ)


def test_next_reminder_at():
    # Будний день до 15:00 - сегодня в 15:00 (среда 2026-08-19).
    assert next_reminder_at(almaty(2026, 8, 19, 9, 0)) == almaty(2026, 8, 19, 15, 0)
    # Будний день после 15:00 - завтра.
    assert next_reminder_at(almaty(2026, 8, 19, 16, 0)) == almaty(2026, 8, 20, 15, 0)
    # Пятница с 15:00 - понедельник (21.08.2026 - пятница).
    assert next_reminder_at(almaty(2026, 8, 21, 15, 0)) == almaty(2026, 8, 24, 15, 0)
    # Суббота - понедельник, даже утром.
    assert next_reminder_at(almaty(2026, 8, 22, 9, 0)) == almaty(2026, 8, 24, 15, 0)
    # UTC на входе тоже работает (04:00 UTC = 09:00 Алма-Ата).
    utc_morning = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)
    assert next_reminder_at(utc_morning) == almaty(2026, 8, 19, 15, 0)



def test_next_reminder_at_takes_zone_and_hour():
    """Пояс и час - из настроек: 9:00 по Берлину, среда 2026-08-19."""
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 8, 19, 8, 0, tzinfo=berlin)
    assert next_reminder_at(now, tz=berlin, hour=9) == datetime(2026, 8, 19, 9, 0, tzinfo=berlin)
    assert next_reminder_at(now.replace(hour=10), tz=berlin, hour=9) == datetime(
        2026, 8, 20, 9, 0, tzinfo=berlin
    )


async def test_post_init_starts_the_reminder_with_configured_zone_and_hour(conn, monkeypatch):
    from types import SimpleNamespace

    from tgbot import bot as bot_mod

    started = []

    async def fake_loop(bot, conn, admin_user_ids, *, tz, hour):
        started.append((tz, hour))

    monkeypatch.setattr(bot_mod, "feedback_reminder_loop", fake_loop)
    monkeypatch.setattr(bot_mod, "set_menu", _no_menu)
    app = SimpleNamespace(bot=None, bot_data={})
    berlin = ZoneInfo("Europe/Berlin")
    await bot_mod.make_post_init([ADMIN_ID], conn, reminder_tz=berlin, reminder_hour=9)(app)
    await app.bot_data[bot_mod.REMINDER_TASK_KEY]
    assert started == [(berlin, 9)]


async def _no_menu(*args, **kwargs) -> None:
    return None

class FakeBot:
    def __init__(self, fail_for: set[int] | None = None):
        self.sent: list[tuple[int, str]] = []
        self.fail_for = fail_for or set()

    async def send_message(self, chat_id, text):
        if chat_id in self.fail_for:
            raise RuntimeError("chat unavailable")
        self.sent.append((chat_id, text))


async def test_reminder_sends_to_all_admins_and_logs(conn):
    db.add_feedback(conn, USER_ID, None, "раз")
    db.add_feedback(conn, USER_ID, None, "два")
    bot = FakeBot()
    await send_feedback_reminder(bot, conn, [ADMIN_ID, 200])
    assert bot.sent == [
        (ADMIN_ID, FEEDBACK_REMINDER.format(n=2)),
        (200, FEEDBACK_REMINDER.format(n=2)),
    ]
    entries = log_rows(conn, "feedback_reminder")
    assert entries[0]["unread"] == 2
    assert entries[0]["admins"] == [ADMIN_ID, 200]


async def test_reminder_silent_when_all_read(conn):
    fid = db.add_feedback(conn, USER_ID, None, "раз")
    db.mark_feedback_read(conn, fid, ADMIN_ID)
    bot = FakeBot()
    await send_feedback_reminder(bot, conn, [ADMIN_ID])
    assert bot.sent == []
    assert log_rows(conn, "feedback_reminder") == []


async def test_reminder_survives_delivery_failure(conn):
    db.add_feedback(conn, USER_ID, None, "раз")
    bot = FakeBot(fail_for={ADMIN_ID})
    await send_feedback_reminder(bot, conn, [ADMIN_ID, 200])
    assert bot.sent == [(200, FEEDBACK_REMINDER.format(n=1))]
    assert log_rows(conn, "feedback_reminder")[0]["admins"] == [200]
