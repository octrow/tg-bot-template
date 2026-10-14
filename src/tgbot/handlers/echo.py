"""Демо-режим `/echo` - образец режима: скопируйте этот файл для нового режима.

Вход (`/echo` или кнопка перехода под ответом Справки) открывает сессию режима;
каждая пачка сообщений возвращается эхом с шапкой и номером шага; кнопка
"Выйти в Справку" и вход в другой режим сессию закрывают. Свободный текст сюда
направляет `handlers.reference.fallback` по режиму открытой сессии.

Новый режим: этот модуль под новым именем со своим `SPEC` (режим, критерий
классификатора, команда, префикс кнопок, вход и шаг), строка в
`handlers.registry.MODES` и тексты в `locales` (`MODE_HEADERS`, `MODE_GO`,
`MODE_STAGES`, описание команды в `CMD_DESCRIPTIONS`, свои строки режима).
Команду, кнопки, меню и метку классификатора `bot` и Справка берут из реестра.
"""

import contextlib

from .. import db, i18n, modes, utils
from ..access import gate
from ..modes import Mode
from .deps import ModeSpec, get_deps, user_lang

MODE = "echo"
EXIT = "echo:exit"


def _rows(lang: str) -> list[tuple[str, str]]:
    return [(i18n.t(lang, "ECHO_EXIT_BUTTON"), EXIT)]


async def text_step(deps, message, lang: str, session: dict, text: str) -> None:
    """Шаг режима: эхо текста пачки; номер шага живёт в сессии.

    Сессию могли закрыть, пока шаг шёл (кнопка выхода, вход в другой режим):
    тогда шаг молча ничего не делает - ни эха, ни записи, сессия не оживает."""
    conn = deps.conn
    session = {**session, "step": session["step"] + 1}
    if not db.advance_session(conn, session):
        return
    db.log_event(conn, session["user_id"], "echo_step", {"step": session["step"]})
    modes.enter(MODE, "echo_step", n=session["step"])
    await message.reply_text(text, reply_markup=utils.markup(_rows(lang)))


async def enter(deps, message, lang: str, user_id: int, text: str | None = None) -> None:
    """Вход в режим; `text` - сообщение, с которым пришли из Справки."""
    conn = deps.conn
    session = db.open_mode_session(conn, user_id, MODE)
    db.log_event(conn, user_id, "echo_entered", {"session_id": session["id"]})
    modes.enter(MODE)
    await message.reply_text(i18n.t(lang, "ECHO_HINT"), reply_markup=utils.markup(_rows(lang)))
    if text:
        await text_step(deps, message, lang, session, text)


async def echo(update, context):
    deps = get_deps(context)
    if not await gate(update, deps.access):
        return
    user_id = update.effective_user.id
    db.log_event(deps.conn, user_id, "command", {"command": "/echo"})
    await enter(deps, update.effective_message, user_lang(deps.conn, update), user_id)


async def echo_callback(update, context):
    """Кнопка "Выйти в Справку"; у устаревшей (режим уже закрыт) снимается
    клавиатура, текстом бот не отвечает."""
    deps = get_deps(context)
    conn = deps.conn
    await utils.safe_answer(update.callback_query)
    if not await gate(update, deps.access):
        return
    user_id = update.effective_user.id
    lang = user_lang(conn, update)
    session = db.active_session(conn, user_id)
    if session is None or session["mode"] != MODE:
        with contextlib.suppress(Exception):  # клавиатуры уже может не быть
            await update.effective_message.edit_reply_markup(reply_markup=None)
        return
    db.close_mode_session(conn, user_id)
    db.log_event(conn, user_id, "echo_left", {"steps": session["step"]})
    message = update.effective_message
    await utils.mark_chosen(message, i18n.t(lang, "ECHO_EXIT_BUTTON"), lang)
    modes.enter(Mode.REFERENCE)
    await message.reply_text(i18n.t(lang, "ECHO_LEFT"))


SPEC = ModeSpec(
    mode=MODE,
    intent="An explicit request to repeat or echo the user's own words back",
    command="echo",
    callback_prefix="echo:",
    command_handler=echo,
    callback_handler=echo_callback,
    enter=enter,
    text=text_step,
)
