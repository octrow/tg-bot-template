"""Приёмы пользователя Telethon против живого бота.

Не `conversation()`: бот шлёт статусы и удаляет их, а кнопки рекомендации
дописывает правкой уже отправленного вопроса - `get_response()` отдал бы
статус и вопрос без кнопок. Поэтому шаг = действие + ожидание, пока история
чата не перестанет меняться, + свежее чтение сообщений."""

import asyncio
import re
import time

from telethon.tl.types import MessageEntityBold, MessageEntityItalic

POLL_S = 1.0
QUIET_S = 2.5
STEP_TIMEOUT_S = 60.0
RAW_TAG = re.compile(r"</?(b|i|code|pre|a|blockquote|u|s)\b[^>]*>")


async def settle(client, bot, after_id: int = 0) -> list:
    """Ждёт тишины в чате; возвращает сообщения бота новее `after_id` (старые -> новые)."""
    deadline = time.monotonic() + STEP_TIMEOUT_S
    last_sig, quiet_since = None, time.monotonic()
    while True:
        messages = await client.get_messages(bot, limit=10, min_id=after_id)
        sig = [(m.id, m.edit_date, m.raw_text) for m in messages]
        now = time.monotonic()
        if sig != last_sig:
            last_sig, quiet_since = sig, now
        elif messages and now - quiet_since >= QUIET_S:
            return [m for m in reversed(messages) if not m.out]
        if now > deadline:
            raise AssertionError(f"бот не ответил за {STEP_TIMEOUT_S} с; видно: {sig}")
        await asyncio.sleep(POLL_S)


async def say(client, bot, text: str) -> list:
    """Сообщение боту; ответы бота на него."""
    sent = await client.send_message(bot, text)
    return await settle(client, bot, sent.id)


def button_data(message, prefix: str) -> bytes:
    for row in message.buttons or []:
        for b in row:
            data = getattr(b, "data", None)
            if data and data.decode().startswith(prefix):
                return data
    raise AssertionError(f"нет кнопки {prefix!r}: {datas(message)}")


def datas(message) -> list[str]:
    return [b.data.decode() for row in message.buttons or [] for b in row
            if getattr(b, "data", None)]


async def press(client, bot, message, prefix: str) -> tuple:
    """Нажатие; (то же сообщение заново, новые сообщения бота)."""
    await message.click(data=button_data(message, prefix))
    new = await settle(client, bot, message.id)
    fresh = await client.get_messages(bot, ids=message.id)
    return fresh, new


def assert_rendered(message, *, formatted: bool = False) -> None:
    """Telegram принял разметку: сырых тегов и сущностей HTML в тексте нет."""
    text = message.raw_text or ""
    assert not RAW_TAG.search(text), f"сырые теги: {text[:200]}"
    assert "&lt;" not in text and "&amp;" not in text, f"сырые сущности: {text[:200]}"
    if formatted:
        kinds = {type(e) for e in message.entities or []}
        assert kinds & {MessageEntityBold, MessageEntityItalic}, f"нет разметки: {kinds}"


def find(messages, contains: str):
    for m in messages:
        if contains in (m.raw_text or ""):
            return m
    raise AssertionError(f"нет сообщения с {contains!r}: {[m.raw_text[:80] for m in messages]}")
