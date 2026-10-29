"""Доступ: /whoami, /request_access и кнопки заявки `access:` (заявитель
и админы). Сама проверка allowlist - `tgbot.access.gate`."""

import logging

from .. import db, i18n, utils
from ..access import is_private
from .common import apply_menu
from .deps import get_deps, user_lang

logger = logging.getLogger(__name__)


async def whoami(update, context):
    # Единственная команда до проверки allowlist.
    if not is_private(update):
        return
    conn = get_deps(context).conn
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    db.remember_user(conn, user_id, getattr(update.effective_user, "username", None))
    db.log_event(conn, user_id, "command", {"command": "/whoami"})
    lang = user_lang(conn, update)
    await update.effective_message.reply_text(
        i18n.t(lang, "WHOAMI", user_id=user_id, chat_id=chat_id)
    )


async def request_access(update, context):
    # Вторая (после /whoami) команда до проверки allowlist (спека
    # access-request): заявку подаёт тот, кого gate не пускает.
    deps = get_deps(context)
    if not is_private(update) or deps.access.owner_only:
        return
    conn = deps.conn
    user_id = update.effective_user.id
    db.remember_user(conn, user_id, getattr(update.effective_user, "username", None))
    lang = user_lang(conn, update)
    T = i18n.texts(lang)
    db.log_event(conn, user_id, "command", {"command": "/request_access"})
    if deps.access.is_allowed(user_id):
        await update.effective_message.reply_text(T.ACCESS_ALREADY)
        return
    req = db.open_access_request(conn, user_id)
    if req is not None and req["status"] == "pending":
        await update.effective_message.reply_text(T.ACCESS_PENDING)
        return
    # Брошенный черновик переиспользуем: вопрос просто задаётся заново.
    request_id = req["id"] if req is not None else db.create_access_draft(conn, user_id)
    await update.effective_message.reply_text(
        T.ACCESS_QUESTION,
        reply_markup=utils.markup([(T.ACCESS_SKIP_BUTTON, f"access:skip:{request_id}")]),
    )


def _access_card(conn, req: dict, user, lang: str) -> str:
    """Карточка заявки на языке получателя: профиль Telegram + история
    контактов из БД."""
    T = i18n.texts(lang)
    username = getattr(user, "username", None)
    full_name = " ".join(
        part
        for part in (getattr(user, "first_name", None), getattr(user, "last_name", None))
        if part
    )
    full_name = full_name or "-"
    language = getattr(user, "language_code", None) or "-"
    history = db.access_history(conn, req["user_id"])
    return T.ACCESS_CARD.format(
        id=req["id"],
        user_id=req["user_id"],
        username=f" @{username}" if username else "",
        full_name=full_name,
        language=language,
        first_seen=history["first_seen"] or "-",
        denied_count=history["denied_count"],
        past=", ".join(history["past"]) or T.ACCESS_PAST_NONE,
        name_reason=req["name_reason"] or T.ACCESS_NAME_SKIPPED,
    )


async def _push_card_to(conn, context, admin_id: int, req: dict, user) -> dict[str, int] | None:
    """Карточка одному админу на его языке; None - не дошла (это в журнале)."""
    lang = i18n.lang_for(conn, admin_id)
    T = i18n.texts(lang)
    card = _access_card(conn, req, user, lang)
    keyboard = utils.markup(
        [
            (T.ACCESS_APPROVE_BUTTON, f"access:approve:{req['id']}"),
            (T.ACCESS_DENY_BUTTON, f"access:deny:{req['id']}"),
        ]
    )
    try:
        message = await context.bot.send_message(admin_id, card, reply_markup=keyboard)
    except Exception:
        logger.warning(
            "Не удалось отправить заявку на доступ админу %s",
            admin_id,
            exc_info=True,
        )
        db.log_event(
            conn,
            req["user_id"],
            "access_push_failed",
            {"request_id": req["id"], "admin_id": admin_id},
        )
        return None
    # Текст запоминаем рядом с пушем: после решения его надо
    # дописать вердиктом ровно на том языке, на котором он ушёл.
    return {"chat_id": admin_id, "message_id": message.message_id, "card": card}


async def _push_access_card(deps, context, req: dict, user) -> tuple[list[dict[str, int]], str]:
    """Карточка каждому админу на его языке; недоставка одному не лишает
    остальных (как напоминания об отзывах). Возвращает, куда дошли пуши,
    и карточку на языке по умолчанию - её же сохраняем в БД записью
    заявки (`db.set_access_messages` хранит один текст на запись)."""
    sent = []
    for admin_id in sorted(deps.access.admin_user_ids):
        pushed = await _push_card_to(deps.conn, context, admin_id, req, user)
        if pushed is not None:
            sent.append(pushed)
    return sent, _access_card(deps.conn, req, user, i18n.DEFAULT_LANG)


async def submit_access(update, context, req: dict, name_reason: str | None) -> None:
    """Подача заявки: draft -> pending, пуш админам, ответ заявителю."""
    deps = get_deps(context)
    conn = deps.conn
    db.submit_access_request(conn, req["id"], name_reason)
    req = db.get_access_request(conn, req["id"]) or req
    sent, default_card = await _push_access_card(deps, context, req, update.effective_user)
    db.set_access_messages(conn, req["id"], default_card, sent)
    # Админ мог решить, пока пуши ещё уходили: его _decide не видел их
    # message_id и ничего не правил - правим здесь.
    # ponytail: при решении ровно между записью и этой проверкой правка
    # пройдёт дважды - второй раз Bot API ответит "not modified" в лог.
    decided = db.get_access_request(conn, req["id"]) or req
    if decided["status"] in ("approved", "denied"):
        await _edit_access_pushes(
            conn, context, decided, decided["decided_by"], decided["status"]
        )
    db.log_event(
        conn,
        req["user_id"],
        "access_request_submitted",
        {
            "request_id": req["id"],
            "with_text": name_reason is not None,
            "admins": [m["chat_id"] for m in sent],
        },
    )
    lang = user_lang(conn, update)
    await update.effective_message.reply_text(i18n.t(lang, "ACCESS_SENT"))


def _parse_payload(payload: str) -> tuple[str, int | None]:
    """`access:<действие>:<id заявки>` -> (действие, id); id None - не число."""
    _, _, rest = payload.partition(":")
    action, _, raw_id = rest.partition(":")
    try:
        return action, int(raw_id)
    except ValueError:
        return action, None


async def access_callback(update, context):
    """Кнопки заявки на доступ: `access:skip` у заявителя,
    `access:approve|deny` у админов. Гонку двух админов решает атомарный
    UPDATE по status='pending' (design, решение 4)."""
    deps = get_deps(context)
    query = update.callback_query

    async def toast(text: str | None = None) -> None:
        await utils.safe_answer(query, text)

    action, request_id = _parse_payload(getattr(query, "data", None) or "")
    req = db.get_access_request(deps.conn, request_id) if request_id is not None else None
    if req is None:
        await toast()
        return
    user_id = update.effective_user.id
    if action == "skip":
        # Только сам заявитель и только по своему черновику.
        if req["user_id"] != user_id or req["status"] != "draft":
            await toast()
            return
        await toast()
        await submit_access(update, context, req, None)
        return
    if action not in ("approve", "deny") or not deps.access.is_admin(user_id):
        await toast()
        return
    await _decide(deps, update, context, req, action, toast)


async def _decide(deps, update, context, req: dict, action: str, toast) -> None:
    """Решение админа по заявке; второе решение той же заявки - только тост."""
    conn = deps.conn
    user_id = update.effective_user.id
    lang = user_lang(conn, update)
    status = "approved" if action == "approve" else "denied"
    if not db.decide_access_request(conn, req["id"], user_id, status):
        await toast(i18n.t(lang, "ACCESS_DECIDED_ALREADY"))
        return
    if status == "approved":
        db.add_user(conn, req["user_id"], user_id)
    db.log_event(
        conn,
        user_id,
        "access_request_decided",
        {"request_id": req["id"], "target_user_id": req["user_id"], "status": status},
    )
    await toast()
    if status == "approved":
        await apply_menu(deps, context.bot, req["user_id"])
    # Решение уже в БД - ошибки правки пушей и уведомления его не отменяют.
    await _edit_access_pushes(conn, context, req, user_id, status)
    await _notify_access_requester(conn, context, req, status)


async def _edit_access_pushes(conn, context, req: dict, admin_id: int, status: str) -> None:
    """Пуши у всех админов: убрать кнопки, дописать кто и что решил -
    каждому на его языке. Берём текст, реально отправленный этому админу
    (`m["card"]`): перерисовывать карточку заново нельзя - живого профиля
    заявителя здесь уже нет. `req["card"]` - фолбэк для пушей, ушедших до
    появления языков."""
    admin_name = db.seen_username(conn, admin_id)
    for m in req["admin_messages"]:
        lang = i18n.lang_for(conn, m["chat_id"])
        T = i18n.texts(lang)
        card = m.get("card") or req.get("card") or ""
        note = T.ACCESS_DECIDED_NOTE.format(
            admin=f"@{admin_name}" if admin_name else admin_id,
            verdict=T.ACCESS_VERDICT_APPROVED
            if status == "approved"
            else T.ACCESS_VERDICT_DENIED,
        )
        try:
            await context.bot.edit_message_text(
                chat_id=m["chat_id"],
                message_id=m["message_id"],
                text=card + note,
            )
        except Exception:
            logger.warning(
                "Не удалось отредактировать пуш заявки у админа %s",
                m["chat_id"],
                exc_info=True,
            )


async def _notify_access_requester(conn, context, req: dict, status: str) -> None:
    """Заявитель узнаёт исход на своём языке; недоставка - в лог и журнал."""
    lang = i18n.lang_for(conn, req["user_id"])
    text = i18n.t(lang, "ACCESS_GRANTED" if status == "approved" else "ACCESS_REJECTED")
    try:
        await context.bot.send_message(req["user_id"], text)
    except Exception:
        logger.warning(
            "Не удалось уведомить заявителя %s о решении",
            req["user_id"],
            exc_info=True,
        )
        db.log_event(
            conn,
            req["user_id"],
            "access_notify_failed",
            {"request_id": req["id"], "status": status},
        )
