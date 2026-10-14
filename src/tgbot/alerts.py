"""Пуш админам о сбое (спека failure-messages).

Три рода вызывают пуш: таймаут жёсткого порога, неисправленный брак и
необработанная ошибка. Не больше одного пуша на род за `WINDOW_S`; пропущенные
считаются и досылаются строкой "и ещё N таких же" в следующем пуше рода.
Отправка - фоновой задачей: ответ пользователю пуша не ждёт.
"""

import asyncio
import contextvars
import logging
import time
from datetime import datetime, UTC

from . import i18n

log = logging.getLogger(__name__)

WINDOW_S = 600.0
QUESTION_CHARS = 200
KIND_TIMEOUT = "timeout"
KIND_REJECTED = "rejected"
KIND_HANDLER_ERROR = "handler_error"
PUSH_KINDS = frozenset({KIND_TIMEOUT, KIND_REJECTED, KIND_HANDLER_ERROR})

clock = time.monotonic  # подменяется в тестах: окно проверяется без ожидания

# ponytail: окно в памяти процесса - после перезапуска обнуляется (один лишний
# пуш в худшем случае); перенести в `log`, если перезапуски станут частыми.
_window: dict[str, list] = {}  # род -> [время последнего пуша, пропущено]
_target: dict = {}  # bot, admins, conn - задаёт `configure` при старте
_tasks: set[asyncio.Task] = set()


def configure(bot, admin_ids, conn=None) -> None:
    _target.update(bot=bot, admins=tuple(admin_ids), conn=conn)


def reset() -> None:
    _window.clear()
    _target.clear()


def notify(kind: str, code: int, *, mode: str = "", model: str = "", question: str = "") -> None:
    """Пуш о сбое, если род его заслуживает и окно рода свободно."""
    if kind not in PUSH_KINDS or not _target.get("admins"):
        return
    now = clock()
    last = _window.get(kind)
    if last is not None and now - last[0] < WINDOW_S:
        last[1] += 1
        return
    skipped = last[1] if last is not None else 0
    _window[kind] = [now, 0]
    fields = dict(
        kind=kind, code=code, mode=mode or "-", model=model or "-",
        time=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        question=(question or "-")[:QUESTION_CHARS],
    )
    # Пустой контекст: шапка режима и язык пользователя (`modes`) - не для
    # пуша админу.
    task = asyncio.create_task(_send(fields, skipped), context=contextvars.Context())
    _tasks.add(task)  # держим ссылку: иначе GC соберёт задачу на полпути
    task.add_done_callback(_tasks.discard)


async def _send(fields: dict, skipped: int) -> None:
    bot, conn = _target["bot"], _target.get("conn")
    for admin_id in _target["admins"]:
        lang = i18n.lang_for(conn, admin_id) if conn is not None else i18n.DEFAULT_LANG
        text = i18n.t(lang, "FAILURE_PUSH", **fields)
        if skipped:
            text += i18n.t(lang, "FAILURE_PUSH_MORE", n=skipped)
        try:
            await bot.send_message(admin_id, text)
        except Exception:  # noqa: BLE001 - один недоступный админ не мешает остальным
            log.warning("Пуш о сбое админу %s не ушёл", admin_id, exc_info=True)
