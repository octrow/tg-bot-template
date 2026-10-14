"""Админ-команды: /allow, /deny, /users, /cost, /cache_clear, /logs (кнопки
`logs:`) и /feedback (кнопки `fb_read:` / `fb_log:`)."""

import asyncio
import logging
import re
from pathlib import Path

from .. import db, i18n, journal, logs_view, utils
from ..access import gate
from .common import apply_menu
from .deps import get_deps, user_lang

logger = logging.getLogger(__name__)


# Страница списка файлов журнала: столько кнопок влезает в экран телефона без
# прокрутки, а файлов в каталоге сотни.
LOGS_PAGE_SIZE = 8
_LOG_NAME_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})-u(\d+)-s(\d+)\.jsonl$")


def _logs_label(name: str) -> str:
    """Подпись кнопки: сессия опознаётся до открытия файла - когда началась,
    чья и какая. Имя не по формату (`bot.jsonl`) показываем как есть."""
    parsed = _LOG_NAME_RE.match(name)
    if parsed is None:
        return name
    _year, month, day, hour, minute, _sec, user_id, session_id = parsed.groups()
    return f"{day}.{month} {hour}:{minute} u{user_id} s{session_id}"


def _render_parts(path: Path, lang: str) -> list[str]:
    """Чтение файла, сборка `.md` и резка по лимиту Bot API - одним вызовом
    для `asyncio.to_thread`; к соединению с БД отсюда не обращаемся."""
    return logs_view.split_for_telegram(logs_view.render_log_file(path, lang), lang)


async def _admin_only(deps, update) -> bool:
    if not await gate(update, deps.access):
        return False
    user_id = update.effective_user.id
    if deps.access.is_admin(user_id):
        return True
    lang = user_lang(deps.conn, update)
    db.log_event(deps.conn, user_id, "admin_denied", {})
    await update.effective_message.reply_text(i18n.t(lang, "ADMIN_REFUSAL"))
    return False


async def _resolve_target(conn, update, context, command: str, lang: str) -> int | None:
    """user_id из аргумента команды (число или @username из писавших боту).

    На плохой аргумент или неизвестный username отвечает сам и возвращает None.
    """
    args = getattr(context, "args", None) or []
    arg = args[0] if len(args) == 1 else ""
    if not arg.lstrip("@"):
        await update.effective_message.reply_text(
            i18n.t(lang, "USAGE_TARGET", command=command)
        )
        return None
    try:
        return int(arg)
    except ValueError:
        pass
    username = arg.lstrip("@")
    found = db.find_seen_by_username(conn, username)
    if found is None:
        await update.effective_message.reply_text(
            i18n.t(lang, "UNSEEN_USERNAME", username=username)
        )
    return found


async def allow(update, context):
    deps = get_deps(context)
    conn = deps.conn
    if not await _admin_only(deps, update):
        return
    admin_id = update.effective_user.id
    lang = user_lang(conn, update)
    target = await _resolve_target(conn, update, context, "/allow", lang)
    if target is None:
        return
    added = db.add_user(conn, target, admin_id)
    db.log_event(
        conn, admin_id, "allow", {"target_user_id": target, "changed": added}
    )
    await apply_menu(deps, context.bot, target)
    await update.effective_message.reply_text(
        i18n.t(lang, "ALLOW_ADDED", target=target)
        if added
        else i18n.t(lang, "ALLOW_ALREADY", target=target)
    )


async def deny(update, context):
    deps = get_deps(context)
    conn = deps.conn
    if not await _admin_only(deps, update):
        return
    admin_id = update.effective_user.id
    lang = user_lang(conn, update)
    target = await _resolve_target(conn, update, context, "/deny", lang)
    if target is None:
        return
    removed = db.remove_user(conn, target)
    db.log_event(
        conn, admin_id, "deny", {"target_user_id": target, "changed": removed}
    )
    # Персональное меню хранит Telegram: без явной замены у отозванного
    # осталось бы рабочее (design.md, риски).
    await apply_menu(deps, context.bot, target)
    await update.effective_message.reply_text(
        i18n.t(lang, "DENY_REMOVED", target=target)
        if removed
        else i18n.t(lang, "DENY_ABSENT", target=target)
    )


async def users(update, context):
    deps = get_deps(context)
    conn = deps.conn
    if not await _admin_only(deps, update):
        return
    admin_id = update.effective_user.id
    lang = user_lang(conn, update)
    T = i18n.texts(lang)
    db.log_event(conn, admin_id, "command", {"command": "/users"})
    rows = db.list_users(conn)
    admins = ", ".join(str(i) for i in sorted(deps.access.admin_user_ids)) or T.USERS_NONE
    lines = [T.USERS_ADMINS.format(admins=admins), T.USERS_ALLOWLIST]
    if rows:
        for r in rows:
            username = db.seen_username(conn, r["user_id"])
            nick = f" @{username}" if username else ""
            lines.append(
                T.USERS_ROW.format(
                    user_id=r["user_id"],
                    nick=nick,
                    added_by=r["added_by"],
                    added_at=r["added_at"],
                )
            )
    else:
        lines.append(T.USERS_EMPTY)
    await update.effective_message.reply_text("\n".join(lines))


async def cost(update, context):
    deps = get_deps(context)
    conn = deps.conn
    if not await _admin_only(deps, update):
        return
    admin_id = update.effective_user.id
    lang = user_lang(conn, update)
    arg = context.args[0] if context.args else None
    db.log_event(conn, admin_id, "command", {"command": "/cost", "arg": arg})

    if arg is None:
        session_id = db.last_session_id(conn, admin_id)
        if session_id is None:
            await update.effective_message.reply_text(i18n.t(lang, "COST_NO_SESSIONS"))
            return
    else:
        try:
            session_id = int(arg)
        except ValueError:
            await update.effective_message.reply_text(
                i18n.t(lang, "COST_NOT_FOUND", session_id=arg)
            )
            return

    summary = db.session_cost(conn, session_id)
    if summary is None:
        await update.effective_message.reply_text(
            i18n.t(lang, "COST_NOT_FOUND", session_id=session_id)
        )
        return
    text = i18n.t(lang, "COST_TEXT", **summary)
    if summary["unpriced"]:
        text += i18n.t(lang, "COST_UNPRICED", **summary)
    await update.effective_message.reply_text(text)


async def cache_clear(update, context):
    """Сброс кэша ответов: ждать сутки TTL после инцидента неприемлемо,
    а лезть в SQLite на сервере руками - тем более."""
    deps = get_deps(context)
    conn = deps.conn
    if not await _admin_only(deps, update):
        return
    admin_id = update.effective_user.id
    lang = user_lang(conn, update)
    removed = db.clear_cache(conn)
    db.log_event(conn, admin_id, "cache_cleared", {"removed": removed})
    await update.effective_message.reply_text(i18n.t(lang, "CACHE_CLEARED", removed=removed))


async def logs(update, context):
    """Файлы журнала в чат: разбор инцидента без ssh на сервер.

    Файл отдаётся целиком, вместе с полными текстами промптов и ответов
    LLM - то есть с перепиской пользователей (спека admin-log-access).
    Условие пересмотра: как только круг администраторов перестанет
    совпадать с кругом людей, которым разрешено читать переписки
    пользователей, выдача обязана смениться на выдачу без текстов - и это
    отдельное изменение спеки, а не правка здесь.
    """
    deps = get_deps(context)
    conn = deps.conn
    if not await _admin_only(deps, update):
        return
    admin_id = update.effective_user.id
    lang = user_lang(conn, update)
    db.log_event(conn, admin_id, "command", {"command": "/logs"})
    await _logs_page(update.effective_message, 0, lang)


async def _logs_page(message, page: int, lang: str, *, edit: bool = False) -> None:
    """Страница списка журнала. `edit` - переход по страницам: правит то
    же сообщение, а не присылает новое (clearer-bot-output)."""
    T = i18n.texts(lang)
    files = journal.list_log_files()
    if not files:
        if edit:
            await utils.edit(message, T.LOGS_EMPTY)
            return
        await message.reply_text(T.LOGS_EMPTY)
        return
    pages = (len(files) + LOGS_PAGE_SIZE - 1) // LOGS_PAGE_SIZE
    page = min(max(page, 0), pages - 1)
    # callback_data - имя файла, а не позиция: ротация или новая сессия
    # сдвигают нумерацию между показом и нажатием (решение 4).
    rows = [
        (_logs_label(f.name), f"logs:f:{f.name}")
        for f in files[page * LOGS_PAGE_SIZE : (page + 1) * LOGS_PAGE_SIZE]
    ]
    if page:
        rows.append((T.LOGS_PREV_BUTTON, f"logs:p:{page - 1}"))
    if page + 1 < pages:
        rows.append((T.LOGS_NEXT_BUTTON, f"logs:p:{page + 1}"))
    title = T.LOGS_PAGE_TITLE.format(page=page + 1, pages=pages, total=len(files))
    if edit:
        await utils.edit(message, title, rows)
        return
    await message.reply_text(title, reply_markup=utils.markup(rows))


async def logs_callback(update, context):
    """Кнопки списка журнала (pattern `^logs:`). Сообщение не расходуется:
    список листают сколько нужно, а повторная выдача файла так же
    попадает в журнал."""
    deps = get_deps(context)
    conn = deps.conn
    if not await _admin_only(deps, update):
        return
    admin_id = update.effective_user.id
    lang = user_lang(conn, update)
    query = update.callback_query
    payload = getattr(query, "data", None) or ""
    await utils.safe_answer(query)
    if payload.startswith("logs:p:"):
        try:
            page = int(payload[len("logs:p:") :])
        except ValueError:
            return
        await _logs_page(update.effective_message, page, lang, edit=True)
    elif payload.startswith("logs:f:"):
        await _logs_send(
            conn,
            update.effective_message,
            admin_id,
            payload[len("logs:f:") :],
            lang,
        )


async def _logs_send(conn, message, admin_id: int, name: str, lang: str) -> None:
    T = i18n.texts(lang)
    path = journal.logs_dir() / name
    # Имя пришло кнопкой, но пришло снаружи: за пределы каталога не пускаем.
    if name != Path(name).name or not path.is_file():
        await message.reply_text(T.LOGS_FILE_GONE.format(name=name))
        return
    parts = await asyncio.to_thread(_render_parts, path, lang)
    total = len(parts)
    for number, part in enumerate(parts, 1):
        await message.reply_document(
            document=part.encode("utf-8"),
            filename=f"{Path(name).stem}.md"
            if total == 1
            else f"{Path(name).stem}.part{number}of{total}.md",
            caption=None
            if total == 1
            else T.LOGS_PART.format(number=number, total=total),
        )
    # Единственный след того, кто выгрузил чужую переписку (решение 6).
    db.log_event(conn, admin_id, "log_file_sent", {"file": name, "parts": total})


def _feedback_author(conn, user_id: int) -> str:
    username = db.seen_username(conn, user_id)
    return f"@{username}" if username else str(user_id)


def _feedback_card(conn, item: dict, lang: str) -> tuple[str, list[tuple[str, str]]]:
    """Текст карточки отзыва и её кнопки; 'Лог сессии' - только у отзывов
    с привязкой (отзыв без единой сессии показывать логом нечем)."""
    T = i18n.texts(lang)
    session_note = (
        T.FEEDBACK_SESSION_NOTE.format(session_id=item["session_id"])
        if item["session_id"] is not None
        else ""
    )
    text = T.FEEDBACK_CARD.format(
        id=item["id"],
        author=_feedback_author(conn, item["user_id"]),
        ts=item["ts"],
        session_note=session_note,
        text=item["text"],
    )
    rows = [(T.FEEDBACK_READ_BUTTON, f"fb_read:{item['id']}")]
    if item["session_id"] is not None:
        rows.append((T.FEEDBACK_LOG_BUTTON, f"fb_log:{item['id']}"))
    return text, rows


FEEDBACK_ALL_LIMIT = 10


def _feedback_all_text(conn, items: list[dict], lang: str) -> str:
    """Сводка последних отзывов одним сообщением со статусами OK/новый."""
    T = i18n.texts(lang)
    total = conn.execute("SELECT count(*) AS n FROM feedback").fetchone()["n"]
    lines = [T.FEEDBACK_ALL_HEADER.format(shown=len(items), total=total)]
    for item in items:
        status = T.FEEDBACK_STATUS_READ if item["read_at"] else T.FEEDBACK_STATUS_UNREAD
        lines.append(
            T.FEEDBACK_ALL_LINE.format(
                status=status,
                id=item["id"],
                ts=item["ts"],
                author=_feedback_author(conn, item["user_id"]),
                text=item["text"],
            )
        )
    return "\n".join(lines)


async def feedback(update, context):
    """Непрочитанные отзывы, каждый отдельным сообщением с кнопками;
    просмотр статус не меняет - его снимает только кнопка 'Прочитано'.
    `/feedback all` - последние 10 со статусом текстом."""
    deps = get_deps(context)
    conn = deps.conn
    if not await _admin_only(deps, update):
        return
    admin_id = update.effective_user.id
    lang = user_lang(conn, update)
    args = getattr(context, "args", None) or []
    db.log_event(conn, admin_id, "command", {"command": "/feedback", "arg": args[:1]})
    message = update.effective_message
    wants_all = args[:1] == ["all"]
    items = (
        db.recent_feedback(conn, FEEDBACK_ALL_LIMIT)
        if wants_all
        else db.unread_feedback(conn)
    )
    if not items:
        await message.reply_text(i18n.t(lang, "FEEDBACK_NONE"))
        return
    if wants_all:
        await message.reply_text(_feedback_all_text(conn, items, lang))
        return
    for item in items:
        text, rows = _feedback_card(conn, item, lang)
        await message.reply_text(text, reply_markup=utils.markup(rows))


async def _feedback_mark_read(conn, update, admin_id: int, item: dict, toast, lang: str) -> None:
    T = i18n.texts(lang)
    marked = db.mark_feedback_read(conn, item["id"], admin_id)
    if not marked:
        # Идемпотентность: второй клик (или второй админ) - не ошибка.
        await toast(T.FEEDBACK_ALREADY_READ)
        return
    db.log_event(conn, admin_id, "feedback_read", {"feedback_id": item["id"]})
    await toast(T.FEEDBACK_MARKED)
    # Кнопку 'Прочитано' убираем, 'Лог сессии' оставляем: журнал
    # полезен и после закрытия отзыва.
    rows = (
        [(T.FEEDBACK_LOG_BUTTON, f"fb_log:{item['id']}")]
        if item["session_id"] is not None
        else []
    )
    try:
        await update.effective_message.edit_reply_markup(
            reply_markup=utils.markup(rows)
        )
    except Exception:  # правка разметки вторична, пометка уже в БД
        logger.warning("Не удалось убрать кнопку отзыва", exc_info=True)


async def _feedback_send_log(conn, message, admin_id: int, item: dict, lang: str) -> None:
    session_id = item["session_id"]
    name = (
        db.session_log_filename(conn, session_id)
        if session_id is not None
        else None
    )
    if name is None:
        await message.reply_text(
            i18n.t(lang, "LOGS_FILE_GONE", name=f"s{session_id}")
        )
        return
    # Дальше - общий с /logs путь: рендер, резка на части, мягкий
    # отказ при файле, удалённом ротацией.
    await _logs_send(conn, message, admin_id, name, lang)


async def feedback_callback(update, context):
    """Кнопки карточки отзыва: `fb_read:<id>` и `fb_log:<id>`."""
    deps = get_deps(context)
    conn = deps.conn
    if not await _admin_only(deps, update):
        return
    admin_id = update.effective_user.id
    lang = user_lang(conn, update)
    query = update.callback_query
    payload = getattr(query, "data", None) or ""

    async def toast(text: str | None = None) -> None:
        await utils.safe_answer(query, text)

    prefix, _, raw_id = payload.partition(":")
    try:
        feedback_id = int(raw_id)
    except ValueError:
        return
    item = db.get_feedback(conn, feedback_id)
    if item is None:
        # Отзыв удалили из БД руками между показом и нажатием.
        await toast(i18n.t(lang, "FEEDBACK_NOT_FOUND"))
        return
    if prefix == "fb_read":
        await _feedback_mark_read(conn, update, admin_id, item, toast, lang)
    elif prefix == "fb_log":
        await toast()
        await _feedback_send_log(conn, update.effective_message, admin_id, item, lang)
