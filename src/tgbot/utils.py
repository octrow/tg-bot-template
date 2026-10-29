"""Небизнесовые помощники диалога: кнопки, показ значений, JSON из ответов
агента, журналируемая отправка."""

import asyncio
import collections
import contextlib
import functools
import html
import json
import re

import logging

import razdel
import telegram.error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import MessageLimit
from telegram.ext import ExtBot
from telegram.request import HTTPXRequest

from . import db, i18n, logging_setup, modes

log = logging.getLogger(__name__)


def esc(value) -> str:
    """Экранирование динамики (ответы пользователя, текст агента) для HTML."""
    return html.escape(str(value), quote=False)


def strip_tags(text: str) -> str:
    """Убирает HTML-теги - фолбэк, когда parse_mode="HTML" не разобрался."""
    return re.sub(r"<[^>]+>", "", text)


# Разметка, которую языковые модели ставят в ответ, не спрашивая: тройные
# кавычки, одинарные, `**жирный**`, заголовки, пункты, цитаты и `*курсив*`.
# Порядок применения важен - код ищется ПЕРВЫМ и прячется в заглушку, иначе
# его содержимое разберут как разметку. Регулярки работают по уже
# экранированному тексту: цитата - это `&gt; `.
_MD_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
_MD_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
# Заголовок со спрятанным кодом не берём: `<b><pre>` Telegram не примет.
_MD_HEADING_RE = re.compile(r"^#{1,3} +([^\x00\n]+?)\s*$", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^(\s*)[-*] +", re.MULTILINE)
_MD_QUOTE_RE = re.compile(r"(?:^&gt; ?.*(?:\n|$))+", re.MULTILINE)
# `_x_` курсивом не считаем: подчёркивания есть в `call_id`, `snake_case`.
# После открывающей звёздочки - буква или цифра: `*.py`, `src/*/x` не курсив.
_MD_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=[^\W_])([^*\n]+?)(?<=\S)\*(?![\w*])")
_HELD = "\x00"
_HELD_RE = re.compile(rf"{_HELD}(\d+){_HELD}")


def md_to_html(text: str, quotes: bool = True) -> str:
    """Markdown из ответа модели -> Telegram-HTML, с экранированием остального.

    Просить модель "сразу писать HTML" недостаточно: она всё равно ставит
    обратные кавычки, и человек видит спецзнаки вместо форматирования (живая
    проверка в исходном боте). Конвертируем ровно три формы, которые модели ставят
    реально; всё прочее едет экранированным текстом - тег из недоверенного
    ответа обязан быть видимым, а не исполненным, иначе один сломанный `<`
    роняет разбор всего сообщения.

    `quotes=False` оставляет строки `> ` текстом: текст, который сам едет
    цитатой, не может содержать цитату - Telegram вложенных не принимает.
    """
    held: list[str] = []

    def hold(markup: str) -> str:
        held.append(markup)
        return f"{_HELD}{len(held) - 1}{_HELD}"

    escaped = esc(str(text).replace(_HELD, ""))
    escaped = _MD_FENCE_RE.sub(lambda m: hold(f"<pre>{m.group(1).strip()}</pre>"), escaped)
    escaped = _MD_CODE_RE.sub(lambda m: hold(f"<code>{m.group(1)}</code>"), escaped)
    # `## **Заголовок**` - одна жирная строка, а не `<b><b>`.
    escaped = _MD_HEADING_RE.sub(lambda m: f"<b>{m.group(1).replace('**', '')}</b>", escaped)
    escaped = _MD_BULLET_RE.sub(r"\1• ", escaped)
    if quotes:
        escaped = _MD_QUOTE_RE.sub(_quote_block, escaped)
    escaped = _MD_BOLD_RE.sub(r"<b>\1</b>", escaped)
    escaped = _MD_ITALIC_RE.sub(r"<i>\1</i>", escaped)
    return _HELD_RE.sub(lambda m: held[int(m.group(1))], escaped)


def _quote_block(match: re.Match) -> str:
    """Подряд идущие строки `> ` - одна свёрнутая цитата: это подробности,
    читатель раскроет их сам (спека chat-text-formatting). Перенос после
    блока остаётся."""
    block = match.group(0)
    tail = "\n" if block.endswith("\n") else ""
    lines = [re.sub(r"^&gt; ?", "", line) for line in block.rstrip("\n").split("\n")]
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>" + tail


# Больше строк подряд сотрудник не читает (docs/simple-language.md).
BLOCK_MAX_LINES = 5


def longest_block(text: str) -> int:
    """Самая длинная серия непустых строк подряд в Markdown модели.

    Строки кода (```) и цитаты (`> `) не считаются и обрывают серию: цитата
    едет свёрнутой.
    Строка - это `\n`, а не ширина экрана."""
    longest = run = 0
    in_fence = False
    for line in str(text).split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            # ```код``` в одну строку блок не открывает.
            in_fence = in_fence != (stripped.count("```") % 2 == 1)
            run = 0
            continue
        if in_fence:
            continue
        # Цитата - отдельный блок на экране: серию обрывает, как пустая строка.
        run = run + 1 if stripped and not stripped.startswith(">") else 0
        longest = max(longest, run)
    return longest


def log_long_block(text: str, mode: str) -> None:
    """Блок длиннее BLOCK_MAX_LINES - в лог. Текст не меняется: длину держит
    промпт, код только сообщает (спека chat-text-formatting)."""
    n = longest_block(text)
    if n > BLOCK_MAX_LINES:
        log.info("answer block too long: %d lines (mode=%s)", n, mode)


# Абзац длиннее этого делится по границе предложения: ~9-10 строк на экране
# телефона (спека chat-text-formatting).
PARAGRAPH_MAX_CHARS = 350
_PARAGRAPH_SEP_RE = re.compile(r"(\n[ \t]*\n)")
# Строка списка, цитаты, заголовка: такой абзац уже разбит по-своему.
_STRUCTURED_LINE_RE = re.compile(r"^\s*(?:[-*•]\s|>|#{1,6}\s|\d+[.)]\s)")


@functools.lru_cache(maxsize=1)
def _english_segmenter():
    # razdel рвёт английский после `e.g.`/`i.e.` перед заглавной буквой;
    # pysbd их знает. ponytail: pysbd не обновлялся с 2021 и сыпет
    # SyntaxWarning на 3.12+; сломается совсем - менять на свою регулярку
    # сокращений поверх razdel.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        import pysbd

    return pysbd.Segmenter(language="en", clean=False)


def _sentence_spans(paragraph: str, lang: str) -> list[tuple[int, int]]:
    if lang != "en":
        return [(s.start, s.stop) for s in razdel.sentenize(paragraph)]
    # Позиции ищем сами от курсора: char_span pysbd путается на повторах.
    spans, cursor = [], 0
    for sentence in _english_segmenter().segment(paragraph):
        piece = sentence.strip()
        start = paragraph.find(piece, cursor)
        if not piece or start < 0:
            return []
        spans.append((start, start + len(piece)))
        cursor = start + len(piece)
    return spans


def _split_paragraph(paragraph: str, lang: str) -> str:
    spans = _sentence_spans(paragraph, lang)
    if len(spans) < 2:
        return paragraph
    chunks, start, end = [], spans[0][0], spans[0][1]
    for s_start, s_stop in spans[1:]:
        # Не режем внутри `**жирного**` и `кода`: разорванная пара доедет
        # до чата сырыми звёздочками.
        head = paragraph[start:s_start]
        balanced = head.count("**") % 2 == 0 and head.count("`") % 2 == 0
        if s_stop - start > PARAGRAPH_MAX_CHARS and balanced:
            chunks.append(paragraph[start:end])
            start = s_start
        end = s_stop
    chunks.append(paragraph[start:end])
    return "\n\n".join(chunks)


def split_long_paragraphs(text: str, lang: str) -> str:
    """Markdown ответа модели: абзац обычного текста длиннее
    `PARAGRAPH_MAX_CHARS` делится по границам предложений. Списки, цитаты,
    заголовки и код не трогаются; одно длинное предложение остаётся целым -
    разорванная фраза хуже длинного абзаца. Применяется до `md_to_html`."""
    parts = _PARAGRAPH_SEP_RE.split(text)
    in_fence = False
    for index in range(0, len(parts), 2):
        paragraph = parts[index]
        fences = paragraph.count("```")
        if in_fence or fences:
            in_fence = in_fence != (fences % 2 == 1)
            continue
        if len(paragraph) <= PARAGRAPH_MAX_CHARS or any(
            _STRUCTURED_LINE_RE.match(line) for line in paragraph.split("\n")
        ):
            continue
        parts[index] = _split_paragraph(paragraph, lang)
    return "".join(parts)


# Что делается кодом, а не просьбой к модели (как `fix_unicode.sh --fix`).
_TYPOGRAPHY = str.maketrans({
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u275d": '"', "\u275e": '"',
    "\u301d": '"', "\u301e": '"', "\u00ab": '"', "\u00bb": '"',
    "\u2018": "'", "\u2019": "'", "\u2014": "-", "\u2013": "-",
    "\u2192": "->", "\u2190": "<-", "\u2194": "<->", "\u21d2": "=>", "\u21d0": "<=",
    "\u2026": "...", "\u2705": "OK", "\u00a0": " ",
    # Отметки несут смысл: без них "✓ a" и "✗ b" читаются одинаково.
    "\u2713": "OK", "\u2714": "OK", "\u2717": "FAIL", "\u2718": "FAIL",
    "\u274c": "FAIL", "\u26a0": "!",
})
# U+FE0F иначе остаётся невидимым символом. Пробел после эмодзи уходит с ним:
# "Готово 🙂 дальше" не получает двойного пробела.
# Прочие стрелки (U+2190-U+21FF) смысла без контекста не несут - удаляются.
_EMOJI_RE = re.compile("[\U0001F300-\U0001FFFF\u2190-\u21ff\u2600-\u27bf\ufe0f]+[ \t]?")
# Экранирование Markdown от модели (`\<x\>`, `snake\_case`): слэш перед
# знаком препинания ASCII, как в CommonMark. `C:\dir` - не экранирование.
_MD_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
# Код - цитата (строки лога, пути): не трогаем. Оборванный блок - до конца текста.
_CODE_SPAN_RE = re.compile(r"(```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)|`[^`\n]*`)", re.DOTALL)


def plain_typography(text: str) -> str:
    parts = _CODE_SPAN_RE.split(text)
    for i in range(0, len(parts), 2):  # чётные - текст, нечётные - код
        text_part = _MD_ESCAPE_RE.sub(r"\1", parts[i].translate(_TYPOGRAPHY))
        parts[i] = _EMOJI_RE.sub("", text_part)
    return "".join(parts)


def tidy_model_text(text: str, lang: str) -> str:
    """Текст модели перед показом в чате: типографика и разбивка абзацев."""
    return split_long_paragraphs(plain_typography(text), lang)


def combined_markup(
    url_rows: list[tuple[str, str]], callback_rows: list[tuple[str, str]]
) -> InlineKeyboardMarkup | None:
    """Одна клавиатура из строк ДВУХ видов - ссылка и callback. Bot API не
    совмещает их в ОДНОЙ кнопке, но разные строки одной разметки - можно;
    ссылка идёт верхней строкой.

    Единственное место, где строится кнопка: `markup` - частный случай, а не
    вторая копия сборки."""
    keyboard = [[InlineKeyboardButton(text, url=url)] for text, url in url_rows]
    keyboard += [
        [InlineKeyboardButton(text, callback_data=data)] for text, data in callback_rows
    ]
    if not keyboard:
        return None
    return InlineKeyboardMarkup(keyboard)


def markup(rows: list[tuple[str, str]]) -> InlineKeyboardMarkup | None:
    return combined_markup([], rows)


def shown(value, default: str = "-") -> str:
    """Значение поля для показа: списки - через запятую, пустое - default."""
    if isinstance(value, list):
        return ", ".join(value) if value else default
    return str(value) if value not in (None, "") else default


async def status(message, text: str):
    """Показывает "чем занят бот" перед долгим шагом с агентом; возвращает
    отправленное сообщение (для последующего удаления) или None."""
    if message is None:
        return None
    try:
        return await message.reply_text(text)
    except Exception:  # noqa: BLE001 - чат недоступен, просто не покажем статус
        return None


async def clear_status(sent) -> None:
    """Убирает статус-сообщение после ответа агента; ошибки не важны."""
    if sent is None:
        return
    with contextlib.suppress(Exception):
        await sent.delete()


async def update_status(sent, text: str) -> None:
    """Правит текст уже отправленного статус-сообщения при смене этапа
    ('ищу ответ...' -> 'подключена ещё одна модель...') вместо второго
    сообщения. `sent` - None-safe, ошибка Bot API не пробрасывается."""
    if sent is None:
        return
    with contextlib.suppress(Exception):
        await sent.edit_text(text)


# Шаг таймера ожидания: чаще это шум правок и лимиты Bot API.
WAIT_TICK_S = 10.0
# Сон тикера - модульная переменная: тесты подменяют её и прогоняют тики без
# реального ожидания.
wait_sleep = asyncio.sleep


def elapsed_clock(seconds: float) -> str:
    """Ожидание как `м:сс`. Минуты не переводятся в часы: ожидание дольше
    часа - уже повод разбираться, и "62:05" честнее выглядит ненормально,
    чем "1:02:05"."""
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


class Wait:
    """Статус ожидания, который знает про лестницу моделей: `on_step` -
    колбэк `call_agent`, подключение следующей ступени меняет текст статуса
    (спека llm-model-ladder). Без статуса - ничего не делает."""

    def __init__(self, sent, message, text: str | None, lang: str):
        self.sent, self.message, self.text, self.lang = sent, message, text, lang
        self.elapsed = 0.0

    def _render(self) -> str:
        if not self.elapsed:
            return self.text
        clock = i18n.t(self.lang, "STATUS_ELAPSED", clock=elapsed_clock(self.elapsed))
        return f"{self.text}\n{clock}"

    async def on_step(self, idx: int, provider: str) -> None:
        if self.sent is None:
            return
        # Ступень одна (ADR 0004): подключилась запасная модель, не провайдер.
        self.text = i18n.t(self.lang, "FAILURE_SLOW")
        await update_status(self.sent, self._render())


async def _tick(wait: Wait) -> None:
    """Раз в WAIT_TICK_S дописывает прошедшее время и заново включает
    "печатает..." (он гаснет за секунды). Время считается по тикам, а не по
    часам: статус - индикатор жизни, а не секундомер."""
    while True:
        await wait_sleep(WAIT_TICK_S)
        wait.elapsed += WAIT_TICK_S
        await update_status(wait.sent, wait._render())
        await typing(wait.message)


@contextlib.asynccontextmanager
async def waiting(message, text: str | None, lang: str = i18n.DEFAULT_LANG):
    """Статус долгого шага с таймером: status -> typing -> тикер; на выходе
    тикер отменяется ДО удаления статуса - иначе правка прилетела бы в уже
    удалённое сообщение (show-wait-timer, design.md, решение 3).
    Без `text` - только "печатает...", без статуса и таймера."""
    sent = await status(message, text) if text else None
    await typing(message)
    wait = Wait(sent, message, text, lang)
    ticker = asyncio.create_task(_tick(wait)) if sent else None
    try:
        yield wait
    finally:
        # Отмена без `await ticker`: ожидание отменённой задачи стоит лишний
        # оборот цикла. Правки после отмены не будет и так -
        # CancelledError приходит в тикер в точке его `await`.
        if ticker is not None:
            ticker.cancel()
        await clear_status(sent)


ACCEPTED_REACTION = "👀"
# Сообщения, уже получившие реакцию: (chat_id, message_id).
# ponytail: память процесса на 1000 сообщений, после рестарта старые не обрабатываются.
_REACTED_MAX = 1000
_reacted_order: collections.deque = collections.deque(maxlen=_REACTED_MAX)
_reacted: set = set()


async def react(message, emoji: str = ACCEPTED_REACTION) -> None:
    """Реакция 'ответ принят' на сообщение пользователя перед долгим шагом
    с LLM-агентом; best effort - реакции могут быть запрещены в чате.
    На сообщение - один вызов: пачка ставит реакцию при входе, Справка перед
    моделью уже не повторяет (session-log-completeness)."""
    if message is None:
        return
    message_id = getattr(message, "message_id", None)
    if message_id is not None:
        key = (getattr(message, "chat_id", None), message_id)
        if key in _reacted:
            return
        if len(_reacted_order) == _reacted_order.maxlen:
            _reacted.discard(_reacted_order[0])
        _reacted_order.append(key)
        _reacted.add(key)
    with contextlib.suppress(Exception):  # реакция - украшение, не критична
        await message.set_reaction(emoji)


async def typing(message) -> None:
    """Индикатор "печатает..." перед долгим вызовом агента; ошибки не важны."""
    if message is None:
        return
    # нет метода/чат недоступен - индикация не критична
    with contextlib.suppress(Exception):
        await message.reply_chat_action("typing")


def _url_buttons(markup) -> InlineKeyboardMarkup | None:
    """Кнопки-ссылки клавиатуры: они переживают нажатие соседней кнопки."""
    rows = [
        [button for button in row if getattr(button, "url", None)]
        for row in getattr(markup, "inline_keyboard", None) or ()
    ]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(rows) if rows else None


async def mark_chosen(message, label: str, lang: str = i18n.DEFAULT_LANG) -> None:
    """Фиксирует нажатую кнопку: сохраняет разметку исходного сообщения
    (через `text_html`, если он есть - настоящий `telegram.Message` его
    отдаёт, `FakeMessage` в тестах нет) и дописывает "Выбрано: ..." курсивом.

    Кнопки-ссылки исходного сообщения переживают фиксацию: правка без
    разметки снимает у Bot API все кнопки сообщения, и ссылка рядом с
    выбором пропала бы. Повторное нажатие той же кнопки ничего не
    дописывает: отметка уже стоит."""
    if message is None:
        return
    base = getattr(message, "text_html", None) or getattr(message, "text", None) or ""
    # Сравниваем строку целиком и без HTML-сущностей: "Нет" - не отметка
    # "Нет, другое", а `&amp;` в `text_html` - это `&` в подписи кнопки.
    chosen = f"{i18n.t(lang, 'CHOSEN_PREFIX')}{label}"
    if chosen in html.unescape(strip_tags(base)).splitlines():
        return
    base = base.strip()
    # Курсивом: служебная отметка не должна читаться продолжением ответа
    # бота (review-774-775).
    line = f"<i>{i18n.t(lang, 'CHOSEN_PREFIX')}{esc(label)}</i>"
    # Широкий except - не как в reply(): здесь правка уже показанного
    # сообщения, нового сообщения при её ошибке не шлём - дублировать нечего.
    keep_markup = _url_buttons(getattr(message, "reply_markup", None))
    # правка текста недоступна - хотя бы убрать кнопки
    with contextlib.suppress(Exception):
        await message.edit_text(
            f"{base}\n\n{line}".strip(), parse_mode="HTML", reply_markup=keep_markup
        )
        return
    with contextlib.suppress(Exception):
        await message.edit_reply_markup(reply_markup=None)


async def send_html(message, text: str, keyboard=None):
    """Отправка с HTML и фолбэком на текст без тегов при ошибке разбора.

    Клавиатура здесь ГОТОВАЯ, а не строки: под вердиктом живёт разметка из
    кнопки-ссылки и кнопок оценки сразу (`combined_markup`), и собрать её из
    одного списка строк нельзя.
    """
    try:
        return await message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
    except telegram.error.BadRequest as exc:
        if not is_parse_error(exc):  # "Text is too long" и прочее - не разметка
            raise
        return await message.reply_text(strip_tags(text), reply_markup=keyboard)
    # остальные ошибки (таймаут, сеть, RetryAfter...) пробрасываются: сообщение
    # могло уже дойти до чата, второй попыткой мы бы задублировали его


async def reply(message, text: str, rows: list[tuple[str, str]] | None = None):
    """То же для обычного случая - клавиатура из строк callback-кнопок.
    Возвращает отправленное сообщение - его правит опоздавшая рекомендация."""
    return await send_html(message, text, markup(rows or []))


async def edit(sent, text: str, rows: list[tuple[str, str]] | None = None) -> None:
    """Дозапись в уже показанное сообщение (опоздавшая рекомендация).
    Ошибку Bot API глотаем: диалог продолжается без рекомендации.
    Широкий except намеренно (в отличие от reply): новое сообщение здесь не
    шлётся, второй попытки нет - дублировать нечего."""
    if sent is None:
        return
    try:
        await sent.edit_text(text, reply_markup=markup(rows or []), parse_mode="HTML")
    except Exception:  # noqa: BLE001, S110 - рекомендация вторична, диалог важнее
        pass


# --- Длинные сообщения (спека long-message-send) ---------------

# Лимит Bot API на текст одного сообщения, в единицах UTF-16.
TELEGRAM_TEXT_LIMIT = MessageLimit.MAX_TEXT_LENGTH


def is_parse_error(exc: Exception) -> bool:
    """Bot API отверг разметку ("Can't parse entities: ..."), а не что-то ещё."""
    return "can't parse entities" in str(exc).lower()


def tg_len(text: str) -> int:
    """Длина так, как её считает Telegram: эмодзи вне BMP - две единицы."""
    return len(text.encode("utf-16-le")) // 2


def _pack(chunks: list[str], sep: str, limit: int) -> list[str]:
    """Жадно склеивает куски через `sep`, пока часть влезает в лимит."""
    out: list[str] = []
    for chunk in chunks:
        if out and tg_len(out[-1]) + tg_len(sep) + tg_len(chunk) <= limit:
            out[-1] += sep + chunk
        else:
            out.append(chunk)
    return out


def _hard_cut(text: str, limit: int) -> list[str]:
    out, cur, units = [], [], 0
    for ch in text:
        width = tg_len(ch)
        if units + width > limit:
            out.append("".join(cur))
            cur, units = [], 0
        cur.append(ch)
        units += width
    return [*out, "".join(cur)]


def _split_plain(text: str, limit: int, seps=("\n\n", "\n", " ")) -> list[str]:
    """Абзацы -> строки -> пробел -> жёстко по лимиту."""
    if tg_len(text) <= limit:
        return [text]
    if not seps:
        return _hard_cut(text, limit)
    sep, rest = seps[0], seps[1:]
    chunks = [piece for part in text.split(sep) for piece in _split_plain(part, limit, rest)]
    return _pack(chunks, sep, limit)


def split_text(text: str, html_mode: bool = False, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Части не длиннее лимита. В HTML разрез только между абзацами, чтобы теги
    не рвались; абзац длиннее лимита теряет теги и делится как обычный текст.
    Длина считается по сырому тексту с тегами - с запасом (design решение 2)."""
    if tg_len(text) <= limit:
        return [text]
    if not html_mode:
        parts = _split_plain(text, limit)
    else:
        chunks: list[str] = []
        for para in text.split("\n\n"):
            fits = tg_len(para) <= limit
            chunks += [para] if fits else _split_plain(strip_tags(para), limit, ("\n", " "))
        parts = _pack(chunks, "\n\n", limit)
    return [part for part in parts if part.strip()]


# Повтор вызова Bot API после 429 (`RetryAfter`). Без него ответ терялся
# молча: глобальный обработчик ошибок RetryAfter не показывает. Ждём внутри
# апдейта, поэтому потолок короткий - дольше пользователь всё равно ушёл.
FLOOD_RETRIES = 2
FLOOD_WAIT_MAX_S = 10.0


def _retry_after_s(exc: telegram.error.RetryAfter) -> float:
    value = exc.retry_after  # int или timedelta - зависит от PTB_TIMEDELTA
    return float(value.total_seconds() if hasattr(value, "total_seconds") else value)


# --- Журнал исходящих вызовов (full-traffic-logging) -------------------------

# Вложения с этими расширениями пишутся в журнал текстом целиком.
TEXT_FILE_SUFFIXES = (".md", ".txt", ".json", ".jsonl")
# Пустые опросы - шум, а их содержимое и так пишется входящими апдейтами.
UNLOGGED_METHODS = frozenset({"getUpdates"})


def _file_entry(part) -> dict:
    """Вложение из multipart: имя, тип, размер; текстовое - с содержимым."""
    filename, content, mime = part
    entry = {"filename": filename, "mime": mime}
    if isinstance(content, bytes):
        entry["size"] = len(content)
        if filename and filename.endswith(TEXT_FILE_SUFFIXES):
            entry["text"] = content.decode("utf-8", "replace")
    return entry


def _call_outcome(code: int | None, body: bytes | None, error: str | None) -> dict:
    if error is not None:
        return {"ok": False, "error": error}
    try:
        response = json.loads(body or b"{}")
    except ValueError:
        return {"status": code, "ok": False, "error": "non-JSON response"}
    result = response.get("result")
    outcome = {"status": code, "ok": response.get("ok")}
    if isinstance(result, dict) and "message_id" in result:
        outcome["message_id"] = result["message_id"]
    if not response.get("ok"):
        outcome["error"] = response.get("description")
    return outcome


def log_bot_call(conn, url: str, request_data, code=None, body=None, error=None) -> None:
    """Вызов Bot API в файловый журнал: метод, параметры, вложения, исход.
    URL с токеном не пишется - только последний сегмент, имя метода."""
    method = url.rsplit("/", 1)[-1]
    # Скачивание файла (`/file/bot...`) - не вызов метода: тело двоичное,
    # а сам `getFile` уже записан.
    if method in UNLOGGED_METHODS or "/file/bot" in url:
        return
    try:
        params = request_data.parameters if request_data is not None else {}
        files = request_data.multipart_data if request_data is not None else {}
        payload = {"method": method, "params": params, **_call_outcome(code, body, error)}
        if files:
            payload["files"] = {name: _file_entry(part) for name, part in files.items()}
        chat_id = params.get("chat_id")
        # Личный чат совпадает с user_id; иначе - чей апдейт сейчас в работе.
        user_id = chat_id if isinstance(chat_id, int) and chat_id > 0 else logging_setup.current_user.get()
        db.log_file_event(conn, user_id, "tg_call", payload)
    except Exception as exc:  # noqa: BLE001 - журнал не роняет отправку
        log.warning("tg_call log failed (%s): %s", method, exc)


class LoggingRequest(HTTPXRequest):
    """Транспорт Bot API, который пишет каждый вызов в журнал. `do_request` -
    публичная точка расширения `BaseRequest`: через неё идут все методы, в том
    числе `reply_*`, `edit_*` и `answer` в обход наших обёрток."""

    def __init__(self, *args, conn, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._conn = conn

    async def do_request(self, url, method, request_data=None, *args, **kwargs):
        try:
            code, body = await super().do_request(url, method, request_data, *args, **kwargs)
        except Exception as exc:
            log_bot_call(self._conn, url, request_data, error=f"{type(exc).__name__}: {exc}")
            raise
        log_bot_call(self._conn, url, request_data, code=code, body=body)
        return code, body


class SplittingBot(ExtBot):
    """Бот, у которого ни один текст не падает на лимите Telegram.

    `Message.reply_text`/`edit_text` внутри зовут эти два метода, поэтому
    подкласс закрывает все места отправки разом (design решение 1). `conn` -
    для записи о пропущенной правке; None - журнала нет."""

    __slots__ = ("_journal",)

    def __init__(self, *args, conn=None, **kwargs):
        super().__init__(*args, **kwargs)
        with self._unfrozen():
            self._journal = conn

    async def _do_post(self, endpoint, data, **kwargs):
        """Каждый вызов Bot API (ExtBot -> `_post` -> `_do_post`, тот же крючок, что
        у `AIORateLimiter` PTB): 429 с коротким `retry_after` - пауза и повтор."""
        for attempt in range(FLOOD_RETRIES + 1):
            try:
                return await super()._do_post(endpoint, data, **kwargs)
            except telegram.error.RetryAfter as exc:
                wait = _retry_after_s(exc)
                if attempt == FLOOD_RETRIES or wait > FLOOD_WAIT_MAX_S:
                    raise
                log.warning("Bot API %s: 429, повтор через %.0f с", endpoint, wait)
                await asyncio.sleep(wait)

    def _headed(self, chat_id, text: str) -> str:
        """Шапка 'режим · этап' - до деления, на языке получателя."""
        if modes.get() is None:
            return text
        lang = modes.ui_lang.get()
        if lang is None and self._journal is not None and isinstance(chat_id, int):
            lang = i18n.lang_for(self._journal, chat_id)
        return modes.with_header(text, lang)

    async def send_message(self, chat_id, text, **kwargs):
        text = self._headed(chat_id, text)
        html_mode = kwargs.get("parse_mode") == "HTML"
        parts = split_text(text, html_mode=html_mode)
        if len(parts) == 1:
            return await super().send_message(chat_id, text, **kwargs)
        markup = kwargs.pop("reply_markup", None)
        sent = None
        for i, part in enumerate(parts):
            extra = dict(kwargs)
            if i:  # ответом на сообщение пользователя идёт только первая часть
                extra.pop("reply_parameters", None)
                extra.pop("reply_to_message_id", None)
            if i == len(parts) - 1:
                extra["reply_markup"] = markup
            try:
                sent = await super().send_message(chat_id, part, **extra)
            except telegram.error.BadRequest as exc:
                if not (html_mode and is_parse_error(exc)):
                    raise
                # Без тегов повторяется только отвергнутая часть: прежние уже в чате.
                extra.pop("parse_mode", None)
                sent = await super().send_message(chat_id, strip_tags(part), **extra)
        return sent  # последняя часть: у неё кнопки, её правит опоздавшая рекомендация

    async def edit_message_text(self, text, *args, **kwargs):
        text = self._headed(kwargs.get("chat_id"), text)
        if tg_len(text) <= TELEGRAM_TEXT_LIMIT:
            return await super().edit_message_text(text, *args, **kwargs)
        # Обрезанный вопрос или вердикт ввёл бы в заблуждение - оставляем прежний.
        log.warning("Правка длиной %s пропущена: длиннее лимита", tg_len(text))
        if self._journal is not None:
            db.log_event(
                self._journal, kwargs.get("chat_id"), "edit_skipped_too_long",
                {"length": tg_len(text), "parse_mode": kwargs.get("parse_mode")},
            )
        return None


async def safe_answer(query, *args, **kwargs) -> None:
    """`answerCallbackQuery`, которому позволено не удаться.

    После рестарта или долгой паузы Telegram отказывает в ответе на старое
    нажатие ("query is too old"). Ответ - лишь снятие "часиков" у кнопки, а
    упавший здесь обработчик терял само нажатие и показывал FAILURE_BOT.
    """
    answer = getattr(query, "answer", None)
    if answer is None:
        return
    try:
        await answer(*args, **kwargs)
    except telegram.error.BadRequest as exc:
        log.info("ответ на нажатие не принят: %s", exc)
