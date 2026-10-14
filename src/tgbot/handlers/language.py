"""Язык интерфейса: /language, кнопки `lang:` и предложение сменить язык
по языку сообщения."""

import logging
from datetime import datetime, timedelta, UTC

from .. import db, i18n, utils
from ..access import is_private
from .common import apply_menu
from .deps import get_deps, user_lang

logger = logging.getLogger(__name__)

# Предложение сменить язык по языку сообщения - не чаще (спека localization).
LANGUAGE_OFFER_EVERY = timedelta(days=30)


async def language(update, context):
    # Как /whoami: доступна и не-allowlist пользователю, сессию не трогает.
    if not is_private(update):
        return
    conn = get_deps(context).conn
    user_id = update.effective_user.id
    db.remember_user(conn, user_id, getattr(update.effective_user, "username", None))
    db.log_event(conn, user_id, "command", {"command": "/language"})
    lang = user_lang(conn, update)
    T = i18n.texts(lang)
    rows = [
        (
            T.LANGUAGE_CURRENT_MARK.format(name=T.LANGUAGE_NAMES[code])
            if code == lang
            else T.LANGUAGE_NAMES[code],
            f"lang:{code}",
        )
        for code in i18n.LANGS
    ]
    await update.effective_message.reply_text(
        T.LANGUAGE_PROMPT, reply_markup=utils.markup(rows)
    )


async def language_callback(update, context):
    deps = get_deps(context)
    query = update.callback_query
    payload = getattr(query, "data", None) or ""
    await utils.safe_answer(query)
    _, _, code = payload.partition(":")
    if code not in i18n.LANGS:
        return
    user_id = update.effective_user.id
    db.set_lang(deps.conn, user_id, code)
    db.log_event(deps.conn, user_id, "language_changed", {"lang": code})
    await apply_menu(deps, context.bot, user_id)
    # Выбор своего же языка - не ошибка: то же подтверждение, без смены состояния.
    await update.effective_message.reply_text(i18n.t(code, "LANGUAGE_SET"))


async def offer_language(conn, update, user_id: int, text: str) -> None:
    """Пишет не на языке интерфейса - двуязычное предложение переключиться,
    не чаще `LANGUAGE_OFFER_EVERY` (спека localization). Кнопки -
    те же `lang:<code>`, что у /language. Сбой не мешает ответу на текст."""
    written = i18n.text_lang(text)
    lang = user_lang(conn, update)
    if written is None or written == lang:
        return
    last = db.last_event_ts(conn, user_id, "language_offer")
    if last and datetime.now(UTC) - datetime.fromisoformat(last) < LANGUAGE_OFFER_EVERY:
        return
    T = i18n.texts(lang)
    try:
        await update.effective_message.reply_text(
            T.LANGUAGE_OFFER[written].format(days=LANGUAGE_OFFER_EVERY.days),
            reply_markup=utils.markup([
                (T.LANGUAGE_OFFER_SWITCH[written], f"lang:{written}"),
                (T.LANGUAGE_OFFER_KEEP[lang], f"lang:{lang}"),
            ]),
        )
    except Exception:
        logger.exception("Предложение сменить язык не отправлено пользователю %s", user_id)
        return
    db.log_event(conn, user_id, "language_offer", {"from": lang, "to": written})
