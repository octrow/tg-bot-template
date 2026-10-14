"""Общее для всех зон: /start, /help, меню команд, обработчик ошибок,
кнопка "Сообщить разработчикам" и ответ на опечатку в команде."""

import logging

from .. import __version__, alerts, db, i18n, modes, utils
from ..access import Access, gate
from ..agent.stages import active_model
from ..modes import Mode
from . import registry
from .deps import get_deps, user_lang

logger = logging.getLogger(__name__)


# --- меню команд Telegram (порядок = порядок в меню) ----------------------
# Три уровня (clearer-bot-output): ограниченное - набор по умолчанию для
# чужого и после /deny, рабочее - допущенному, админу - рабочее плюс
# админское. Команды, убранные из меню, работают как раньше.
LIMITED_COMMAND_NAMES: tuple[str, ...] = (
    "whoami",
    "request_access",
    "language",
)
# Команды режимов - из реестра (`handlers.registry`), сразу после /help.
USER_COMMAND_NAMES: tuple[str, ...] = (
    "start",
    "help",
    *registry.commands(),
    "support",
    "language",
)
ADMIN_COMMAND_NAMES: tuple[str, ...] = (
    "allow",
    "deny",
    "users",
    "cost",
    "cache_clear",
    "logs",
    "feedback",
)


def _commands(lang: str, names: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    descriptions = i18n.texts(lang).CMD_DESCRIPTIONS
    return tuple((name, descriptions[name]) for name in names)


def user_commands(lang: str) -> tuple[tuple[str, str], ...]:
    return _commands(lang, USER_COMMAND_NAMES)


def admin_commands(lang: str) -> tuple[tuple[str, str], ...]:
    return _commands(lang, ADMIN_COMMAND_NAMES)


def menu_commands(lang: str, tier: str) -> tuple[tuple[str, str], ...]:
    """Меню уровня `tier` (limited / user / admin) на языке `lang`."""
    if tier == "limited":
        return _commands(lang, LIMITED_COMMAND_NAMES)
    if tier == "admin":
        return user_commands(lang) + admin_commands(lang)
    return user_commands(lang)


def help_text(model: str, is_admin: bool, lang: str) -> str:
    """Тот же источник, что и меню Telegram: админ видит и админ-команды."""
    T = i18n.texts(lang)
    commands = user_commands(lang) + (admin_commands(lang) if is_admin else ())
    lines = "\n".join(
        T.HELP_COMMAND_LINE.format(name=name, desc=desc) for name, desc in commands
    )
    return T.HELP_TEXT.format(lines=lines, model=model, version=__version__)


async def set_menu(bot, user_id: int | None, tier: str, lang: str) -> bool:
    """Меню уровня `tier` (limited / user / admin) на языке `lang`.

    `user_id` - персонально его чату; None - набор по умолчанию (русский без
    `language_code`, остальные - со своим). Ошибка Bot API пишется в лог и не
    отменяет действие, ради которого меню менялось (clearer-bot-output).
    True - Bot API меню принял.
    """
    from telegram import BotCommand, BotCommandScopeChat

    kwargs = {}
    if user_id is not None:
        kwargs["scope"] = BotCommandScopeChat(chat_id=user_id)
    elif lang != i18n.DEFAULT_LANG:
        kwargs["language_code"] = lang
    try:
        commands = [BotCommand(name, desc) for name, desc in menu_commands(lang, tier)]
        await bot.set_my_commands(commands, **kwargs)
    except Exception:
        logger.warning(
            "Не удалось поставить меню %s пользователю %s", tier, user_id, exc_info=True
        )
        return False
    return True


def menu_tier(access: Access, user_id: int) -> str:
    """Уровень меню по текущему доступу: админ остаётся админом и после /deny."""
    if access.is_admin(user_id):
        return "admin"
    return "user" if access.is_allowed(user_id) else "limited"


async def apply_menu(deps, bot, user_id: int) -> None:
    """Меню текущего уровня на языке пользователя; запоминаем только успех."""
    menu = (menu_tier(deps.access, user_id), i18n.lang_for(deps.conn, user_id))
    if await set_menu(bot, user_id, *menu):
        deps.applied_menus[user_id] = menu


async def ensure_menu(deps, bot, user_id: int) -> None:
    """Ставит меню, только если нужное ещё не стоит."""
    menu = (menu_tier(deps.access, user_id), i18n.lang_for(deps.conn, user_id))
    if deps.applied_menus.get(user_id) != menu:
        await apply_menu(deps, bot, user_id)


# --- глобальный обработчик ошибок -------------------------------------------

def _update_context(update) -> tuple[int | None, int | None, int | None]:
    """(user_id, chat_id, update_id) из апдейта; ошибка могла прийти и без
    апдейта (update=None) - тогда все три None, писать пользователю некуда."""
    if update is None or not hasattr(update, "update_id"):
        return None, None, None
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    return (
        user.id if user is not None else None,
        chat.id if chat is not None else None,
        update.update_id,
    )


# Отказы Bot API, которые не значат поломку: повторная правка тем же
# содержимым и ответ на нажатие, у которого истёк срок.
TRANSIENT_BAD_REQUESTS = ("not modified", "query is too old")


def _is_transient(error, update) -> bool:
    from telegram.error import BadRequest, NetworkError, RetryAfter

    if isinstance(error, BadRequest):
        text = str(error).lower()
        return any(marker in text for marker in TRANSIENT_BAD_REQUESTS)
    if isinstance(error, RetryAfter):
        return True
    # Сеть посреди хендлера (ответ не ушёл) - человек ждёт, молчание хуже:
    # error_handler шлёт FAILURE_NETWORK. Без апдейта (сбой опроса) - некому.
    # TimedOut - подкласс NetworkError; BadRequest тоже, поэтому он выше.
    return isinstance(error, NetworkError) and update is None


def _stop_on_conflict(conn, context, error) -> None:
    """Второй экземпляр перехватил getUpdates - вместо бесконечных повторов
    polling логируем конфликт и останавливаем процесс. stop_running()
    завершает run_polling() штатно (код 0), поэтому флаг в bot_data велит
    main() выйти с ненулевым кодом - иначе systemd Restart не сочтёт это
    сбоем и бот останется лежать."""
    logger.error(
        "Второй экземпляр бота перехватил getUpdates (Conflict)",
        exc_info=error,
    )
    db.log_event(conn, None, "instance_conflict", {"error": str(error)})
    context.application.bot_data["instance_conflict"] = True
    context.application.stop_running()


def _failure_reply(conn, error, user_id, details: dict):
    """(текст, кнопка) пользователю о сбое; событие уже в журнале."""
    from telegram.error import BadRequest, NetworkError

    from ..agent.failures import failure_markup

    # user_id может быть None (апдейт без отправителя) - lang_for
    # в этом случае просто отдаёт язык по умолчанию.
    lang = i18n.lang_for(conn, user_id)
    # BadRequest - подкласс NetworkError, но это отказ Bot API, а не сеть.
    if isinstance(error, NetworkError) and not isinstance(error, BadRequest):
        # Сеть до Telegram посреди хендлера (код 8241, 06.10.2026): бот
        # цел, ответ просто не ушёл. Человеку - что повтор поможет; без
        # кода инцидента и алерта, это не поломка.
        logger.warning("Сеть до Telegram посреди хендлера: %r", error)
        db.log_event(conn, user_id, "telegram_network", details)
        return i18n.t(lang, "FAILURE_NETWORK"), None
    logger.error("Необработанное исключение в хендлере", exc_info=error)
    code = db.log_event(conn, user_id, "handler_error", details)
    alerts.notify(alerts.KIND_HANDLER_ERROR, code, mode="handler_error")
    text = f"{i18n.t(lang, 'FAILURE_BOT')}\n\n{i18n.t(lang, 'FAILURE_CODE', code=code)}"
    # Кнопка - только со своим пользователем: Отзыв привязывается к нему.
    return text, failure_markup(lang, code) if user_id is not None else None


def make_error_handler(conn):
    """Собирает глобальный обработчик ошибок (`app.add_error_handler`):
    необработанное исключение в любом хендлере не должно молча уходить
    только в лог - пользователь должен узнать, что что-то сломалось, а
    инцидент - остаться в журнале с контекстом. Сам обработчик исключение
    никогда не пробрасывает дальше, иначе PTB просто напечатает "No error
    handlers are registered" и всё равно проглотит ошибку."""

    async def error_handler(update, context):
        from telegram.error import Conflict

        error = context.error
        if isinstance(error, Conflict):
            _stop_on_conflict(conn, context, error)
            return
        if _is_transient(error, update):
            # Сеть моргнула или кнопку нажали дважды: бот не сломан, и
            # FAILURE_BOT с кодом и алертом здесь - ложная тревога.
            logger.warning("Временная ошибка Telegram: %r", error)
            return
        user_id, chat_id, update_id = _update_context(update)
        details = {"update_id": update_id, "chat_id": chat_id, "error": str(error),
                   "exception": type(error).__name__}
        text, markup = _failure_reply(conn, error, user_id, details)
        if chat_id is None:
            return
        try:
            await context.bot.send_message(chat_id, text, reply_markup=markup)
        except Exception:
            # Отправка ошибки об ошибке не должна ронять обработчик -
            # инцидент уже записан в лог и журнал строками выше.
            logger.warning(
                "Не удалось отправить сообщение об ошибке пользователю",
                exc_info=True,
            )

    return error_handler


# --- обработчики --------------------------------------------------------------

async def start(update, context):
    # Telegram шлёт /start по кнопке "Start" при первом контакте.
    deps = get_deps(context)
    if not await gate(update, deps.access):
        return
    conn = deps.conn
    user_id = update.effective_user.id
    lang = user_lang(conn, update)
    db.log_event(conn, user_id, "command", {"command": "/start"})
    await ensure_menu(deps, context.bot, user_id)
    # /start возвращает в режим по умолчанию - Справку: сессия режима закрывается.
    db.close_mode_session(conn, user_id)
    modes.enter(Mode.REFERENCE)
    await utils.send_html(update.effective_message, i18n.t(lang, "START"))


async def help_cmd(update, context):
    deps = get_deps(context)
    if not await gate(update, deps.access):
        return
    user_id = update.effective_user.id
    lang = user_lang(deps.conn, update)
    db.log_event(deps.conn, user_id, "command", {"command": "/help"})
    model = active_model(deps.config)
    await update.effective_message.reply_text(
        help_text(model, deps.access.is_admin(user_id), lang)
    )


async def not_a_question(deps, update) -> None:
    """Не текст (стикер, голосовое) и опечатка в команде: молчание выглядит
    поломкой, а BotFather считает его алертом "Too few replies"."""
    if not await gate(update, deps.access):
        return
    user_id = update.effective_user.id
    message = update.effective_message
    lang = user_lang(deps.conn, update)
    text = (getattr(message, "text", None) or "").strip()
    if not text:
        await message.reply_text(i18n.t(lang, "NON_TEXT_HINT"))
        return
    if text.startswith("/"):
        # Известные команды разобрали CommandHandler'ы; сюда доходят
        # только опечатки (/whomis).
        db.log_event(deps.conn, user_id, "unknown_command", {"text": text})
        await message.reply_text(i18n.t(lang, "UNKNOWN_COMMAND"))


# Записи о сбое, под которыми стоит кнопка "Сообщить разработчикам".
FAILURE_KINDS = ("llm_failure", "handler_error", "image_failed")


async def _report_failure(conn, message, lang: str, user_id: int, data: str) -> None:
    """Отзыв по коду инцидента `fail:<log_id>` - только из своей записи о сбое."""
    log_id = int(data.partition(":")[2])
    record = next(
        (p for kind in FAILURE_KINDS
         if (p := db.log_payload(conn, log_id, kind=kind, user_id=user_id)) is not None),
        None,
    )
    if record is None:
        return
    if db.failure_reported(conn, user_id, log_id):
        await message.reply_text(i18n.t(lang, "FAILURE_ALREADY_REPORTED"))
        return
    text = i18n.t(
        lang, "FAILURE_FEEDBACK_TAG", code=log_id,
        mode=record.get("context") or "handler_error",
        question=record.get("question") or "-",
    )
    feedback_id = db.add_feedback(conn, user_id, db.last_session_id(conn, user_id), text)
    db.log_event(conn, user_id, "failure_reported", {"log_id": log_id, "feedback_id": feedback_id})
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001 - снятие кнопки вторично
        pass
    await message.reply_text(i18n.t(lang, "FAILURE_REPORTED"))


async def failure_callback(update, context):
    """Кнопка "Сообщить разработчикам" под сообщением о сбое: `fail:<log_id>`.

    Отзыв создаётся сразу, по коду инцидента, только из своей записи о
    сбое; повторное нажатие второго Отзыва не создаёт. Наружу не бросает:
    ошибка здесь сама стала бы `handler_error`."""
    deps = get_deps(context)
    query = update.callback_query
    await utils.safe_answer(query)
    if not await gate(update, deps.access):
        return
    user_id = update.effective_user.id
    lang = user_lang(deps.conn, update)
    message = update.effective_message
    try:
        await _report_failure(deps.conn, message, lang, user_id, getattr(query, "data", "") or "")
    except Exception:  # noqa: BLE001 - см. docstring
        logger.warning("Отзыв о сбое не создан", exc_info=True)
        try:
            await message.reply_text(i18n.t(lang, "FAILURE_REPORT_FAILED"))
        except Exception:  # noqa: BLE001
            pass


async def stray_callback(update, context):
    """Кнопка без своего обработчика (сообщение старой версии бота): гасим
    часики на кнопке, в чат ничего не пишем (click-idempotency)."""
    await utils.safe_answer(update.callback_query)
