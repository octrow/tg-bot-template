import pytest

from tgbot import db
from tgbot.locales.ru import STRINGS as RU

from .conftest import ADMIN_ID, log_kinds, make_context, make_update

REFUSAL = RU["REFUSAL"]


async def test_allowlisted_user_passes(conn, handlers):
    db.add_user(conn, 5, added_by=ADMIN_ID)
    # Именно текст: сообщение без текста бот сознательно оставляет без ответа.
    update = make_update(5, text="привет")
    await handlers["fallback"](update, make_context())
    assert update.effective_message.replies
    assert REFUSAL not in update.effective_message.replies


async def test_admin_is_allowed_without_db_record(conn, handlers):
    update = make_update(ADMIN_ID)
    await handlers["fallback"](update, make_context())
    assert REFUSAL not in update.effective_message.replies


async def test_stranger_is_refused_exactly_once(conn, handlers):
    first = make_update(7)
    await handlers["fallback"](first, make_context())
    assert first.effective_message.replies == [REFUSAL]

    for _ in range(3):
        again = make_update(7)
        await handlers["fallback"](again, make_context())
        assert again.effective_message.replies == []

    assert log_kinds(conn).count("access_denied") == 4


async def test_removed_user_is_refused(conn, handlers):
    db.add_user(conn, 8, added_by=ADMIN_ID)
    db.remove_user(conn, 8)
    update = make_update(8)
    await handlers["fallback"](update, make_context())
    assert update.effective_message.replies == [REFUSAL]


async def test_whoami_before_access_check(conn, handlers):
    update = make_update(999, chat_id=555)
    await handlers["whoami"](update, make_context())
    assert update.effective_message.replies == ["user_id: 999\nchat_id: 555"]
    assert not db.is_allowed(conn, 999)


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_group_messages_ignored(conn, handlers, chat_type):
    db.add_user(conn, ADMIN_ID + 1, added_by=ADMIN_ID)
    for name in ("fallback", "whoami", "allow", "users"):
        update = make_update(ADMIN_ID, chat_type=chat_type, chat_id=-100)
        await handlers[name](update, make_context("42"))
        assert update.effective_message.replies == []
    assert not db.is_allowed(conn, 42)


async def test_start_replies_greeting(conn, handlers):
    db.add_user(conn, 5, added_by=ADMIN_ID)
    update = make_update(5, text="/start")
    await handlers["start"](update, make_context())
    assert "/help" in update.effective_message.replies[0]
