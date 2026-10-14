"""Язык пользователя и доступ к каталогу переводов (спека localization).

Один источник языка на весь бот - `lang_for`: сохранённый выбор пользователя,
иначе автоопределение по `language_code` профиля Telegram, иначе русский.
Дальше язык передаётся явным аргументом: получателем текста может быть не тот,
кто прислал апдейт (карточка заявки уходит админам, уведомление об исходе -
заявителю), и неявный "язык текущего апдейта" молча дал бы не ту локаль.

Тексты берутся либо пачкой - `T = texts(lang)` и дальше `T.START`, либо
по одному - `t(lang, "START", **fmt)`.
"""

import logging
from functools import lru_cache

from . import db
from .locales import CATALOGS

log = logging.getLogger(__name__)

LANGS: tuple[str, ...] = ("ru", "en")
DEFAULT_LANG = "ru"


class Texts:
    """Каталог одного языка с доступом по атрибуту.

    Отсутствующий ключ - ошибка разработки, а не рантайма: строку либо забыли
    добавить в обе локали, либо опечатались в имени. Молча подставить пустоту
    хуже - текст пропадёт из сообщения незаметно.
    """

    __slots__ = ("lang", "_strings")

    def __init__(self, lang: str, strings: dict):
        self.lang = lang
        self._strings = strings

    def __getattr__(self, name: str):
        try:
            return self._strings[name]
        except KeyError:
            raise AttributeError(
                f"нет ключа {name!r} в каталоге {self.lang!r}"
            ) from None

    def get(self, name: str, default=None):
        return self._strings.get(name, default)


@lru_cache(maxsize=len(LANGS))
def texts(lang: str | None) -> Texts:
    """Каталог языка; неизвестный или пустой язык - русский с записью в лог."""
    if lang not in CATALOGS:
        if lang is not None:
            log.warning("Неизвестный язык %r - показываем %s", lang, DEFAULT_LANG)
        lang = DEFAULT_LANG
    return Texts(lang, CATALOGS[lang])


def t(lang: str | None, key: str, **fmt) -> str:
    """Одна строка каталога, при необходимости - с подстановкой."""
    value = getattr(texts(lang), key)
    return value.format(**fmt) if fmt else value


def detect(language_code: str | None) -> str:
    """Язык по `language_code` профиля Telegram: `en*` - английский, иначе русский."""
    code = (language_code or "").strip().lower()
    return "en" if code.startswith("en") else DEFAULT_LANG


# Язык текста по алфавиту (спека localization): языков два, и у них
# разные алфавиты - классификатор не нужен. Пороги отсекают "ok", номера,
# ссылки и русский текст с английскими терминами.
TEXT_LANG_MIN_LETTERS = 20
TEXT_LANG_SHARE = 0.8


def text_lang(text: str | None) -> str | None:
    """`ru` / `en`, если текст явно на одном алфавите, иначе None.
    ponytail: кириллица = ru, латиница = en; украинский и транслит не различаются."""
    letters = [c for c in text or "" if c.isalpha()]
    if len(letters) < TEXT_LANG_MIN_LETTERS:
        return None
    cyrillic = sum("а" <= c.lower() <= "я" or c.lower() == "ё" for c in letters)
    latin = sum("a" <= c.lower() <= "z" for c in letters)
    for lang, count in (("ru", cyrillic), ("en", latin)):
        if count / len(letters) >= TEXT_LANG_SHARE:
            return lang
    return None


def normalize(lang: str | None) -> str | None:
    """Поддерживаемый язык или None (значение из БД могло прийти повреждённым)."""
    return lang if lang in LANGS else None


def lang_for(conn, user_id: int | None, language_code: str | None = None) -> str:
    """Язык получателя: сохранённый выбор -> автоопределение -> русский.

    `language_code` передаётся только там, где под рукой есть профиль Telegram
    того же пользователя: для чужого получателя (админ, заявитель) его нет, и
    решает сохранённое значение.
    """
    if user_id is None:
        return detect(language_code)
    stored = db.get_lang(conn, user_id)
    if stored is not None and normalize(stored) is None:
        log.warning(
            "Повреждённый язык %r у пользователя %s - показываем %s",
            stored, user_id, DEFAULT_LANG,
        )
        db.log_event(conn, user_id, "lang_invalid", {"value": str(stored)})
        return DEFAULT_LANG
    return stored or detect(language_code)
