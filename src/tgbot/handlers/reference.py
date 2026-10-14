"""Справка - режим по умолчанию: свободный текст (пачки, скриншоты), ответ
модели, кнопки `ref:` под ответом, повтор пачки `pack:redo:` и запись каждого
апдейта в журнал (файл сессии Справки выбирается до записи)."""

import logging

from .. import db, i18n, intent, mode_switch, modes, packs, reference_mode, utils
from ..access import gate, is_private
from ..agent.ladder import call_agent
from ..agent.result import is_unverified
from ..modes import Mode
from . import registry
from .access import submit_access
from .common import not_a_question
from .deps import get_deps, user_lang
from .language import offer_language

logger = logging.getLogger(__name__)


# --- Справка (спека reference-mode) -------------------------------

def _reference_rows(
    lang: str, attempt: int, token: str | None, offer=None
) -> list[tuple[str, str]]:
    """Кнопки под ответом Справки: оценка и переход в режим, о котором
    просил вопрос (`offer`). Номер попытки - в payload: по нему устаревшее
    нажатие не запускает лишний повтор, а журнал знает, какой ответ оценён.
    Метка диалога (`token`) - там же: кнопка старого диалога не трогает новый.
    `token=None` - ответ уже не к текущему диалогу, оценивать нечего."""
    # "Понятно" убрано (clearer-bot-output): оно ничего не меняло.
    rows = []
    if token is not None:
        rows.append((i18n.texts(lang).REFERENCE_UNCLEAR, f"ref:bad:{attempt}:{token}"))
    if offer is not None:
        rows.append((mode_switch.go_label(lang, offer), f"{mode_switch.PREFIX}go:{offer.token}"))
    return rows


async def _reference_call(
    deps, message, lang: str, user_id: int, prompt: str, status_key: str = "STATUS_REFERENCE",
    question: str = "",
):
    """Вызов модели в окне статуса с таймером - тот же порядок, что у
    обычного вопроса: react -> статус -> клон -> вызов. `status_key` -
    свой текст у повторного ответа: человек должен видеть, что нажатие
    "Не понятно" принято (спека reference-mode)."""
    await utils.react(message)
    async with utils.waiting(message, i18n.t(lang, status_key), lang) as wait:
        if deps.config is not None:
            from ..repo import ensure_repo_async

            await ensure_repo_async(deps.config, deps.conn)
        return await call_agent(
            deps.config, deps.conn, prompt, user_id, reference_mode.REFERENCE_CONTEXT,
            message=message, lang=lang, on_step=wait.on_step, question=question,
        )


async def _reference_send(
    deps, message, lang: str, user_id: int, answer, attempt: int, token: str | None,
    light: bool = False, offer=None,
) -> str | None:
    """Показывает ответ; возвращает этот текст для памяти диалога. None -
    модель не ответила: текст сбоя с кодом уже отправил `call_agent`.
    `light` - облегчённый ответ о боте: без метки "не подтверждено"."""
    if answer is None:
        return None
    # До приклейки служебных строк: делится только текст модели. В память
    # диалога уходит разбитый - человек видел именно его.
    text = utils.tidy_model_text(str(answer).rstrip(), lang)
    utils.log_long_block(text, "reference")
    shown = text
    if not light and is_unverified(answer):
        shown = f"{text}\n\n{i18n.t(lang, 'REFERENCE_UNVERIFIED')}"
    # Ответ целиком: из кэша он приходит без `llm_response` с текстом.
    db.log_event(
        deps.conn, user_id, "reference_answer", {"text": shown, "attempt": attempt}
    )
    await utils.reply(
        message, utils.md_to_html(shown), _reference_rows(lang, attempt, token, offer)
    )
    return text


async def _classified(fast) -> str | None:
    """Итог классификатора. Его сбой не должен стоить ответа: тогда
    обычный путь."""
    try:
        return await fast
    except Exception:  # noqa: BLE001 - предложение лишь подсказка
        logger.warning("классификатор намерения упал", exc_info=True)
        return None


def _offer(deps, user_id: int, track: str | None, question: str):
    """Предложение перейти в режим, если классификатор узнал просьбу о нём:
    кнопка встаёт под ответом, а не вместо него."""
    if track not in registry.by_mode():
        return None
    offer = deps.switches.offer(
        user_id, current=Mode.REFERENCE.value, target=track, text=question
    )
    db.log_event(deps.conn, user_id, "mode_switch_offered", {
        "from": offer.current, "to": offer.target, "text": question[:intent.INTENT_MAX_CHARS],
    })
    return offer


async def _reference_answer(deps, message, lang: str, user_id: int, question: str) -> None:
    """Один ответ Справки: журнал, классификация, промпт, вызов, отправка,
    память. `lang` - язык интерфейса; отвечаем на языке вопроса, если он явно
    другой (reference-language-and-grounding), язык пользователя не меняется."""
    conn = deps.conn
    lang = i18n.text_lang(question) or lang
    modes.enter(Mode.REFERENCE)
    history = deps.dialogs.history(user_id)
    # Режим по умолчанию - свой файл журнала.
    db.reference_log_session(conn, user_id, new=_reference_dialog_expired(deps, user_id))
    db.log_event(
        conn, user_id, "reference_question",
        {"text": question, "length": len(question), "has_history": bool(history)},
    )
    try:
        # Классификация до выбора пути (reference-language-and-grounding):
        # её потолок `intent.INTENT_TIMEOUT_S`, обычно 0,5 с, а без ключа -
        # ни секунды. Болтовня идёт облегчённым путём.
        await utils.react(message)
        track = await _classified(intent.classify(
            conn, user_id, getattr(deps.config, "typesafe_api_key", None), question,
            registry.intents(),
        ))
        await _answer_by_track(deps, message, lang, user_id, question, history, track)
    finally:
        deps.reference_opened.discard(user_id)


async def _answer_by_track(deps, message, lang, user_id, question, history, track) -> None:
    """Болтовня - облегчённым путём, остальное (и болтовня без ответа) -
    полным; ответ, кнопки и память диалога."""
    product = getattr(deps.config, "product_name", None)
    answer, route = None, "full"
    if track == intent.CHAT:
        answer = await _reference_light(deps, message, lang, user_id, question, history)
        route = "chat" if answer is not None else "chat_fallback"
    db.log_event(deps.conn, user_id, "reference_route", {"route": route})
    if answer is None:
        prompt = reference_mode.prompt(question, history, lang=lang, product=product)
        answer = await _reference_call(deps, message, lang, user_id, prompt, question=question)
    token = reference_mode.new_token()
    text = await _reference_send(
        deps, message, lang, user_id, answer, reference_mode.FIRST_ATTEMPT, token,
        light=route == "chat",
        offer=_offer(deps, user_id, track, question) if answer is not None else None,
    )
    if text is not None:
        deps.dialogs.remember(user_id, question, text, token)


async def _reference_light(deps, message, lang: str, user_id: int, question: str, history):
    """Болтовня и вопросы о боте - без инструментов и статуса поиска. None -
    модель вернула маркер или не ответила: тогда обычный путь."""
    product = getattr(deps.config, "product_name", None)
    await utils.typing(message)
    answer = await call_agent(
        deps.config, deps.conn,
        reference_mode.chat_prompt(question, history, lang=lang, product=product),
        user_id,
        reference_mode.CHAT_CONTEXT, message=message, light=True, lang=lang,
        notify=False, question=question,
    )
    if answer is None or reference_mode.needs_sources(answer):
        return None
    return answer


def _reference_dialog_expired(deps, user_id: int) -> bool:
    """Новый диалог Справки - новая сессия журнала, если её ещё не открыл
    входящий апдейт этого же вопроса."""
    return deps.dialogs.get(user_id) is None and user_id not in deps.reference_opened


def _is_reference_text(deps, update) -> bool:
    """Свободный текст в личке от пользователя с доступом, не команда и не
    в другом режиме - кандидат в Справку (и в предложение сменить язык)."""
    user = getattr(update, "effective_user", None)
    text = packs.text_of(update)
    return bool(
        user is not None and is_private(update) and text
        and not text.startswith("/") and deps.access.is_allowed(user.id)
        and db.active_session(deps.conn, user.id) is None
    )


def _open_reference_log(deps, update) -> None:
    """Файл сессии Справки выбирается до записи входящего апдейта: иначе
    апдейт, реакция и подсказка о языке уходят в общий файл
    (session-log-completeness). Условие - как в `_reference_answer`."""
    if getattr(update, "callback_query", None) is not None or not _is_reference_text(deps, update):
        return
    user_id = update.effective_user.id
    db.reference_log_session(deps.conn, user_id, new=_reference_dialog_expired(deps, user_id))
    deps.reference_opened.add(user_id)


async def log_update(update, context):
    """Каждый апдейт целиком в файловый журнал - до фильтров и проверки
    доступа, из любого типа чата (full-traffic-logging). Сбой записи не
    мешает обработке: журнал вторичен."""
    try:
        deps = get_deps(context)
        _open_reference_log(deps, update)
        user, chat = update.effective_user, update.effective_chat
        db.log_file_event(deps.conn, user.id if user else None, "tg_update", {
            "chat_type": chat.type if chat else None,
            "update": update.to_dict(),
        })
    except Exception as exc:  # noqa: BLE001 - журнал не роняет апдейт
        logger.warning("tg_update log failed: %s", exc)


async def _reference(deps, update, lang: str, user_id: int) -> bool:
    """Свободный текст - Справке. True - ответили. Не текст (стикер) и
    опечатка в команде остаются `fallback` - там у них свои ответы."""
    message = update.effective_message
    question = (getattr(message, "text", None) or "").strip()
    if not question or question.startswith("/"):
        return False
    await _reference_answer(deps, message, lang, user_id, question)
    return True


async def _mode_text(deps, update, lang: str, user_id: int) -> bool:
    """Текст режиму открытой сессии (`handlers.registry`). True - ответил режим."""
    session = db.active_session(deps.conn, user_id)
    spec = registry.by_mode().get(session["mode"]) if session is not None else None
    text = packs.text_of(update)
    if spec is None or not text or text.startswith("/"):
        return False
    await spec.text(deps, update.effective_message, lang, session, text)
    return True


async def command_reset(update, context):
    """Любая команда отбрасывает открытую пачку сообщений - с одной
    строкой об этом (спека message-packs)."""
    deps = get_deps(context)
    user = getattr(update, "effective_user", None)
    if getattr(update, "callback_query", None) is not None:
        return  # у кнопки в effective_message текст бота, а не команда
    text = (getattr(update.effective_message, "text", None) or "").strip()
    if user is None or not text.startswith("/"):
        return
    if deps.packs is not None and deps.packs.drop(user.id):
        await update.effective_message.reply_text(
            i18n.t(user_lang(deps.conn, update), "PACK_DROPPED")
        )


async def _reference_retry(deps, message, lang: str, user_id: int, token: str) -> None:
    """"Не понятно" под первым ответом: один повтор проще и с перепроверкой.

    Диалог берётся по метке кнопки и держится объектом через await: пока
    модель думает, новый вопрос заменяет диалог, и тогда ответ повтора
    отправляется без кнопки оценки, а новый диалог не меняется."""
    state = deps.dialogs.current(user_id, token)
    if state is None or state.attempt != reference_mode.FIRST_ATTEMPT or state.retrying:
        return  # устаревшее нажатие: повторять нечего, уже повторили или повтор готовится
    lang = i18n.text_lang(state.question) or lang  # как у первого ответа
    prompt = reference_mode.retry_prompt(
        state.question, state.first_answer, lang=lang,
        product=getattr(deps.config, "product_name", None),
    )
    # Флаг до первого await: попытка меняется только с ответом повтора. Сбой
    # повтора флаг снимает - следующее нажатие пробует снова.
    state.retrying = True
    try:
        answer = await _reference_call(
            deps, message, lang, user_id, prompt, "STATUS_REFERENCE_RETRY", question=state.question
        )
    finally:
        state.retrying = False
    still_current = deps.dialogs.get(user_id) is state
    text = await _reference_send(
        deps, message, lang, user_id, answer, reference_mode.RETRY_ATTEMPT,
        state.token if still_current else None,
    )
    if text is not None:
        deps.dialogs.remember_retry(user_id, text, state)


async def _reference_handoff(deps, message, lang: str, user_id: int, token: str) -> None:
    """Вопрос, который Справка не смогла объяснить, - людям, Отзывом."""
    conn = deps.conn
    state = deps.dialogs.current(user_id, token)
    if state is None or state.attempt != reference_mode.RETRY_ATTEMPT:
        return
    text = i18n.t(
        lang, "REFERENCE_FEEDBACK_TAG",
        question=state.question,
        first_answer=state.first_answer,
        second_answer=state.pairs[-1][1],
    )
    session_id = db.last_session_id(conn, user_id)
    feedback_id = db.add_feedback(conn, user_id, session_id, text)
    db.log_event(conn, user_id, "reference_handoff", {"feedback_id": feedback_id})
    deps.dialogs.drop(user_id)
    await message.reply_text(i18n.t(lang, "REFERENCE_HUMAN_SENT"))


async def reference_callback(update, context):
    """Кнопки под ответом Справки: оценка, передача людям."""
    deps = get_deps(context)
    query = update.callback_query
    payload = getattr(query, "data", None) or ""
    await utils.safe_answer(query)
    if not await gate(update, deps.access):
        return
    user_id = update.effective_user.id
    lang = user_lang(deps.conn, update)
    message = update.effective_message
    T = i18n.texts(lang)
    # Кнопки несут метку диалога (`ref:bad:<попытка>:<метка>`, `ref:human:<метка>`);
    # кнопка без метки или с меткой старого диалога - устаревшая.
    if payload == "ref:human" or payload.startswith("ref:human:"):
        await utils.mark_chosen(message, T.REFERENCE_HUMAN, lang)
        await _reference_handoff(deps, message, lang, user_id, payload[len("ref:human:"):])
        return
    # ponytail: удалить "ok" после 2026-11-06 - кнопка "Понятно" убрана
    # (clearer-bot-output), обработчик живёт ради старых сообщений.
    verdict, _, rest = payload.partition(":")[2].partition(":")
    raw_attempt, _, token = rest.partition(":")
    if verdict not in ("ok", "bad") or not raw_attempt.isdigit():
        return
    attempt = int(raw_attempt)
    clear = verdict == "ok"
    db.log_event(deps.conn, user_id, "reference_rated", {"attempt": attempt, "clear": clear})
    await utils.mark_chosen(
        message, T.REFERENCE_CLEAR if clear else T.REFERENCE_UNCLEAR, lang
    )
    if clear:
        return
    if attempt == reference_mode.FIRST_ATTEMPT:
        await _reference_retry(deps, message, lang, user_id, token)
        return
    if deps.dialogs.current(user_id, token) is None:
        return  # кнопка старого диалога: людям передавать уже нечего
    # Второе "Не понятно": третьего круга нет - предлагаем людей.
    await utils.reply(
        message, i18n.t(lang, "REFERENCE_HUMAN_PROMPT"),
        [(T.REFERENCE_HUMAN, f"ref:human:{token}")],
    )


async def fallback(update, context):
    """Свободный текст: у не-allowlist пользователя с черновиком заявки
    это ответ на вопрос о ФИО/причине (до отказа gate); у допущенного -
    Справка; остальным - отказ gate или подсказка (`not_a_question`).
    Пачка сообщений (`tgbot.packs`) отдаёт сюда склеенный текст."""
    deps = get_deps(context)
    conn, access = deps.conn, deps.access
    user = getattr(update, "effective_user", None)
    if is_private(update) and user is not None and not access.is_allowed(user.id):
        req = db.open_access_request(conn, user.id)
        if req is not None and req["status"] == "draft":
            text = (getattr(update.effective_message, "text", None) or "").strip()
            await submit_access(update, context, req, text or None)
            return
    if is_private(update) and user is not None and access.is_allowed(user.id):
        lang = user_lang(conn, update)
        # Новое сообщение заменяет ожидающее предложение перехода.
        deps.switches.drop(user.id)
        # Открыта сессия режима - текст ему, иначе Справке (спека reference-mode).
        if (
            await _mode_text(deps, update, lang, user.id)
            or await _reference(deps, update, lang, user.id)
        ):
            return
    await not_a_question(deps, update)


async def packed_fallback(update, context):
    """Вход свободного сообщения: текст допущенного - через пачку, остальное
    (чужие, группы, не текст, опечатка в команде) - сразу в `fallback`.
    Свободный текст - Справке; исключение - черновик заявки на доступ."""
    deps = get_deps(context)
    user = getattr(update, "effective_user", None)
    text = packs.text_of(update)
    if _is_reference_text(deps, update):
        await offer_language(deps.conn, update, user.id, text)
    if (
        deps.packs is None or user is None or not is_private(update)
        or not deps.access.is_allowed(user.id) or not text or text.startswith("/")
    ):
        await fallback(update, context)
        return
    await deps.packs.add(update, context)


async def pack_callback(update, context):
    """Кнопка "Ответить с учётом новых" под пометкой о поздних сообщениях."""
    deps = get_deps(context)
    if deps.packs is None or not await gate(update, deps.access):
        await utils.safe_answer(update.callback_query)
        return
    await deps.packs.redo(update, context)


async def image(update, context):
    """Картинка распознаётся в текст и идёт тем же входом, что текст
    (`packed_fallback`)."""
    await get_deps(context).images.handle(update, context)
