"""Переход из Справки в режим по кнопке `mode:go:` под её ответом.
Предложения - `tgbot.mode_switch`, реестр режимов - `handlers.registry`."""

import contextlib

from .. import db, i18n, mode_switch, utils
from ..access import gate
from . import registry
from .deps import get_deps, user_lang


async def mode_callback(update, context):
    """Кнопка предложения перехода: `mode:go:<токен>`."""
    deps = get_deps(context)
    conn = deps.conn
    query = update.callback_query
    payload = getattr(query, "data", None) or ""
    await utils.safe_answer(query)
    if not await gate(update, deps.access):
        return
    user_id = update.effective_user.id
    lang = user_lang(conn, update)
    message = update.effective_message
    T = i18n.texts(lang)
    token = payload.removeprefix(f"{mode_switch.PREFIX}go:")
    offer = deps.switches.take(user_id, token)
    if isinstance(offer, str):
        await message.reply_text(
            T.MODE_SWITCH_STALE if offer == mode_switch.STALE else T.MODE_SWITCH_LOST
        )
        with contextlib.suppress(Exception):  # клавиатуры уже может не быть
            await message.edit_reply_markup(reply_markup=None)
        return
    db.log_event(conn, user_id, "mode_switch_chosen", {
        "from": offer.current, "to": offer.target,
    })
    await utils.mark_chosen(message, mode_switch.go_label(lang, offer), lang)
    # Вход в режим закрывает прежнюю сессию режима; то же сообщение - первым шагом.
    await registry.by_mode()[offer.target].enter(deps, message, lang, user_id, offer.text)
