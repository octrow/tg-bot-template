"""Слой Bot API: настоящий `build_application` против фейкового Telegram.

Бот собирается тем же кодом, что в проде, и ходит по HTTP в `tests/fakes`.
Порядок запуска повторяет `run_polling` (design, решение 2): `post_init`
вручную и ДО `start()` - иначе бесконечный цикл напоминаний попадает в задачи
приложения, и `stop()` ждёт его вечно."""

import asyncio
import itertools
import time
import warnings

import pytest

from tgbot import alerts, db
from tgbot.agent import tools
from tgbot.bot import build_application
from tgbot.config import load_config

from ..fakes.llm_api import FakeLLM
from ..fakes.telegram_api import FakeTelegram

TOKEN = "123456:TEST"
ADMIN = 1001
ALLOWED = 1002
STRANGER = 1003
GROUP_CHAT = -5001
WAIT_S = 8.0

_ids = itertools.count(1)


def user(uid: int, lang: str = "ru") -> dict:
    return {"id": uid, "is_bot": False, "first_name": f"U{uid}", "language_code": lang}


class BotHarness:
    """Ручки теста: писать боту от имени пользователя и ждать его вызовов."""

    def __init__(self, tg: FakeTelegram, llm: FakeLLM, conn, app):
        self.tg, self.llm, self.conn, self.app = tg, llm, conn, app
        self.clicked: list[str] = []  # id нажатий: каждое обязано получить ответ

    # --- апдейты --------------------------------------------------------
    def send(self, uid: int, text: str, *, chat_id: int | None = None, lang: str = "ru") -> int:
        """Сообщение пользователя; возвращает границу журнала до него."""
        chat_id = uid if chat_id is None else chat_id
        message = {
            "message_id": self.tg.next_message_id(), "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "group"},
            "from": user(uid, lang), "text": text,
        }
        if text.startswith("/"):
            command = text.split()[0]
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        mark = self.tg.mark()
        self.tg.push_update({"message": message})
        return mark

    def click(self, uid: int, message: dict, data: str, *, lang: str = "ru") -> int:
        """Нажатие инлайн-кнопки `data` под сообщением бота `message`."""
        mark = self.tg.mark()
        query_id = str(next(_ids))
        self.clicked.append(query_id)
        self.tg.push_update({"callback_query": {
            "id": query_id, "from": user(uid, lang), "chat_instance": "ci",
            "data": data, "message": message,
        }})
        return mark

    # --- ожидание -------------------------------------------------------
    async def wait(self, method: str, *, chat: int | None = None, after: int = 0,
                   contains: str | None = None, timeout: float = WAIT_S, count: int = 1):
        def match(params):
            if chat is not None and params.get("chat_id") != chat:
                return False
            return contains is None or contains in str(params.get("text", ""))

        calls = await asyncio.to_thread(
            self.tg.wait_for, method, match, timeout=timeout, count=count, after=after
        )
        return calls[-1] if count == 1 else calls

    async def ask(self, uid: int, text: str, *, contains: str | None = None, **kw):
        """Сообщение и первый ответ бота этому пользователю."""
        mark = self.send(uid, text, **kw)
        return await self.wait("sendMessage", chat=kw.get("chat_id", uid), after=mark,
                               contains=contains)

    async def press(self, uid: int, call, data_prefix: str, *, method: str = "sendMessage",
                    contains: str | None = None):
        """Жмёт кнопку с `callback_data`, начинающейся на `data_prefix`, под
        сообщением из `call`; ждёт `method` в этот чат."""
        data = button(call, data_prefix)
        mark = self.click(uid, call.result, data)
        return await self.wait(method, chat=call.result["chat"]["id"], after=mark,
                               contains=contains)

    async def silent(self, uid: int, after: int, *, settle: float = 0.5) -> None:
        """Бот этому пользователю ничего не прислал за `settle` секунд."""
        await asyncio.sleep(settle)
        sent = [c for c in self.tg.calls[after:]
                if c.method.startswith(("send", "edit")) and c.params.get("chat_id") == uid]
        assert not sent, [c.params.get("text") for c in sent]


def buttons(call) -> list[tuple[str, str]]:
    markup = call.params.get("reply_markup") or {}
    return [(b["text"], b.get("callback_data") or b.get("url", ""))
            for row in markup.get("inline_keyboard", []) for b in row]


def button(call, data_prefix: str) -> str:
    for _, data in buttons(call):
        if data.startswith(data_prefix):
            return data
    raise AssertionError(f"нет кнопки {data_prefix!r}: {buttons(call)}")


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    tg, llm = FakeTelegram().start(), FakeLLM().start()
    yield tg, llm
    for fake in (tg, llm):
        fake.stop()


def layer_config(tg, llm, **over):
    env = {
        "TELEGRAM_BOT_TOKEN": TOKEN,
        "TELEGRAM_BOT_API_BASE_URL": tg.base_url,
        "TGBOT_LLM_BASE_URL": llm.base_url,
        "OPENROUTER_API_KEY": "sk-or-fake",
        "ADMIN_USER_IDS": str(ADMIN),
        "TGBOT_TEXT_DEBOUNCE_S": "0",
        "TGBOT_AGENT_CACHE_TTL_S": "0",
        **over,
    }
    return load_config(env, env_files=[])


@pytest.fixture
async def bot(request, fakes, tmp_path):
    tg, llm = fakes
    conn = db.connect(tmp_path / "bot.db")
    db.add_user(conn, ALLOWED, ADMIN)
    over = {}
    # `@pytest.mark.packs` - с окном склейки: сообщения подряд - одна пачка (спека message-packs).
    if request.node.get_closest_marker("packs"):
        over["TGBOT_TEXT_DEBOUNCE_S"] = "0.3"
    app = build_application(layer_config(tg, llm, **over), conn)
    await app.initialize()
    with warnings.catch_warnings():
        # Ожидаемо: так же цикл напоминаний создаётся и в `run_polling`.
        warnings.filterwarnings("ignore", "Tasks created via `Application.create_task`")
        await app.post_init(app)
    await app.start()
    await app.updater.start_polling(timeout=1, poll_interval=0)
    harness = BotHarness(tg, llm, conn, app)
    yield harness
    # BotFather шлёт алерт "Too few answers to callback queries": кнопка без
    # answerCallbackQuery - это ещё и вечные часики у пользователя.
    unanswered = await _unanswered(harness)
    tg.release()
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    # Голые create_task бота (цикл напоминаний, пуши админам) `stop()` не ждёт - гасим их, чтобы не протекали в соседа.
    current = asyncio.current_task()
    leftovers = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    for task in leftovers:
        task.cancel()
    await asyncio.gather(*leftovers, return_exceptions=True)
    alerts.reset()
    tools.reset_overview()
    conn.close()
    assert not unanswered, f"нажатия без answerCallbackQuery: {unanswered}"


async def _unanswered(harness: BotHarness, timeout: float = 3.0) -> list[str]:
    deadline = time.monotonic() + timeout
    while True:
        answered = {c.params.get("callback_query_id")
                    for c in harness.tg.calls_of("answerCallbackQuery")}
        missing = [q for q in harness.clicked if q not in answered]
        if not missing or time.monotonic() > deadline:
            return missing
        await asyncio.sleep(0.1)
