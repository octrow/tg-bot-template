"""Заявка на доступ (/request_access) - спека access-request."""

from types import SimpleNamespace

import pytest

from tgbot import db
from tgbot.access import Access
from tgbot.locales.ru import STRINGS as RU

from .conftest import make_handlers, TEST_CONFIG
from tests.conftest import ADMIN_ID, buttons, log_rows, make_update, next_message_id

REFUSAL = RU["REFUSAL"]
ACCESS_ALREADY = RU["ACCESS_ALREADY"]
ACCESS_DECIDED_ALREADY = RU["ACCESS_DECIDED_ALREADY"]
ACCESS_GRANTED = RU["ACCESS_GRANTED"]
ACCESS_PENDING = RU["ACCESS_PENDING"]
ACCESS_QUESTION = RU["ACCESS_QUESTION"]
ACCESS_REJECTED = RU["ACCESS_REJECTED"]
ACCESS_SENT = RU["ACCESS_SENT"]
ACCESS_SKIP_BUTTON = RU["ACCESS_SKIP_BUTTON"]

USER = 555
SECOND_ADMIN = 101


class FakeBot:
    """context.bot для пушей админам и уведомлений заявителю."""

    def __init__(self):
        self.sent: list[SimpleNamespace] = []
        self.edited: list[SimpleNamespace] = []
        self.fail_for: set[int] = set()

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if chat_id in self.fail_for:
            raise RuntimeError("chat blocked")
        message = SimpleNamespace(
            chat_id=chat_id,
            message_id=next_message_id(),
            text=text,
            reply_markup=reply_markup,
        )
        self.sent.append(message)
        return message

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, **kwargs):
        if chat_id in self.fail_for:
            raise RuntimeError("chat blocked")
        self.edited.append(
            SimpleNamespace(chat_id=chat_id, message_id=message_id, text=text)
        )


def make_ctx(bot: FakeBot | None = None, *args: str):
    return SimpleNamespace(args=list(args), bot=bot or FakeBot())


@pytest.fixture
def two_admins_access(conn):
    return Access(conn, [ADMIN_ID, SECOND_ADMIN])


@pytest.fixture
def two_admins_handlers(two_admins_access):
    return make_handlers(two_admins_access, TEST_CONFIG)


def requester(text=None, callback_data=None):
    update = make_update(USER, text=text, callback_data=callback_data)
    update.effective_user.username = "petya"
    update.effective_user.first_name = "Пётр"
    update.effective_user.last_name = "Иванов"
    update.effective_user.language_code = "ru"
    return update


async def submit(handlers, bot, text="Иванов Пётр, нужен доступ"):
    """Полный путь заявителя: /request_access + ответ текстом."""
    await handlers["request_access"](requester(text="/request_access"), make_ctx(bot))
    update = requester(text=text)
    await handlers["fallback"](update, make_ctx(bot))
    return update


# --- заявитель --------------------------------------------------------------


async def test_request_access_works_before_allowlist(handlers):
    update = requester(text="/request_access")
    await handlers["request_access"](update, make_ctx())
    assert update.effective_message.replies == [ACCESS_QUESTION]
    assert buttons(update)[0][0] == ACCESS_SKIP_BUTTON


async def test_allowed_user_gets_already_have_access(conn, handlers):
    db.add_user(conn, USER, ADMIN_ID)
    update = requester(text="/request_access")
    await handlers["request_access"](update, make_ctx())
    assert update.effective_message.replies == [ACCESS_ALREADY]
    assert db.open_access_request(conn, USER) is None


async def test_refusal_mentions_request_access():
    assert "/request_access" in REFUSAL


async def test_text_answer_submits_request(conn, handlers):
    bot = FakeBot()
    update = await submit(handlers, bot)
    assert update.effective_message.replies == [ACCESS_SENT]
    req = conn.execute("SELECT * FROM access_requests").fetchone()
    assert req["status"] == "pending"
    assert req["name_reason"] == "Иванов Пётр, нужен доступ"


async def test_skip_submits_without_text(conn, handlers):
    await handlers["request_access"](requester(text="/request_access"), make_ctx())
    req = db.open_access_request(conn, USER)
    bot = FakeBot()
    update = requester(callback_data=f"access:skip:{req['id']}")
    await handlers["access_callback"](update, make_ctx(bot))
    assert db.get_access_request(conn, req["id"])["status"] == "pending"
    assert db.get_access_request(conn, req["id"])["name_reason"] is None
    assert update.effective_message.replies == [ACCESS_SENT]


async def test_abandoned_draft_survives_next_message(conn, handlers):
    """Пользователь пропал после вопроса: заявка не подана, следующее
    сообщение продолжает оформление, а не получает отказ gate."""
    await handlers["request_access"](requester(text="/request_access"), make_ctx())
    assert db.open_access_request(conn, USER)["status"] == "draft"
    bot = FakeBot()
    update = requester(text="Иванов, доступ")
    await handlers["fallback"](update, make_ctx(bot))
    assert db.open_access_request(conn, USER)["status"] == "pending"
    assert REFUSAL not in update.effective_message.replies


async def test_one_active_request(conn, handlers):
    bot = FakeBot()
    await submit(handlers, bot)
    update = requester(text="/request_access")
    await handlers["request_access"](update, make_ctx())
    assert update.effective_message.replies == [ACCESS_PENDING]
    assert conn.execute("SELECT count(*) AS n FROM access_requests").fetchone()["n"] == 1


async def test_new_request_after_denial(conn, handlers):
    bot = FakeBot()
    await submit(handlers, bot)
    req = db.open_access_request(conn, USER)
    db.decide_access_request(conn, req["id"], ADMIN_ID, "denied")
    update = requester(text="/request_access")
    await handlers["request_access"](update, make_ctx())
    assert update.effective_message.replies == [ACCESS_QUESTION]


# --- пуш админам ------------------------------------------------------------


async def test_push_contains_autodata(conn, two_admins_handlers):
    db.log_event(conn, USER, "access_denied", {})  # история до заявки
    bot = FakeBot()
    await submit(two_admins_handlers, bot)
    assert sorted(m.chat_id for m in bot.sent) == [ADMIN_ID, SECOND_ADMIN]
    card = bot.sent[0].text
    for expected in (str(USER), "@petya", "Пётр Иванов", "ru", "Отказов в доступе: 1"):
        assert expected in card
    assert "Иванов Пётр, нужен доступ" in card
    req = db.open_access_request(conn, USER)
    assert len(req["admin_messages"]) == 2


async def test_push_failure_to_one_admin_keeps_others(conn, two_admins_handlers):
    bot = FakeBot()
    bot.fail_for.add(ADMIN_ID)
    await submit(two_admins_handlers, bot)
    assert [m.chat_id for m in bot.sent] == [SECOND_ADMIN]
    assert log_rows(conn, "access_push_failed")[0]["admin_id"] == ADMIN_ID
    assert db.open_access_request(conn, USER)["status"] == "pending"


# --- решение админа ---------------------------------------------------------


async def decide(handlers, bot, req_id, admin_id=ADMIN_ID, action="approve"):
    update = make_update(admin_id, callback_data=f"access:{action}:{req_id}")
    await handlers["access_callback"](update, make_ctx(bot))
    return update


async def test_approve_adds_to_allowlist_and_notifies(conn, handlers):
    bot = FakeBot()
    await submit(handlers, bot)
    req = db.open_access_request(conn, USER)
    await decide(handlers, bot, req["id"])
    assert db.is_allowed(conn, USER)
    assert db.get_access_request(conn, req["id"])["status"] == "approved"
    assert bot.sent[-1].chat_id == USER
    assert bot.sent[-1].text == ACCESS_GRANTED
    assert log_rows(conn, "access_request_decided")[0]["status"] == "approved"


async def test_deny_notifies_neutrally(conn, handlers):
    bot = FakeBot()
    await submit(handlers, bot)
    req = db.open_access_request(conn, USER)
    await decide(handlers, bot, req["id"], action="deny")
    assert not db.is_allowed(conn, USER)
    assert bot.sent[-1].text == ACCESS_REJECTED


async def test_decision_edits_all_admin_pushes(conn, two_admins_handlers):
    bot = FakeBot()
    await submit(two_admins_handlers, bot)
    req = db.open_access_request(conn, USER)
    await decide(two_admins_handlers, bot, req["id"])
    assert sorted(e.chat_id for e in bot.edited) == [ADMIN_ID, SECOND_ADMIN]
    assert all("Решил" in e.text and "одобрено" in e.text for e in bot.edited)


async def test_admin_race_second_gets_already_decided(conn, two_admins_handlers):
    bot = FakeBot()
    await submit(two_admins_handlers, bot)
    req = db.open_access_request(conn, USER)
    await decide(two_admins_handlers, bot, req["id"], ADMIN_ID, "approve")
    late = await decide(two_admins_handlers, bot, req["id"], SECOND_ADMIN, "deny")
    assert db.get_access_request(conn, req["id"])["status"] == "approved"
    assert db.is_allowed(conn, USER)
    assert late.callback_query.toasts == [ACCESS_DECIDED_ALREADY]


async def test_non_admin_cannot_decide(conn, handlers):
    bot = FakeBot()
    await submit(handlers, bot)
    req = db.open_access_request(conn, USER)
    await decide(handlers, bot, req["id"], admin_id=USER, action="approve")
    assert not db.is_allowed(conn, USER)
    assert db.get_access_request(conn, req["id"])["status"] == "pending"


async def test_pending_survives_restart(conn, handlers, access):
    """Рестарт = новые handlers на том же соединении: заявка остаётся
    активной, кнопки в старых пушах продолжают работать."""
    bot = FakeBot()
    await submit(handlers, bot)
    req = db.open_access_request(conn, USER)

    fresh_handlers = make_handlers(access, TEST_CONFIG)
    update = requester(text="/request_access")
    await fresh_handlers["request_access"](update, make_ctx())
    assert update.effective_message.replies == [ACCESS_PENDING]
    await decide(fresh_handlers, bot, req["id"])
    assert db.is_allowed(conn, USER)
