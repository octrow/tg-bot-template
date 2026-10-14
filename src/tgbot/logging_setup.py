"""Журнал процесса (stderr -> journald): уровень из окружения, контекст апдейта
в каждой строке и маска секретов.

Контекст - чей это апдейт: при `concurrent_updates(True)` строки разных
пользователей перемешаны, и без `u=`/`upd=` их не развести. Маска - последняя
линия: токен Bot API сидит в URL запросов, ключи LLM - в текстах ошибок, и
трейсбек унёс бы их в journald.

ponytail: только stderr - ротацию и хранение делает journald; файл с ротацией
добавлять, когда бот поедет туда, где systemd нет.
"""

import contextvars
import logging
import os
import re

LEVEL_ENV = "TGBOT_LOG_LEVEL"
DEFAULT_LEVEL = "INFO"
FORMAT = "%(asctime)s %(levelname)s %(name)s [%(ctx)s] %(message)s"
MASK = "***"
NO_CONTEXT = "-"

# Чей апдейт обрабатывается в текущей задаче. Задача на апдейт у PTB своя, и
# contextvar не протекает между пользователями.
update_context: contextvars.ContextVar[str] = contextvars.ContextVar(
    "update_context", default=NO_CONTEXT
)

# Чей апдейт - числом: исходящие вызовы и запросы к внешним сервисам находят по
# нему файл сессии (full-traffic-logging). Фоновые задачи копируют контекст.
current_user: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_user", default=None
)

# Узнаваемые по виду секреты - на случай, если значение не зарегистрировано:
# токен Bot API (`123456:AA...`) и ключи вида `sk-...` (OpenAI, OpenRouter).
_SECRET_SHAPES = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}|\bsk-[A-Za-z0-9_-]{16,}")
# Короче этого значение не маскируем: короткая строка совпала бы с обычным текстом.
_MIN_SECRET_LEN = 8
_secrets: set[str] = set()


def mask_secrets(*values: str | None) -> None:
    """Зарегистрировать точные значения секретов из конфига."""
    _secrets.update(v for v in values if v and len(v) >= _MIN_SECRET_LEN)


def redact(text: str) -> str:
    for secret in _secrets:
        text = text.replace(secret, MASK)
    return _SECRET_SHAPES.sub(MASK, text)


class Formatter(logging.Formatter):
    """Маска по ГОТОВОЙ строке: так она покрывает и аргументы, и трейсбек."""

    def format(self, record: logging.LogRecord) -> str:
        record.ctx = update_context.get()
        return redact(super().format(record))


def setup(env=os.environ) -> None:
    name = (env.get(LEVEL_ENV) or DEFAULT_LEVEL).upper()
    level = logging.getLevelName(name)
    handler = logging.StreamHandler()
    handler.setFormatter(Formatter(FORMAT))
    logging.basicConfig(
        level=level if isinstance(level, int) else logging.INFO,
        handlers=[handler],
    )
    if not isinstance(level, int):
        logging.getLogger(__name__).warning(
            "%s=%r - нет такого уровня, пишем %s", LEVEL_ENV, name, DEFAULT_LEVEL
        )
    # httpx печатает URL Bot API, а в нём токен - не пускаем его в логи.
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def bind_update(update, _context) -> None:
    """Первый обработчик каждого апдейта (группа -2): метка для всех строк,
    что напишет его обработка."""
    user = getattr(update, "effective_user", None)
    current_user.set(user.id if user else None)
    update_context.set(
        f"u={user.id if user else NO_CONTEXT} upd={getattr(update, 'update_id', NO_CONTEXT)}"
    )
