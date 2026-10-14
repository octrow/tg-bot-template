"""Справка: ответ на вопрос обычным текстом (спека reference-mode).

Здесь только чистая часть - промпты и память диалога. Хендлеры и кнопки
живут в `handlers/reference.py`.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import i18n

# Метка вызова агента: свой кэш и своя строка в `/cost`.
REFERENCE_CONTEXT = "reference_question"
# Облегчённый ответ на болтовню и вопросы о боте - без инструментов
# (reference-language-and-grounding).
CHAT_CONTEXT = "reference_chat"

# Ответ облегчённого вызова "вопрос всё-таки о продукте": идём обычным путём.
# Служебный маркер протокола с моделью - одинаков во всех локалях.
NEED_SOURCES = "NEED_SOURCES"

# Сколько живёт контекст диалога и сколько пар в него входит (спека).
DIALOG_TTL_S = 30 * 60
DIALOG_PAIRS = 2

# Попытки ответа на один вопрос: первая и одна повторная по "Не понятно".
FIRST_ATTEMPT = 1
RETRY_ATTEMPT = 2

# Метка диалога в callback_data кнопок под ответом: кнопка старого диалога
# не трогает новый. Случайная, а не счётчик: после перезапуска счётчик начал бы
# заново, и кнопка из прошлой жизни процесса совпала бы с новым диалогом.
TOKEN_BYTES = 4


def new_token() -> str:
    return secrets.token_hex(TOKEN_BYTES)


HOWTO_PATH = Path(__file__).resolve().parents[2] / "docs" / "howto.md"


def read_howto(path: Path = HOWTO_PATH) -> str | None:
    """Инструкция пользователя бота - с диска при каждом вызове: файл
    маленький, а кэш отдавал бы текст до деплоя. Нет файла - None, ответ
    всё равно идёт, просто без этого источника."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _history_block(history: list[tuple[str, str]], lang: str) -> str:
    if not history:
        return ""
    turns = "\n\n".join(
        i18n.t(lang, "PROMPT_REFERENCE_TURN", question=q, answer=a) for q, a in history
    )
    return i18n.t(lang, "PROMPT_REFERENCE_HISTORY", turns=turns)


def _howto_block(howto: str | None, lang: str) -> str:
    return i18n.t(lang, "PROMPT_REFERENCE_HOWTO", howto=howto) if howto else ""


def _product(product: str | None, lang: str) -> str:
    """Тема Справки: имя продукта из конфига или нейтральное по языку."""
    return product or i18n.t(lang, "PROMPT_PRODUCT_DEFAULT")


def prompt(
    question: str, history: list[tuple[str, str]], *, lang: str, product: str | None = None,
) -> str:
    return i18n.t(
        lang,
        "PROMPT_REFERENCE",
        product=_product(product, lang),
        question=question,
        history=_history_block(history, lang),
        howto=_howto_block(read_howto(), lang),
        style=i18n.t(lang, "PROMPT_CHAT_STYLE"),
    )


def chat_prompt(
    question: str, history: list[tuple[str, str]], *, lang: str, product: str | None = None,
) -> str:
    """Облегчённый ответ на болтовню и вопросы о боте: без инструментов и
    списка файлов (reference-language-and-grounding)."""
    return i18n.t(
        lang,
        "PROMPT_REFERENCE_CHAT",
        product=_product(product, lang),
        question=question,
        history=_history_block(history, lang),
        howto=_howto_block(read_howto(), lang),
        style=i18n.t(lang, "PROMPT_CHAT_STYLE"),
        marker=NEED_SOURCES,
    )


def needs_sources(answer) -> bool:
    """Модель облегчённого пути вернула маркер вместо ответа."""
    return str(answer or "").strip().strip("`.").strip() == NEED_SOURCES


def retry_prompt(
    question: str, first_answer: str, *, lang: str, product: str | None = None,
) -> str:
    """Повтор по "Не понятно": тот же вопрос, первый ответ и требование
    перепроверить и объяснить проще. Текст промпта другой - значит, и ключ
    кэша другой, из кэша первый ответ не вернётся."""
    return i18n.t(
        lang,
        "PROMPT_REFERENCE_RETRY",
        product=_product(product, lang),
        question=question,
        first_answer=first_answer,
        howto=_howto_block(read_howto(), lang),
        style=i18n.t(lang, "PROMPT_CHAT_STYLE"),
    )


@dataclass
class Dialog:
    pairs: list[tuple[str, str]] = field(default_factory=list)
    at: float = 0.0
    question: str = ""
    first_answer: str = ""
    attempt: int = FIRST_ATTEMPT
    # Повтор по "Не понятно" уже готовится: второе нажатие до ответа молчит.
    retrying: bool = False
    # Метка из callback_data кнопок под ответами этого диалога (`new_token`).
    token: str = ""


class Dialogs:
    """Короткий контекст Справки по пользователям.

    ponytail: память процесса - теряется при перезапуске, и тогда "Не понятно"
    под старым ответом ничего не повторяет (спека это допускает).
    Путь усиления - таблица в БД, если потери станут заметны.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._by_user: dict[int, Dialog] = {}

    def get(self, user_id: int) -> Dialog | None:
        """Живой контекст или None. Чтение ничего не удаляет: устаревшую
        запись перезапишет следующий `remember`."""
        dialog = self._by_user.get(user_id)
        if dialog is None or self._clock() - dialog.at > DIALOG_TTL_S:
            return None
        return dialog

    def history(self, user_id: int) -> list[tuple[str, str]]:
        dialog = self.get(user_id)
        return list(dialog.pairs) if dialog is not None else []

    def remember(self, user_id: int, question: str, answer: str, token: str = "") -> None:
        pairs = self.history(user_id) + [(question, answer)]
        self._by_user[user_id] = Dialog(
            pairs=pairs[-DIALOG_PAIRS:],
            at=self._clock(),
            question=question,
            first_answer=answer,
            token=token,
        )

    def current(self, user_id: int, token: str) -> Dialog | None:
        """Живой диалог с этой меткой; кнопка чужого (старого) диалога - None."""
        dialog = self.get(user_id)
        if dialog is None or not token or dialog.token != token:
            return None
        return dialog

    def remember_retry(self, user_id: int, answer: str, dialog: Dialog) -> bool:
        """Ответ повтора - в тот диалог, для которого он готовился. Пока модель
        думала, пользователь мог задать новый вопрос: тогда новый диалог не
        трогаем (False)."""
        if self.get(user_id) is not dialog:
            return False
        dialog.pairs[-1] = (dialog.question, answer)
        dialog.at = self._clock()
        dialog.attempt = RETRY_ATTEMPT
        return True

    def drop(self, user_id: int) -> None:
        self._by_user.pop(user_id, None)
