"""Пачка сообщений (спека message-packs, термин - CONTEXT.md).

Сообщения подряд собираются в одну пачку и получают один ответ.
Пачка закрывается окном тишины (`text_debounce_s`) и не закрывается, пока у
пользователя распознаётся скриншот. Сообщение, пришедшее, пока ответ на пачку
готовится, - позднее: ответ его не ждёт, под ответом уходит пометка с кнопкой
пересчёта, а без нажатия позднее входит в следующую пачку.

ponytail: всё в памяти процесса - перезапуск в окне склейки теряет открытую
пачку (как альбом в screenshot_input). Таблица в БД - если потери всплывут.
"""

import asyncio
import contextlib
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from . import db, i18n, utils
from .modes import Mode

logger = logging.getLogger(__name__)

# Сколько картинок распознаётся в одной пачке (spec screenshot-input).
IMAGES_MAX = 10
# Как часто закрытие пачки перепроверяет, не кончилось ли распознавание.
RECOGNIZING_POLL_S = 0.2
REDO_PREFIX = "pack:redo:"

# Идущие распознавания по пользователю. Модульное, а не поле `Packs`: его
# читает и код вне пачки, у которого своего доступа к пачкам нет.
_recognizing: dict[int, int] = {}


def busy(user_id: int) -> bool:
    """Идёт ли у пользователя распознавание скриншота."""
    return _recognizing.get(user_id, 0) > 0


@contextlib.contextmanager
def recognizing(user_id: int):
    _recognizing[user_id] = _recognizing.get(user_id, 0) + 1
    try:
        yield
    finally:
        left = _recognizing[user_id] - 1
        if left:
            _recognizing[user_id] = left
        else:
            del _recognizing[user_id]


async def wait_idle(user_id: int) -> None:
    """Ждёт конца распознавания; его потолок держит сам vision (60 с)."""
    while busy(user_id):
        await asyncio.sleep(RECOGNIZING_POLL_S)


def text_of(update) -> str:
    return (getattr(update.effective_message, "text", None) or "").strip()


@dataclass
class _Pack:
    updates: list = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    images: int = 0
    task: asyncio.Task | None = None
    context: object = None
    started: float = field(default_factory=time.monotonic)


@dataclass
class _Offer:
    """Поздние сообщения под отправленным ответом и кнопка их пересчёта."""
    token: int
    update: object
    pack: list[str]
    late: list[str]


class Packs:
    def __init__(
        self, conn, window: float,
        answer: Callable[[object, object], Awaitable[None]],
        lang_of: Callable[[object], str],
    ):
        self.conn = conn
        self.window = window
        self._answer = answer  # общий маршрут текста бота (`fallback`)
        self._lang_of = lang_of
        self._open: dict[int, _Pack] = {}
        self._answering: dict[int, list[str]] = {}  # user -> поздние сообщения
        self._images: dict[int, int] = {}  # картинок в пачке, включая идущие
        self._offers: dict[int, _Offer] = {}
        self._tokens = itertools.count(1)

    def take_images(self, user_id: int, wanted: int) -> int:
        """Сколько из `wanted` картинок ещё можно распознать в этой пачке."""
        used = self._images.get(user_id, 0)
        taken = max(0, min(wanted, IMAGES_MAX - used))
        self._images[user_id] = used + taken
        return taken

    def give_back_images(self, user_id: int, count: int) -> None:
        """Возврат квоты за картинки, которые не дали текста."""
        if count > 0 and user_id in self._images:
            self._images[user_id] = max(0, self._images[user_id] - count)

    async def add(self, update, context) -> None:
        user_id = update.effective_user.id
        text = text_of(update)
        if user_id in self._answering:
            # Ответ на прошлую пачку готовится: не ждём и не отменяем его.
            self._answering[user_id].append(text)
            db.log_event(self.conn, user_id, "pack_late", {"mode": self._mode_of(user_id)})
            return
        pack = self._open.get(user_id)
        if pack is None:
            pack = self._open[user_id] = _Pack()
            offer = self._offers.pop(user_id, None)
            if offer is not None:
                # Кнопку не нажали - поздние сообщения начинают следующую пачку.
                pack.texts.extend(offer.late)
        pack.updates.append(update)
        pack.texts.append(text)
        pack.context = context
        if getattr(update.effective_message, "is_screenshot", False):
            pack.images += 1
        await utils.react(update.effective_message)
        if pack.task is not None:
            pack.task.cancel()
        pack.task = asyncio.create_task(self._close_later(user_id, pack))

    def _mode_of(self, user_id: int) -> str:
        """Режим, которому идёт пачка: открытой сессии режима, иначе Справка."""
        session = db.active_session(self.conn, user_id)
        return session["mode"] if session is not None else Mode.REFERENCE.value

    def drop(self, user_id: int) -> bool:
        """Команда отбрасывает открытую пачку. True - было что отбросить."""
        pack = self._open.pop(user_id, None)
        if pack is None:
            return False
        if pack.task is not None:
            pack.task.cancel()
        self._images.pop(user_id, None)
        db.log_event(self.conn, user_id, "pack_dropped", {"messages": len(pack.texts)})
        return True

    async def _close_later(self, user_id: int, pack: _Pack) -> None:
        try:
            await asyncio.sleep(self.window)
            await wait_idle(user_id)
        except asyncio.CancelledError:
            return
        if self._open.get(user_id) is not pack:
            return  # пачку отбросила команда, пока ждали распознавания
        del self._open[user_id]
        self._images.pop(user_id, None)
        db.log_event(self.conn, user_id, "pack_closed", {
            "messages": len(pack.texts), "screenshots": pack.images,
            "ms": int((time.monotonic() - pack.started) * 1000),
        })
        await self._run(user_id, pack.updates[0], pack.context, pack.texts)

    async def _run(self, user_id: int, first, context, texts: list[str]) -> None:
        """Ответ на пачку; поздние за время ответа - пометкой с кнопкой."""
        joined = "\n".join(t for t in texts if t)
        message = first.effective_message
        self._answering[user_id] = []
        try:
            await self._answer(_with_text(first, joined), context)
        except Exception as error:
            # Фоновая задача: исключение само до обработчика ошибок PTB не
            # дойдёт, а человек должен узнать о сбое, как у обычного апдейта.
            app = getattr(context, "application", None)
            if app is None:
                logger.exception("Ответ на пачку упал")
            else:
                try:
                    await app.process_error(first, error)
                except Exception:  # noqa: BLE001 - фоновая задача, дальше некому
                    logger.exception("Ответ на пачку упал, обработчик ошибок тоже")
        finally:
            late = self._answering.pop(user_id, [])
        if not late:
            return
        offer = _Offer(next(self._tokens), first, texts, late)
        self._offers[user_id] = offer
        lang = self._lang_of(first)
        try:
            await utils.reply(
                message,
                i18n.t(lang, "PACK_LATE_NOTE", k=len(texts), m=len(late)),
                [(i18n.t(lang, "PACK_REDO_BUTTON"), f"{REDO_PREFIX}{offer.token}")],
            )
        except Exception:  # noqa: BLE001 - кнопка вторична, ответ уже ушёл
            logger.warning("Пометка о поздних сообщениях не отправлена", exc_info=True)

    async def redo(self, update, context) -> None:
        """Кнопка пересчёта: ответ по пачке и поздним; первый ответ модель
        видит в контексте диалога Справки (`Dialogs.remember`)."""
        query = update.callback_query
        user_id = update.effective_user.id
        token = (getattr(query, "data", None) or "").removeprefix(REDO_PREFIX)
        offer = self._offers.get(user_id)
        if offer is None or str(offer.token) != token:
            await utils.safe_answer(
                query, i18n.t(self._lang_of(update), "PACK_REDO_STALE")
            )
            return
        if user_id in self._answering or user_id in self._open:
            # Идёт ответ на новую пачку: пересчёт сейчас затёр бы её поздние.
            await utils.safe_answer(query, i18n.t(self._lang_of(update), "PACK_REDO_BUSY"))
            return
        await utils.safe_answer(query)
        del self._offers[user_id]
        db.log_event(self.conn, user_id, "pack_redo", {"late": len(offer.late)})
        await self._run(user_id, offer.update, context, offer.pack + offer.late)


def _with_text(update, text: str):
    from .screenshot_input import Override

    return Override(update, effective_message=Override(update.effective_message, text=text))
