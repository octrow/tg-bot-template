"""Отзыв: /support от пользователя и ежедневное напоминание админам о
непрочитанных отзывах."""

import asyncio
import logging
from datetime import datetime, timedelta, UTC
from zoneinfo import ZoneInfo

from .. import db, i18n
from ..access import gate
from ..config import REMINDER_HOUR_DEFAULT, REMINDER_TZ_DEFAULT
from .deps import get_deps, user_lang

logger = logging.getLogger(__name__)


async def support(update, context):
    """Отзыв о работе бота одним сообщением: `/support <текст>` (спека
    user-feedback)."""
    deps = get_deps(context)
    if not await gate(update, deps.access):
        return
    conn = deps.conn
    user_id = update.effective_user.id
    lang = user_lang(conn, update)
    text = " ".join(getattr(context, "args", None) or []).strip()
    if not text:
        await update.effective_message.reply_text(i18n.t(lang, "SUPPORT_USAGE"))
        return
    # Привязка фиксируется в момент отправки: жалоба - про то, что
    # происходило сейчас, а не про сессию на момент чтения админом.
    session_id = db.last_session_id(conn, user_id)
    feedback_id = db.add_feedback(conn, user_id, session_id, text)
    db.log_event(
        conn, user_id, "feedback_received",
        {"feedback_id": feedback_id, "session_id": session_id},
    )
    await update.effective_message.reply_text(i18n.t(lang, "SUPPORT_SAVED"))


REMINDER_TZ = REMINDER_TZ_DEFAULT
REMINDER_HOUR = REMINDER_HOUR_DEFAULT


def next_reminder_at(now: datetime, *, tz: ZoneInfo = REMINDER_TZ, hour: int = REMINDER_HOUR) -> datetime:
    """Следующее пн-пт `hour`:00 в поясе `tz` строго после `now` (aware).

    Пояс и час - из настроек (`TGBOT_REMINDER_TZ`, `TGBOT_REMINDER_HOUR`).
    Дни прибавляются по местным часам: перевод часов тик не сдвигает."""
    local = now.astimezone(tz)
    candidate = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if local >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:  # 5, 6 - суббота, воскресенье
        candidate += timedelta(days=1)
    return candidate


async def send_feedback_reminder(bot, conn, admin_user_ids) -> None:
    """Один тик напоминания: при непрочитанных отзывах шлёт счётчик каждому
    админу на его языке; при нуле молчит (спека user-feedback). Ошибка
    доставки одному админу не лишает остальных."""
    unread = db.unread_feedback_count(conn)
    if unread == 0:
        return
    delivered = []
    for admin_id in sorted(admin_user_ids):
        try:
            lang = i18n.lang_for(conn, admin_id)
            await bot.send_message(admin_id, i18n.t(lang, "FEEDBACK_REMINDER", n=unread))
            delivered.append(admin_id)
        except Exception:
            logger.warning(
                "Не удалось отправить напоминание об отзывах админу %s",
                admin_id,
                exc_info=True,
            )
    db.log_event(
        conn, None, "feedback_reminder", {"unread": unread, "admins": delivered}
    )


async def feedback_reminder_loop(
    bot, conn, admin_user_ids, *, tz: ZoneInfo = REMINDER_TZ, hour: int = REMINDER_HOUR
) -> None:
    """Вечный цикл напоминаний: сон до следующего пн-пт `hour`:00, тик, снова сон.

    Фоновая asyncio-задача вместо JobQueue: extra `[job-queue]` не установлен,
    а APScheduler ради одного ежедневного тика - лишняя зависимость (design,
    решение 1). Условие пересмотра: второе-третье расписание в боте."""

    while True:
        target = next_reminder_at(datetime.now(UTC), tz=tz, hour=hour)
        # Вычитание через UTC: у двух дат одного пояса Python вычитает
        # показания часов и теряет час в ночь перевода.
        delay = (target - datetime.now(UTC)).total_seconds()
        await asyncio.sleep(max(delay, 0))
        try:
            await send_feedback_reminder(bot, conn, admin_user_ids)
        except Exception:  # тик не должен убивать цикл до конца жизни процесса
            logger.warning("Тик напоминания об отзывах упал", exc_info=True)
