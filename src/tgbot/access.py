import sqlite3
from collections.abc import Iterable

from . import db, i18n


def is_private(update) -> bool:
    chat = getattr(update, "effective_chat", None)
    return chat is not None and chat.type == "private"


class Access:
    def __init__(self, conn: sqlite3.Connection, admin_user_ids: Iterable[int],
                 owner_only: bool = False):
        self.conn = conn
        self.admin_user_ids = frozenset(admin_user_ids)
        # Бот только для админов (TGBOT_OWNER_ONLY): allowlist не действует,
        # чужим - молчание, без отказа и заявок.
        self.owner_only = owner_only
        self._refused: set[int] = set()

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_user_ids

    def is_allowed(self, user_id: int) -> bool:
        return self.is_admin(user_id) or (not self.owner_only and db.is_allowed(self.conn, user_id))

    def should_refuse(self, user_id: int) -> bool:
        """True - ровно один раз на пользователя за время работы процесса.

        Текст отказа рендерит вызывающий (`gate`) на языке получателя -
        здесь остаётся только неязыковая часть решения "показывать или нет".
        """
        if self.owner_only or user_id in self._refused:
            return False
        self._refused.add(user_id)
        return True


async def gate(update, access: Access) -> bool:
    """True - сообщение можно обрабатывать дальше."""
    if not is_private(update):
        return False
    user_id = update.effective_user.id
    language_code = getattr(update.effective_user, "language_code", None)
    db.remember_user(
        access.conn,
        user_id,
        getattr(update.effective_user, "username", None),
        lang=i18n.detect(language_code),
    )
    if access.is_allowed(user_id):
        return True
    show = access.should_refuse(user_id)
    db.log_event(access.conn, user_id, "access_denied", {"replied": show})
    if show:
        lang = i18n.lang_for(access.conn, user_id, language_code)
        await update.effective_message.reply_text(i18n.t(lang, "REFUSAL"))
    return False
