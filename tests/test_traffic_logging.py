"""full-traffic-logging: входящие апдейты и исходящие вызовы Bot API целиком."""

import json

import pytest
from telegram import Update

from tgbot import db

from .conftest import make_context


def _events(tmp_path, kind: str) -> list[dict]:
    out = []
    for path in (tmp_path / "logs").glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["kind"] == kind:
                out.append(record)
    return out


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(tmp_path / "logs"))
    return tmp_path


def _user(uid=501):
    return {"id": uid, "is_bot": False, "first_name": "Оля"}


def _message(text=None, chat_type="private", chat_id=501, uid=501, **extra):
    msg = {
        "message_id": 10, "date": 1_700_000_000,
        "chat": {"id": chat_id, "type": chat_type}, "from": _user(uid), **extra,
    }
    if text is not None:
        msg["text"] = text
    return msg


UPDATES = {
    "private_text": {"update_id": 1, "message": _message("куда делась задача?")},
    "button": {"update_id": 2, "callback_query": {
        "id": "q1", "from": _user(), "chat_instance": "ci", "data": "sum:ok",
        "message": _message("карточка"),
    }},
    "photo": {"update_id": 3, "message": _message(caption="вот экран", photo=[
        {"file_id": "F1", "file_unique_id": "U1", "width": 90, "height": 90, "file_size": 1234},
    ])},
    "group": {"update_id": 4, "message": _message("всем привет", chat_type="group", chat_id=-100)},
    "stranger": {"update_id": 5, "message": _message("пустите", chat_id=999, uid=999)},
}


@pytest.mark.parametrize("name", list(UPDATES))
async def test_every_update_is_logged_whole(handlers, logs, name):
    raw = UPDATES[name]
    update = Update.de_json(raw, None)

    await handlers["log_update"](update, make_context())

    [event] = _events(logs, "tg_update")
    assert event["payload"]["update"]["update_id"] == raw["update_id"]
    assert event["payload"]["chat_type"] == update.effective_chat.type
    assert event["user_id"] == update.effective_user.id
    text = json.dumps(event["payload"], ensure_ascii=False)
    for needle in ("куда делась задача?", "sum:ok", "вот экран", "F1", "всем привет", "пустите"):
        if needle in json.dumps(raw, ensure_ascii=False):
            assert needle in text


async def test_a_failing_log_does_not_break_the_update(handlers, logs, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "log_file_event", boom)
    update = Update.de_json(UPDATES["private_text"], None)

    await handlers["log_update"](update, make_context())  # не бросает


def test_binary_attachment_is_logged_by_name_and_size_only():
    from tgbot.utils import _file_entry

    entry = _file_entry(("shot.png", b"\x89PNG\x00\x01", "image/png"))

    assert entry == {"filename": "shot.png", "mime": "image/png", "size": 6}


def test_file_downloads_are_not_logged(conn, monkeypatch):
    from tgbot import utils

    seen = []
    monkeypatch.setattr(db, "log_file_event", lambda *a, **k: seen.append(a))
    utils.log_bot_call(conn, "https://api.telegram.org/file/bot123:ABC/photos/f.jpg", None,
                       code=200, body=b"\x89PNG")
    assert seen == []
