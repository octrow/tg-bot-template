"""Переход из Справки в режим по просьбе словами (bot-modes): ожидающие
предложения и подпись кнопки. Что делает кнопка - в `handlers/modes.py`."""

import itertools
import secrets
from dataclasses import dataclass

from . import i18n

PREFIX = "mode:"
STALE, LOST = "stale", "lost"


@dataclass(frozen=True)
class Offer:
    token: str
    current: str  # режим, из которого предлагается переход: "reference"
    target: str  # режим из реестра (`handlers.registry`), метка классификатора
    text: str  # сообщение, с которым человек придёт в выбранный режим


class Switches:
    """Одно ожидающее предложение на пользователя.

    ponytail: память процесса: после рестарта кнопка
    отвечает 'пришлите ещё раз'. В БД - если рестарты станут частыми."""

    def __init__(self) -> None:
        # Метка процесса в callback_data: кнопку до рестарта отличаем от
        # устаревшей кнопки этого же процесса.
        self._epoch = secrets.token_hex(2)
        self._ids = itertools.count(1)
        self._pending: dict[int, Offer] = {}

    def offer(self, user_id: int, **fields) -> Offer:
        offer = Offer(token=f"{self._epoch}.{next(self._ids)}", **fields)
        self._pending[user_id] = offer
        return offer

    def drop(self, user_id: int) -> None:
        """Новое сообщение заменяет ожидающее предложение."""
        self._pending.pop(user_id, None)

    def take(self, user_id: int, token: str) -> Offer | str:
        """Предложение по нажатой кнопке, иначе `STALE` или `LOST` (рестарт)."""
        if not token.startswith(f"{self._epoch}."):
            return LOST
        offer = self._pending.get(user_id)
        if offer is None or offer.token != token:
            return STALE
        return self._pending.pop(user_id)


def go_label(lang: str, offer: Offer) -> str:
    return i18n.texts(lang).MODE_GO[offer.target]
