"""Скриншот как вход бота (спека screenshot-input).

Картинка один раз распознаётся в текст (`vision`), и дальше сообщение идёт
общим текстовым путём (`fallback` бота) как текст: подпись плюс помеченный
распознанный скриншот. Справку это не меняет - она
читает `message.text` обёртки (ADR-0005, design.md решение 1).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from . import db, i18n, packs as packs_mod, utils, vision
from .access import is_private
from .agent.stages import STAGES, effective_provider, stage_url

logger = logging.getLogger(__name__)

MAX_BYTES = 10 * 1024 * 1024
MAX_MB = MAX_BYTES // (1024 * 1024)
# Без пачек - на альбом; в пачке лимит общий (`packs.IMAGES_MAX`).
ALBUM_MAX = packs_mod.IMAGES_MAX
# Тишина, после которой альбом считается пришедшим целиком: части приходят
# отдельными апдейтами, обычно в пределах секунды.
ALBUM_QUIET_S = 1.5
# Сколько помнить отвеченный альбом: опоздавшая часть не открывает второй ответ.
ALBUM_DONE_TTL_S = 300
# "печатает..." гаснет за ~5 с, а распознавание идёт до 60 с.
TYPING_EVERY_S = 4
# Причины, о которых человеку говорится прямо, без кода инцидента: это не сбой.
REFUSALS = {"format": "IMAGE_BAD_FORMAT", "size": "IMAGE_TOO_BIG", "no_key": "IMAGE_NO_KEY"}


class Override:
    """Объект с подменёнными полями; всё остальное - от исходного.

    Так сообщение с картинкой выглядит для бота текстовым (`text`), а ответы
    (`reply_text`, реакции, "печатает") уходят в исходное сообщение."""

    def __init__(self, base, **fields):
        self._base = base
        self.__dict__.update(fields)

    def __getattr__(self, name):
        return getattr(self._base, name)


def compose(caption: str, texts: list[str], lang: str) -> str:
    """Текст сообщения: подпись (или вопрос по умолчанию) и помеченные блоки."""
    head = caption or i18n.t(lang, "IMAGE_DEFAULT_QUESTION")
    blocks = [
        f"{i18n.t(lang, 'IMAGE_MARK') if len(texts) == 1 else i18n.t(lang, 'IMAGE_MARK_OF', k=i, n=len(texts))}\n{text}"
        for i, text in enumerate(texts, 1)
    ]
    return "\n\n".join(part for part in (head, *blocks) if part)


# Файл, похожий на картинку по типу или имени: формат всё равно решают первые
# байты, а это только отбор, кому отвечать про картинки. PDF - сюда же: его
# шлют вместо скриншота, и ответ "пришлите PNG/JPG" ему понятнее подсказки
# "пишите текстом". Прочие файлы получают прежнюю подсказку.
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif", ".gif", ".svg", ".bmp", ".tif", ".tiff", ".pdf")


def _looks_like_image(document) -> bool:
    mime = (getattr(document, "mime_type", None) or "").lower()
    if mime:
        return mime.startswith("image/") or mime == "application/pdf"
    return (getattr(document, "file_name", None) or "").lower().endswith(IMAGE_EXTS)


def _image_of(message):
    """(файл, размер, фото ли) или None, если картинки нет."""
    photo = getattr(message, "photo", None)
    if photo:
        biggest = photo[-1]
        return biggest, getattr(biggest, "file_size", None), True
    document = getattr(message, "document", None)
    if document is None or not _looks_like_image(document):
        return None
    return document, getattr(document, "file_size", None), False


async def _read_one(message, *, config, conn, user_id) -> dict:
    """Одна картинка -> {"text"} или {"reason"}; плюс поля для журнала."""
    file_obj, size, is_photo = _image_of(message)
    item = {"size": size, "photo": is_photo}
    if size is not None and size > MAX_BYTES:
        return {**item, "reason": "size"}
    provider = effective_provider(config)
    api_key = getattr(config, STAGES[provider]["key_attr"], None)
    if not api_key:
        return {**item, "reason": "no_key"}
    data = bytes(await (await file_obj.get_file()).download_as_bytearray())
    if len(data) > MAX_BYTES:  # размер не был известен до скачивания
        return {**item, "size": len(data), "reason": "size"}
    fmt = vision.detect_format(data[:16])
    if fmt is None:
        return {**item, "size": len(data), "reason": "format"}
    result = await vision.recognize(
        data, fmt, models=config.vision_models, api_key=api_key,
        conn=conn, user_id=user_id, judge_key=getattr(config, "typesafe_api_key", None),
        url=stage_url(config, STAGES[provider]["chat_url"]), provider=provider,
    )
    return {
        **item, "size": len(data), "format": fmt, "model": result.model,
        "seconds": result.seconds, "text": result.text, "reason": result.reason,
    }


async def _read_safely(message, **kwargs) -> dict:
    """Сбой скачивания не роняет весь альбом: картинка просто не прочитана."""
    try:
        return await _read_one(message, **kwargs)
    except Exception:  # noqa: BLE001 - причина уходит в журнал как error
        logger.warning("Картинка не скачана или не распознана", exc_info=True)
        return {"reason": "error"}


def _caption(messages: list) -> str:
    """Подпись альбома: первая непустая среди его частей."""
    return next(
        (c.strip() for m in messages if (c := getattr(m, "caption", None)) and c.strip()), ""
    )


def _limit_notes(lang: str, count: int, limit: int, texts: list, items: list) -> list[str]:
    """Пометки о прочитанном не целиком: лишние картинки, нераспознанные."""
    notes = []
    if count > limit:
        notes.append(i18n.t(lang, "IMAGE_FIRST_OF", k=limit, n=count))
    if texts and len(texts) < len(items):
        notes.append(i18n.t(lang, "IMAGE_READ_OF", k=len(texts), n=len(items)))
    return notes


async def _send_notes(message, notes: list[str]) -> None:
    if not notes:
        return
    try:
        await message.reply_text("\n".join(notes))
    except Exception:  # noqa: BLE001 - пометка вторична, ответ важнее
        logger.warning("Пометка о скриншотах не отправлена", exc_info=True)


class Images:
    """Вход фото и файлов (`handle`). Чужим и не-картинкам - прежний `fallback`.

    Держит альбомы, которые ещё собираются, и недавно отвеченные: части
    альбома приходят отдельными апдейтами."""

    def __init__(
        self, conn, access, config,
        fallback: Callable[..., Awaitable[None]],
        *, packs=None,
    ):
        self.conn, self.access, self.config = conn, access, config
        self.fallback = fallback
        self.packs = packs
        self.albums: dict[tuple[int, str], list] = {}
        self.done: dict[tuple[int, str], float] = {}  # отвеченные альбомы -> время ответа

    def _limit(self, user_id: int, count: int) -> int:
        if self.packs is None:
            return min(count, ALBUM_MAX)
        return self.packs.take_images(user_id, count)

    async def _pack_full(self, update, context, first, lang: str, caption: str, count: int) -> None:
        """Лимит пачки исчерпан прошлыми скриншотами: не распознаём, но и
        не молчим; подпись идёт в пачку как обычный текст."""
        db.log_event(self.conn, update.effective_user.id, "image_pack_full", {"count": count})
        await first.reply_text(i18n.t(lang, "IMAGE_PACK_FULL", n=count))
        if caption:
            await self.fallback(
                Override(update, effective_message=Override(first, text=caption)), context
            )

    async def _read_all(self, first, messages: list, user_id: int) -> list[dict]:
        typing = asyncio.create_task(_keep_typing(first))
        try:
            return list(await asyncio.gather(*(
                _read_safely(m, config=self.config, conn=self.conn, user_id=user_id)
                for m in messages
            )))
        finally:
            typing.cancel()

    def _log_failed(self, user_id: int, count: int, caption: str, items: list):
        """Ни одна картинка не прочитана: (код инцидента, ключ прямого отказа)."""
        reasons = [item["reason"] for item in items]
        code = db.log_event(self.conn, user_id, "image_failed", {
            "context": vision.CONTEXT_LABEL, "question": caption or "-",
            "count": count, "reasons": reasons,
        })
        return code, next((REFUSALS[r] for r in reasons if r in REFUSALS), None)

    async def _answer(self, update, context, messages: list) -> None:
        user = update.effective_user
        user_id = user.id
        lang = i18n.lang_for(self.conn, user_id, getattr(user, "language_code", None))
        first, caption = messages[0], _caption(messages)
        limit = self._limit(user_id, len(messages))
        if limit == 0:
            await self._pack_full(update, context, first, lang, caption, len(messages))
            return
        items = await self._read_all(first, messages[:limit], user_id)
        texts = [item["text"] for item in items if item.get("text")]
        if self.packs is not None:
            # Нераспознанные не съедают лимит пачки.
            self.packs.give_back_images(user_id, limit - len(texts))
        notes = _limit_notes(lang, len(messages), limit, texts, items)
        if texts:
            db.log_event(self.conn, user_id, "image_attached", {
                "count": len(messages), "caption": caption,
                "items": [{k: v for k, v in item.items() if k != "photo"} for item in items],
            })
        else:
            code, refusal = self._log_failed(user_id, len(messages), caption, items)
            if not caption:
                await _refuse(first, lang, refusal, code, items)
                return
            notes.insert(0, i18n.t(lang, refusal, mb=MAX_MB) if refusal
                         else i18n.t(lang, "IMAGE_FAILED_WITH_TEXT"))
        await _send_notes(first, notes)
        text = compose(caption, texts, lang)
        await self.fallback(
            Override(update, effective_message=Override(first, text=text, is_screenshot=True)),
            context,
        )

    async def _collect(self, update, context, key) -> None:
        """Ждёт тишины после последней части альбома и отвечает один раз."""
        seen = 0
        while seen != len(self.albums[key]):
            seen = len(self.albums[key])
            await asyncio.sleep(ALBUM_QUIET_S)
        now = time.monotonic()
        for old in [k for k, at in self.done.items() if now - at > ALBUM_DONE_TTL_S]:
            del self.done[old]
        self.done[key] = now
        await self._answer(update, context, self.albums.pop(key))

    async def handle(self, update, context) -> None:
        message = update.effective_message
        user = getattr(update, "effective_user", None)
        if (
            _image_of(message) is None or user is None
            or not is_private(update) or not self.access.is_allowed(user.id)
        ):
            await self.fallback(update, context)
            return
        group = getattr(message, "media_group_id", None)
        if group is None:
            # Счётчик держится от приёма картинки до передачи текста в пачку:
            # иначе пачка с текстом до скриншота закрылась бы в этом зазоре.
            with packs_mod.recognizing(user.id):
                await self._answer(update, context, [message])
            return
        await self._album_part(update, context, user.id, group, message)

    async def _album_part(self, update, context, user_id: int, group: str, message) -> None:
        # Ключ с пользователем: id альбома уникален только внутри чата.
        key = (user_id, group)
        # ponytail: буфер в памяти - рестарт в окне ALBUM_QUIET_S теряет
        # альбом; в БД - если это всплывёт.
        if key in self.albums:
            self.albums[key].append(message)
            return
        if key in self.done:
            # Часть пришла после ответа на альбом: второго ответа без контекста нет.
            db.log_event(self.conn, user_id, "image_late_part", {"group": group})
            return
        # Первая часть ждёт остальные и отвечает за весь альбом; апдейты идут
        # параллельно (`concurrent_updates`), так что остальные её не ждут.
        self.albums[key] = [message]
        with packs_mod.recognizing(user_id):
            await self._collect(update, context, key)


async def _keep_typing(message) -> None:
    while True:
        await utils.typing(message)
        await asyncio.sleep(TYPING_EVERY_S)


async def _refuse(message, lang: str, refusal: str | None, code: int, items: list) -> None:
    """Отказ без подписи: прямой - для формата и размера, со кодом инцидента -
    для сбоя модели (spec screenshot-input, "Сбой распознавания")."""
    if refusal:
        await message.reply_text(i18n.t(lang, refusal, mb=MAX_MB))
        return
    from .agent.failures import failure_markup

    text = i18n.t(lang, "IMAGE_FAILED")
    if any(item.get("photo") and item.get("reason") == "empty" for item in items):
        text = f"{text}\n\n{i18n.t(lang, 'IMAGE_SEND_AS_FILE')}"
    text = f"{text}\n\n{i18n.t(lang, 'FAILURE_CODE', code=code)}"
    await message.reply_text(text, reply_markup=failure_markup(lang, code))
