"""Сборка `Application`: зависимости обработчиков (`Deps` в `bot_data`),
обработчики зон из `handlers/` и их регистрация."""

import asyncio
import contextlib

from . import alerts, db, i18n, logging_setup, mode_switch, packs, screenshot_input, utils
from .access import Access
from .config import REMINDER_HOUR_DEFAULT, REMINDER_TZ_DEFAULT, TELEGRAM_BOT_API_BASE_URL_DEFAULT, Config
from .handlers import access as access_handlers
from .handlers import admin, common, language, reference, registry, support
from .handlers import modes as mode_handlers
from .handlers.common import make_error_handler, set_menu
from .handlers.deps import DEPS_KEY, Deps, user_lang
from .handlers.support import feedback_reminder_loop

# Задача цикла напоминаний об отзывах в `bot_data` (её снимает `post_stop`).
REMINDER_TASK_KEY = "reminder_task"

# Команды, которым `build_application` заводит CommandHandler. Списком, а не
# инлайном в цикле: тест обязан видеть, что новая команда действительно
# зарегистрирована, - handler в словаре без регистрации молчит в проде.
# Команды режимов дописывает реестр (`handlers.registry`).
COMMAND_HANDLER_NAMES = (
    "start", "help", "whoami", "language", "allow", "deny", "users", "cost",
    "cache_clear", "logs", "support", "feedback", "request_access",
    *registry.commands(),
)

# Кнопки (префикс callback_data -> имя обработчика) до общего обработчика: он
# забирает любой callback. Кнопки режимов дописывает реестр.
CALLBACK_PATTERNS = (
    ("^lang:", "language_callback"),
    ("^logs:", "logs_callback"),
    ("^fb_", "feedback_callback"),
    ("^fail:", "failure_callback"),
    (f"^{packs.REDO_PREFIX}", "pack_callback"),
    ("^access:", "access_callback"),
    ("^ref:", "reference_callback"),
    (f"^{mode_switch.PREFIX}", "mode_callback"),
    *((f"^{spec.callback_prefix}", f"{spec.mode}_callback") for spec in registry.MODES),
)


def build_deps(access: Access, config=None) -> Deps:
    """Зависимости обработчиков на всё приложение: пачки и вход картинок
    отдают текст обработчикам Справки."""
    conn = access.conn
    # Пачка сообщений (спека message-packs); окно 0 - без пачек.
    window = getattr(config, "text_debounce_s", 0) or 0
    pack_buffer = (
        packs.Packs(conn, window, reference.fallback, lambda update: user_lang(conn, update))
        if window > 0 else None
    )
    # Картинка распознаётся в текст и идёт тем же входом, что текст.
    images = screenshot_input.Images(
        conn, access, config, reference.packed_fallback, packs=pack_buffer
    )
    return Deps(access, config, packs=pack_buffer, images=images)


def build_handlers() -> dict:
    """Обработчики всех зон по именам; зависимости они берут из
    `context.bot_data` (`handlers.deps.get_deps`)."""
    handlers = {
        "cache_clear": admin.cache_clear,
        "logs": admin.logs,
        "logs_callback": admin.logs_callback,
        "cost": admin.cost,
        "start": common.start,
        "help": common.help_cmd,
        "whoami": access_handlers.whoami,
        "language": language.language,
        "language_callback": language.language_callback,
        "allow": admin.allow,
        "deny": admin.deny,
        "users": admin.users,
        "support": support.support,
        "reference_callback": reference.reference_callback,
        "mode_callback": mode_handlers.mode_callback,
        "command_reset": reference.command_reset,
        "log_update": reference.log_update,
        "feedback": admin.feedback,
        "feedback_callback": admin.feedback_callback,
        "failure_callback": common.failure_callback,
        "request_access": access_handlers.request_access,
        "access_callback": access_handlers.access_callback,
        # Свободный текст - Справке; исключение - черновик заявки на доступ
        # (см. handlers.reference.fallback).
        "fallback": reference.packed_fallback,
        "pack_callback": reference.pack_callback,
        "stray_callback": common.stray_callback,
        "image": reference.image,
    }
    for spec in registry.MODES:
        handlers[spec.command] = spec.command_handler
        handlers[f"{spec.mode}_callback"] = spec.callback_handler
    return handlers


def make_post_init(admin_user_ids, conn=None, *, reminder_tz=REMINDER_TZ_DEFAULT,
                   reminder_hour=REMINDER_HOUR_DEFAULT):
    """Собирает post_init-коллбек: меню команд трёх уровней (спека
    command-menu) - ограниченное по умолчанию (русское и английское вторым
    набором, `language_code="en"`), рабочее - персонально каждому из
    allowlist, рабочее плюс админское - каждому админу, всем на их языке;
    плюс запуск цикла напоминаний об отзывах. Ошибка Bot API не должна
    ронять запуск.

    ponytail: по одному вызову на пользователя; если их станут сотни -
    пакетирование с паузами под rate limit."""

    async def post_init(app) -> None:
        for lang in (i18n.DEFAULT_LANG, "en"):
            await set_menu(app.bot, None, "limited", lang)
        admins = set(admin_user_ids)
        if conn is not None:
            for row in db.list_users(conn):
                if row["user_id"] not in admins:
                    await set_menu(
                        app.bot, row["user_id"], "user", i18n.lang_for(conn, row["user_id"])
                    )
        for admin_id in admin_user_ids:
            lang = i18n.lang_for(conn, admin_id) if conn is not None else i18n.DEFAULT_LANG
            await set_menu(app.bot, admin_id, "admin", lang)
        alerts.configure(app.bot, admin_user_ids, conn)
        if conn is not None and admin_user_ids:
            # Не `app.create_task`: PTB 22 `Application.stop()` ждёт все такие
            # задачи, а цикл бесконечен - остановка висела бы до SIGKILL.
            # Ссылка в bot_data (GC не соберёт задачу), отмена - в `post_stop`.
            app.bot_data[REMINDER_TASK_KEY] = asyncio.create_task(feedback_reminder_loop(
                app.bot, conn, admin_user_ids, tz=reminder_tz, hour=reminder_hour,
            ))

    return post_init


async def post_stop(app) -> None:
    """Снимает цикл напоминаний: PTB зовёт это после `Application.stop()`."""
    task = app.bot_data.pop(REMINDER_TASK_KEY, None)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _register(app, handlers: dict) -> None:
    from telegram import Update
    from telegram.ext import (
        CallbackQueryHandler,
        CommandHandler,
        MessageHandler,
        TypeHandler,
        filters,
    )

    for name in COMMAND_HANDLER_NAMES:
        app.add_handler(CommandHandler(name, handlers[name]))
    for pattern, name in CALLBACK_PATTERNS:
        app.add_handler(CallbackQueryHandler(handlers[name], pattern=pattern))
    # Последним из кнопок: всё, что не разобрали обработчики выше.
    app.add_handler(CallbackQueryHandler(handlers["stray_callback"]))
    # Фото и файлы - до общего: картинка распознаётся в текст.
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handlers["image"]))
    app.add_handler(MessageHandler(filters.ALL, handlers["fallback"]))
    # Группа -1 идёт до основных и их не останавливает: только отбрасывает
    # пачку на команде. TypeHandler, а не MessageHandler - тесты ищут
    # текстовый хендлер как первый MessageHandler приложения.
    app.add_handler(TypeHandler(Update, handlers["command_reset"]), group=-1)
    # Раньше всех: метка `u=`/`upd=` в журнале процесса для всей обработки.
    # Своя группа - в одной группе срабатывает лишь первый подходящий хендлер.
    app.add_handler(TypeHandler(Update, logging_setup.bind_update), group=-2)
    # Ещё раньше - запись апдейта целиком: до проверки доступа и фильтра групп.
    app.add_handler(TypeHandler(Update, handlers["log_update"]), group=-3)


def build_application(config: Config, conn):
    from telegram.ext import Application

    # Свой бот, а не `.token()`: он делит тексты длиннее лимита Telegram
    # (спека long-message-send). `.bot()` с `.token()` несовместим,
    # и пул 256 - тот, что builder дал бы сам: у голого бота пул на одно соединение.
    base_url = getattr(config, "telegram_bot_api_base_url", TELEGRAM_BOT_API_BASE_URL_DEFAULT)
    bot = utils.SplittingBot(
        config.telegram_bot_token, request=utils.LoggingRequest(connection_pool_size=256, conn=conn), conn=conn,
        # Файлы живут под `/file/bot...` того же хоста (и с тем же `/test`).
        base_url=base_url, base_file_url="/file/bot".join(base_url.rsplit("/bot", 1)),
    )
    app = (
        Application.builder()
        .bot(bot)
        .post_init(make_post_init(
            config.admin_user_ids, conn,
            reminder_tz=config.reminder_tz, reminder_hour=config.reminder_hour,
        ))
        # Апдейты разных пользователей обрабатываются параллельно: иначе долгий
        # шаг одного (git, вызов LLM) держит очередь всем остальным.
        .concurrent_updates(True)
        .post_stop(post_stop)
        .build()
    )
    app.bot_data[DEPS_KEY] = build_deps(Access(conn, config.admin_user_ids, config.owner_only), config)
    _register(app, build_handlers())
    app.add_error_handler(make_error_handler(conn))
    return app
