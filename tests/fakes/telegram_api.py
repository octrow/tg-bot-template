"""Фейковый Bot API: бот работает без изменений, вместо api.telegram.org - он.

Отдаёт апдейты через настоящий long polling `getUpdates`, пишет каждый вызов
бота в журнал и отвечает правдоподобными объектами Bot API. Ошибку на метод
можно заказать с настоящим HTTP-статусом: PTB выбирает исключение по статусу,
а не по `error_code` (design, решение 1)."""

import html
import itertools
import json
import re
import threading
import time
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import HTTP

from ._server import JsonServer

BOT_ID = 7000000001
BOT_USER = {
    "id": BOT_ID, "is_bot": True, "first_name": "Test Bot", "username": "tgbot_test_bot",
    "can_join_groups": True, "can_read_all_group_messages": False,
    "supports_inline_queries": False,
}
# Значения этих полей - всегда строки: "1" в ответе пользователя не число.
TEXT_KEYS = {"text", "caption", "parse_mode", "callback_query_id", "message_effect_id"}
SEND_METHODS = {"sendMessage", "sendDocument", "sendPhoto"}

# Тексты ошибок - как их отдаёт telegram-bot-api (Client.cpp).
CANT_PARSE = "Bad Request: can't parse entities: Unsupported start tag \"x\" at byte offset 0"
NOT_MODIFIED = (
    "Bad Request: message is not modified: specified new message content and reply markup "
    "are exactly the same as a current content and reply markup of the message"
)
EDIT_NOT_FOUND = "Bad Request: message to edit not found"


@dataclass
class Call:
    method: str
    params: dict
    at: float = field(default_factory=time.monotonic)
    result: object = None  # что фейк ответил: по нему тест жмёт кнопки


@dataclass
class _Failure:
    status: int
    description: str
    retry_after: int | None
    when: object  # предикат по params или None
    times: int


def _decode_value(key: str, raw: str):
    if key in TEXT_KEYS:
        return raw
    try:
        value = json.loads(raw)
    except ValueError:
        return raw
    # ponytail: строка, похожая на JSON-строку, остаётся сырой - JSON-кодирует
    # PTB только нестроковые значения.
    return raw if isinstance(value, str) else value


def _parse_body(headers, body: bytes) -> dict:
    ctype = headers.get("Content-Type", "")
    if ctype.startswith("multipart/"):
        message = BytesParser(policy=HTTP).parsebytes(
            f"Content-Type: {ctype}\r\n\r\n".encode() + body
        )
        params = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True) or b""
            filename = part.get_filename()
            if filename:
                params[name] = {"filename": filename, "size": len(payload), "content": payload}
            else:
                params[name] = _decode_value(name, payload.decode())
        return params
    if not body:
        return {}
    from urllib.parse import parse_qsl

    return {k: _decode_value(k, v) for k, v in parse_qsl(body.decode(), keep_blank_values=True)}


def _plain(text: str, parse_mode) -> str:
    """Что Telegram показал бы текстом: при HTML теги уходят в сущности."""
    if parse_mode == "HTML":
        return html.unescape(re.sub(r"<[^>]+>", "", text))
    return text


class FakeTelegram:
    def __init__(self):
        self._lock = threading.Condition()
        self._updates: list[dict] = []
        self._update_ids = itertools.count(1)
        self._message_ids = itertools.count(100)
        self._failures: dict[str, list[_Failure]] = {}
        # file_id -> байты: что бот скачает через `getFile` (скриншоты).
        self.files: dict[str, bytes] = {}
        self.calls: list[Call] = []
        self._released = False
        self._server = JsonServer(self._handle)

    # --- жизненный цикл -------------------------------------------------
    def start(self) -> "FakeTelegram":
        self._server.start()
        return self

    def release(self) -> None:
        """Висящий и последующие `getUpdates` отвечают сразу: остановка бота
        иначе ждёт конца long polling."""
        with self._lock:
            self._released = True
            self._lock.notify_all()

    def stop(self) -> None:
        self.release()
        self._server.stop()

    @property
    def base_url(self) -> str:
        return f"{self._server.url}/bot"

    # --- управление из теста -------------------------------------------
    def push_update(self, update: dict) -> int:
        """Кладёт апдейт в очередь `getUpdates`; возвращает его update_id."""
        with self._lock:
            update = {**update, "update_id": next(self._update_ids)}
            self._updates.append(update)
            self._lock.notify_all()
            return update["update_id"]

    def next_message_id(self) -> int:
        return next(self._message_ids)

    def fail(self, method, description, *, status=400, retry_after=None, when=None, times=1):
        """Следующие `times` вызовов `method` (подходящих под `when`) - ошибка."""
        with self._lock:
            self._failures.setdefault(method, []).append(
                _Failure(status, description, retry_after, when, times)
            )

    def calls_of(self, method: str, predicate=None) -> list[Call]:
        with self._lock:
            return [
                c for c in self.calls
                if c.method == method and (predicate is None or predicate(c.params))
            ]

    def mark(self) -> int:
        """Граница журнала: `wait_for(after=...)` смотрит только новее неё."""
        with self._lock:
            return len(self.calls)

    def wait_for(
        self, method: str, predicate=None, *, timeout: float = 5.0, count: int = 1,
        after: int = 0,
    ) -> list[Call]:
        """Ждёт `count` вызовов `method` под предикат после `after`.

        Блокирующий - из async-теста звать через `asyncio.to_thread`."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while True:
                found = [
                    c for c in self.calls[after:]
                    if c.method == method and (predicate is None or predicate(c.params))
                ]
                if len(found) >= count:
                    return found
                left = deadline - time.monotonic()
                if left <= 0:
                    seen = [(c.method, c.params.get("text", "")[:60]) for c in self.calls[after:]]
                    raise AssertionError(f"нет {count}x {method} за {timeout} с; были: {seen}")
                self._lock.wait(left)

    # --- сервер --------------------------------------------------------
    def _handle(self, verb, path, query, headers, body):
        download = re.fullmatch(r"/file/bot[^/]+/files/(.+)", path)
        if download:
            data = self.files.get(download.group(1))
            return (200, data) if data is not None else (404, {"ok": False})
        match = re.fullmatch(r"/bot[^/]+/(\w+)", path)
        if not match:
            return 404, {"ok": False, "error_code": 404, "description": "Not Found"}
        method = match.group(1)
        params = {**query, **_parse_body(headers, body)}
        if method == "getUpdates":
            return 200, {"ok": True, "result": self._get_updates(params)}
        with self._lock:
            call = Call(method, params)
            failure = self._take_failure(method, params)
            if failure is None:
                call.result = self._result(method, params)
            self.calls.append(call)
            self._lock.notify_all()
        if failure is not None:
            payload = {"ok": False, "error_code": failure.status, "description": failure.description}
            if failure.retry_after is not None:
                payload["parameters"] = {"retry_after": failure.retry_after}
            return failure.status, payload
        return 200, {"ok": True, "result": call.result}

    def _take_failure(self, method, params):
        for failure in self._failures.get(method, []):
            if failure.times > 0 and (failure.when is None or failure.when(params)):
                failure.times -= 1
                return failure
        return None

    def _get_updates(self, params) -> list[dict]:
        offset = int(params.get("offset") or 0)
        timeout = float(params.get("timeout") or 0)
        deadline = time.monotonic() + timeout
        with self._lock:
            # Подтверждённые (`offset`) больше не отдаются - как у Telegram.
            self._updates = [u for u in self._updates if u["update_id"] >= offset]
            while not self._updates and not self._released and time.monotonic() < deadline:
                self._lock.wait(deadline - time.monotonic())
            return list(self._updates)

    def _result(self, method, params):
        if method == "getMe":
            return BOT_USER
        if method in SEND_METHODS:
            return self._message(params, next(self._message_ids))
        if method in {"editMessageText", "editMessageReplyMarkup", "editMessageCaption"}:
            if "inline_message_id" in params:
                return True
            return self._message(params, int(params["message_id"]))
        if method == "getMyCommands":
            return []
        if method == "getFile":
            file_id = params["file_id"]
            return {
                "file_id": file_id, "file_unique_id": f"u-{file_id}",
                "file_size": len(self.files.get(file_id, b"")), "file_path": f"files/{file_id}",
            }
        return True

    def _message(self, params, message_id: int) -> dict:
        chat_id = int(params.get("chat_id", 0))
        message = {
            "message_id": message_id, "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "group"},
            "from": BOT_USER,
        }
        if "text" in params:
            message["text"] = _plain(params["text"], params.get("parse_mode"))
        if "caption" in params:
            message["caption"] = _plain(params["caption"], params.get("parse_mode"))
        if isinstance(params.get("document"), dict):
            message["document"] = {
                "file_id": f"doc{message_id}", "file_unique_id": f"u{message_id}",
                "file_name": params["document"]["filename"],
            }
        markup = params.get("reply_markup")
        # В Message Telegram возвращает только инлайн-клавиатуру.
        if isinstance(markup, dict) and "inline_keyboard" in markup:
            message["reply_markup"] = markup
        return message
