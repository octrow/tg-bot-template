import itertools
import json
from types import SimpleNamespace

import httpx
import pytest

from tgbot import db, modes, utils
from tgbot.access import Access
from tgbot.bot import build_deps, build_handlers
from tgbot.config import load_config
from tgbot.handlers.deps import DEPS_KEY

# Конфиг для тестов, которым нужен работающий агент. До удаления ступени Claude
# хендлеры собирались с `config=None`: ступень Claude ключа не требовала, и
# подменённая заглушка вызывалась и без конфига. Теперь ступень одна и ключ ей
# нужен, поэтому конфиг настоящий (`load_config`, а не SimpleNamespace - иначе
# у него не хватало бы полей, которые читают хендлеры). Кэш выключен,
# склейка сообщений тоже: с ними тест, отправивший один
# апдейт, возвращался бы раньше, чем бот обработал текст. Ровно эти значения и
# действовали при `config=None`, поэтому поведение тестов не меняется - меняется
# только то, что ключ ступени теперь есть.
TEST_CONFIG = load_config(
    {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "OPENROUTER_API_KEY": "sk-or-test",
        "TGBOT_AGENT_CACHE_TTL_S": "0",
        "TGBOT_TEXT_DEBOUNCE_S": "0",
    },
    env_files=[],
)
def pytest_configure(config):
    config.addinivalue_line("markers", "packs: стенд Bot API с окном склейки сообщений")


class FakeSent:
    """Сообщение-статус, возвращённое `reply_text`: помнит текст, чтобы тест
    мог проверить, что оно было удалено после ответа агента (7.4), и правки
    текста между этапами (`update_status`, задача 2.2) вместо второго
    сообщения."""

    def __init__(self, message: "FakeMessage", text: str):
        self._message = message
        self._text = text
        self.edited: list[str] = []  # тексты, на которые правили статус
        self.markups: list = []  # разметка правок (дозапись опоздавшей рекомендации)

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edited.append(text)
        self.markups.append(reply_markup)
        self._text = text

    async def delete(self, **kwargs):
        if self._message.fail_status_delete:
            raise RuntimeError("service messages cannot be deleted in this chat")
        self._message.deleted.append(self._text)


# Каждое новое сообщение получает свой message_id - как в Telegram. Тест,
# которому нужны два нажатия по ОДНОМУ сообщению, передаёт message_id явно.
_message_ids = itertools.count(1000)


def next_message_id() -> int:
    """Свободный message_id. Единственный источник на все тестовые модули:
    свой счётчик в каждом файле разъезжается с этим только по договорённости."""
    return next(_message_ids)


class FakeMessage:
    def __init__(self, text: str | None = None, date=None, message_id: int | None = None):
        self.text = text
        self.message_id = next(_message_ids) if message_id is None else message_id
        # Время отправки сообщения (как у telegram.Message): по нему бот
        # отличает опоздавший текст от ответа на текущий вопрос. None -
        # апдейт без времени, поведение "как раньше".
        self.date = date
        self.replies: list[str] = []
        # Шапка 'режим · этап' каждого ответа: её ставит
        # `SplittingBot`, мимо которого идёт FakeMessage, - тест видит этап.
        self.headers: list[str | None] = []
        # Отправленные сообщения (FakeSent) - через них тест видит правку уже
        # показанного вопроса: дозапись опоздавшей рекомендации.
        self.sent: list[FakeSent] = []
        self.markups: list = []
        self.parse_modes: list = []  # parse_mode каждого reply_text
        self.documents: list[tuple[bytes, str | None]] = []
        self.document_effects: list[str | None] = []
        self.document_captions: list[tuple[str | None, str | None]] = []
        self.edits: list[str] = []
        # Разметка каждой правки тела (см. `edit_text`): отдельно от `markups`,
        # который идёт в ногу с `replies` - тесты читают его по индексу ответа.
        self.edit_markups: list = []
        self.deleted: list[str] = []  # тексты статус-сообщений, удалённых delete()
        self.reactions: list = []  # эмодзи, переданные в set_reaction
        # Индикатор активности чата ("печатает"): им бот показывает, что
        # ожидание живо, и проверить это можно только запомнив вызовы.
        self.chat_actions: list[str] = []
        # Вызовы edit_reply_markup отдельно от markups (который смешивает их с
        # разметкой reply_text по тому же сообщению) - снятие клавиатуры
        # иначе неотличимо от разметки-по-умолчанию нового ответа.
        self.markup_edits: list = []
        # Тесты фолбэка Bot API (реакции/эффекты недоступны):
        self.fail_reaction = False
        self.fail_effect = False
        # Чат не позволяет удалить статус-сообщение (`sent.delete()` падает) -
        # `utils.clear_status` должен глотать это, не теряя ответ (сценарий
        # "чат не позволяет служебные сообщения").
        self.fail_status_delete = False

    async def edit_text(self, text, reply_markup=None, **kwargs):
        # `reply_markup` записывается, а НЕ глотается в `**kwargs`: у настоящего
        # `Message.edit_text` правка без разметки снимает клавиатуру целиком.
        # Пока двойник этого не моделировал, потеря кнопки была невидима для
        # тестов - нажатие уносило с собой кнопки под сообщением.
        self.edits.append(text)
        self.edit_markups.append(reply_markup)
        self.text = text

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)
        stage = modes.get()
        self.headers.append(modes.header("ru", stage) if stage else None)
        self.markups.append(reply_markup)
        # Форматирование - часть ответа: без parse_mode разметка уезжает в чат
        # спецзнаками, и тест этого иначе не увидит.
        self.parse_modes.append(kwargs.get("parse_mode"))
        sent = FakeSent(self, text)
        self.sent.append(sent)
        return sent

    async def reply_document(self, document, filename=None, **kwargs):
        effect = kwargs.get("message_effect_id")
        if effect and self.fail_effect:
            raise RuntimeError("message effect unavailable")
        self.documents.append((document, filename))
        self.document_effects.append(effect)
        # Подпись к файлу (clearer-bot-output).
        self.document_captions.append((kwargs.get("caption"), kwargs.get("parse_mode")))

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        # Перерисовка кнопок того же сообщения - без нового ответа в ленте.
        self.markups.append(reply_markup)
        self.markup_edits.append(reply_markup)

    async def set_reaction(self, reaction, **kwargs):
        if self.fail_reaction:
            raise RuntimeError("reactions disabled in this chat")
        self.reactions.append(reaction)

    async def reply_chat_action(self, action, **kwargs):
        self.chat_actions.append(action)


class FakeCallbackQuery:
    def __init__(self, data: str):
        self.data = data
        self.answered = False
        # Тексты тостов (answer(text=...)) - отклик без сообщения в ленте.
        self.toasts: list[str] = []

    async def answer(self, *args, **kwargs):
        self.answered = True
        text = kwargs.get("text") or (args[0] if args else None)
        if text:
            self.toasts.append(text)


def make_update(
    user_id: int,
    chat_type: str = "private",
    chat_id: int | None = None,
    text: str | None = None,
    callback_data: str | None = None,
    date=None,
    message_id: int | None = None,
    language_code: str | None = None,
):
    # `language_code` - как в профиле Telegram: по нему бот определяет язык при
    # первом контакте (спека localization). None - как у большинства тестов:
    # автоопределение даёт русский.
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, language_code=language_code),
        effective_chat=SimpleNamespace(id=chat_id if chat_id is not None else user_id, type=chat_type),
        effective_message=FakeMessage(text, date, message_id),
        callback_query=FakeCallbackQuery(callback_data) if callback_data else None,
    )


def buttons(update, index: int = -1) -> list[tuple[str, str]]:
    """(подпись, callback_data) кнопок последнего (или index-го) ответа бота."""
    markup = update.effective_message.markups[index]
    if markup is None:
        return []
    # У кнопки-ссылки callback_data пуст, а адрес лежит в url - отдаём то, что
    # у кнопки есть, иначе тест ссылки видит None и проверять ему нечего.
    return [
        (b.text, b.callback_data or getattr(b, "url", None))
        for row in markup.inline_keyboard
        for b in row
    ]


class StubBot:
    """Бот контекста по умолчанию: меню команд (`set_my_commands`) молча
    принимает - хендлеры зовут `context.bot` без проверки на None."""

    async def set_my_commands(self, commands, **kwargs):
        return True


def make_context(*args: str):
    return SimpleNamespace(args=list(args), bot=StubBot())


@pytest.fixture(autouse=True)
def _no_real_env_files(monkeypatch):
    """Файлы окружения в тестах не читаем.

    `load_config()` без аргументов добирает переменные из `./.env`,
    `/opt/tg-bot/.env` и `~/.env` (спека llm-provider-selection). В
    тестах это означало бы, что настоящий `.env` разработчика меняет их
    результат - и настоящие ключи попадали бы в тестовые вызовы. Тесты самого
    поиска по файлам передают `env_files=` явно и этой подмены не видят."""
    monkeypatch.setattr("tgbot.config.ENV_FILES", ())


LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Сети в тестах нет.

    У ступени теперь настоящий ключ в `TEST_CONFIG`, поэтому тест, забывший
    подменить `ask_openai`, уходил бы живым запросом в OpenRouter и получал 401
    (так и случилось с одним из тестов до этой фикстуры).
    Запрещаем именно выход в сеть, а не сам `httpx.AsyncClient`: тесты
    с `httpx.MockTransport` работают через него, и подменять весь клиент
    сломало бы их.
    """

    real = httpx.AsyncHTTPTransport.handle_async_request

    async def no_network(self, request):
        # Локальные фейки (tests/fakes) - не сеть: им отвечает этот же процесс.
        if request.url.host in LOOPBACK_HOSTS:
            return await real(self, request)
        raise AssertionError(
            f"тест пошёл в сеть ({request.url}) - подмените "
            "tgbot.agent.openrouter.ask_openai либо транспорт httpx"
        )

    monkeypatch.setattr("httpx.AsyncHTTPTransport.handle_async_request", no_network)
    # Фоновый поиск хостера полного вызова OpenRouter: без попыток он сразу
    # завершается, и тестам с подменённым клиентом не нужен `client.get`.
    monkeypatch.setattr("tgbot.agent.openrouter.UPSTREAM_LOOKUP_DELAYS_S", ())


@pytest.fixture(autouse=True)
def _logs_dir_isolated(tmp_path, monkeypatch):
    """Файловые логи (`log_event`-зеркало) не должны утекать в репозиторий -
    каждый тест получает свой каталог логов внутри собственного tmp_path."""
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(tmp_path / "logs"))


@pytest.fixture(autouse=True)
def _reactions_forgotten():
    """Память реакций (`utils.react`) живёт весь процесс, а фейковый Telegram
    в botapi-тестах нумерует сообщения заново: чужая реакция не должна
    глушить реакцию следующего теста."""
    utils._reacted.clear()
    utils._reacted_order.clear()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


ADMIN_ID = 100


@pytest.fixture
def access(conn):
    return Access(conn, [ADMIN_ID])


@pytest.fixture
def no_clone_check(monkeypatch):
    """Клон `REPO_URL` в тестах не трогаем.

    До удаления ступени Claude хендлеры собирались с `config=None`, и Справка
    вовсе не заходила в `ensure_repo_async`. Теперь конфиг настоящий, а значит
    Справка пошла бы проверять клон - в лучшем случае лишним предупреждением
    админу, в худшем реальным `git pull`/`git clone` из теста. Тесты самого
    клона (`test_repo.py`) вызывают `repo`-функции напрямую и этой подмены не
    видят."""

    async def clone_is_there(config, conn):
        return True

    monkeypatch.setattr("tgbot.repo.ensure_repo_async", clone_is_there)


def make_handlers(access, config=TEST_CONFIG) -> dict:
    """Обработчики приложения по именам, как их зовёт PTB: зависимости (`Deps`)
    лежат в `context.bot_data`. Фейковым контекстам тестов их подкладывает
    обёртка - у каждого набора свои (рестарт процесса = новый набор).
    `dialogs` - память Справки, для тестов истечения контекста."""
    deps = build_deps(access, config)

    def bind(handler):
        async def bound(update, context):
            if getattr(context, "bot_data", None) is None:
                context.bot_data = {}
            context.bot_data[DEPS_KEY] = deps
            return await handler(update, context)

        return bound

    handlers = {name: bind(handler) for name, handler in build_handlers().items()}
    handlers["dialogs"] = deps.dialogs
    return handlers


@pytest.fixture
def handlers(access, no_clone_check):
    return make_handlers(access)


def log_kinds(conn) -> list[str]:
    return [r["kind"] for r in conn.execute("SELECT kind FROM log ORDER BY id")]


def log_rows(conn, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT user_id, kind, payload, ts FROM log WHERE kind = ? ORDER BY id", (kind,)
    ).fetchall()
    return [
        {"user_id": r["user_id"], "ts": r["ts"], **json.loads(r["payload"])} for r in rows
    ]
