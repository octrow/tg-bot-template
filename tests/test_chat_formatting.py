"""Разметка ответа модели и разбивка длинных абзацев (спека chat-text-formatting):
`##` и `- ` из ответа должны доезжать до чата оформлением, а не сырыми
знаками, а простыня - абзацами.

Запуск: .venv/bin/python -m pytest tests/test_chat_formatting.py -q
"""

import pytest

from tgbot import utils

# --- md_to_html: новые формы ------------------------------------------------


@pytest.mark.parametrize("hashes", ["#", "##", "###"])
def test_a_heading_line_becomes_a_bold_line(hashes) -> None:
    assert utils.md_to_html(f"{hashes} Как настроить\nтекст") == "<b>Как настроить</b>\nтекст"


def test_four_hashes_stay_text() -> None:
    assert utils.md_to_html("#### мелко") == "#### мелко"


@pytest.mark.parametrize("marker", ["-", "*"])
def test_a_list_item_gets_a_bullet(marker) -> None:
    assert utils.md_to_html(f"{marker} первый\n{marker} второй") == "• первый\n• второй"


def test_consecutive_quote_lines_become_one_blockquote() -> None:
    assert utils.md_to_html("до\n> раз\n> два\nпосле") == (
        "до\n<blockquote expandable>раз\nдва</blockquote>\nпосле"
    )


def test_italic_inside_a_line() -> None:
    assert utils.md_to_html("это *важно* знать") == "это <i>важно</i> знать"


def test_bold_is_not_read_as_italic() -> None:
    assert utils.md_to_html("это **важно** знать") == "это <b>важно</b> знать"


def test_bold_inside_a_list_item() -> None:
    assert utils.md_to_html("- **Шаг**: открыть") == "• <b>Шаг</b>: открыть"


def test_underscores_in_identifiers_stay_as_is() -> None:
    text = "поле call_id в snake_case и _не_курсив_"
    assert utils.md_to_html(text) == text


def test_a_multiplication_star_is_not_italic() -> None:
    assert utils.md_to_html("2 * 3 * 4") == "2 * 3 * 4"


def test_tags_from_the_model_stay_visible() -> None:
    assert utils.md_to_html("<h1>x</h1> <script>") == "&lt;h1&gt;x&lt;/h1&gt; &lt;script&gt;"


def test_markup_inside_a_fenced_block_is_untouched() -> None:
    converted = utils.md_to_html("```\n# comment\n- item\n> q\n*x*\n```")
    assert converted == "<pre># comment\n- item\n&gt; q\n*x*</pre>"


def test_markup_inside_inline_code_is_untouched() -> None:
    assert utils.md_to_html("`*a*` и `**b**`") == "<code>*a*</code> и <code>**b**</code>"


def test_a_real_greater_than_is_not_a_quote() -> None:
    assert utils.md_to_html("a > b") == "a &gt; b"


# --- split_long_paragraphs ----------------------------------------------------

LIMIT = utils.PARAGRAPH_MAX_CHARS
SENTENCE = "Кампания звонит только в рабочие часы, которые заданы в её настройках."


def _paragraphs(text: str) -> list[str]:
    return text.split("\n\n")


def test_the_threshold_is_350() -> None:
    assert LIMIT == 350


def test_a_long_paragraph_is_split_into_parts_under_the_limit() -> None:
    text = " ".join([SENTENCE] * 9)
    assert len(text) > 600
    parts = _paragraphs(utils.split_long_paragraphs(text, "ru"))
    assert len(parts) > 1
    assert all(len(part) <= LIMIT for part in parts)
    assert " ".join(parts).split() == text.split()


def test_a_short_answer_is_unchanged() -> None:
    text = f"{SENTENCE}\n\n{SENTENCE} {SENTENCE}"
    assert utils.split_long_paragraphs(text, "ru") == text


@pytest.mark.parametrize("abbr", ["т. е.", "и т. д.", "см.", "v4.2", "1.5 сек."])
def test_no_break_after_an_abbreviation(abbr) -> None:
    text = " ".join([f"Настройка {abbr} влияет на звонок, если он идёт ночью."] * 8)
    for part in _paragraphs(utils.split_long_paragraphs(text, "ru")):
        assert part.startswith("Настройка"), part


@pytest.mark.parametrize("abbr", ["e.g.", "i.e.", "U.S.", "Mr."])
def test_no_break_after_an_english_abbreviation(abbr) -> None:
    text = " ".join([f"The setting {abbr} Smith changes how the call goes at night."] * 8)
    for part in _paragraphs(utils.split_long_paragraphs(text, "en")):
        assert part.startswith("The setting"), part


def test_one_long_sentence_stays_whole() -> None:
    text = ", ".join(["кампания звонит в рабочие часы"] * 15) + "."
    assert len(text) > LIMIT
    assert utils.split_long_paragraphs(text, "ru") == text


@pytest.mark.parametrize("prefix", ["- ", "* ", "• ", "> ", "1. ", "## "])
def test_lists_quotes_and_headings_are_not_split(prefix) -> None:
    text = "\n".join(f"{prefix}{SENTENCE} {SENTENCE}" for _ in range(4))
    assert utils.split_long_paragraphs(text, "ru") == text


def test_a_fenced_block_is_not_split() -> None:
    body = " ".join([SENTENCE] * 9)
    text = f"Вот:\n\n```\n{body}\n\n{body}\n```"
    assert utils.split_long_paragraphs(text, "ru") == text


def test_other_paragraphs_keep_their_place() -> None:
    long = " ".join([SENTENCE] * 9)
    text = f"Суть.\n\n{long}\n\n- пункт"
    parts = _paragraphs(utils.split_long_paragraphs(text, "ru"))
    assert parts[0] == "Суть."
    assert parts[-1] == "- пункт"


# --- plain_typography: что делается кодом, а не просьбой к модели ------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Типографика - \u-экранами: литеральные символы однажды заменились
        # на ASCII (9.46.0), и тест стал сравнивать ASCII с ASCII.
        ("Это \u2014 шаг, 3\u20135 минут", "Это - шаг, 3-5 минут"),
        ("\u00abёлочки\u00bb, \u201eлапки\u201c и \u2018одинарные\u2019",
         "\"ёлочки\", \"лапки\" и 'одинарные'"),
        ("дальше \u2192 назад \u2190", "дальше -> назад <-"),
        ("синк local\u2194\ufe0fshared", "синк local<->shared"),
        ("A \u21d2 B \u21d0 C", "A => B <= C"),
        ("рост \u2191 спад", "рост спад"),
        ("и так далее\u2026", "и так далее..."),
        ("100\u00a0мс", "100 мс"),
    ],
)
def test_typography_becomes_plain(text, expected) -> None:
    assert utils.plain_typography(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("✓ база ✗ логи", "OK база FAIL логи"),
        ("Сделано OK.", "Сделано OK."),
        ("❌ сбой", "FAIL сбой"),
        ("Внимание⚠️", "Внимание!"),
    ],
)
def test_pass_fail_marks_keep_their_meaning(text, expected) -> None:
    assert utils.plain_typography(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Готово 🙂 дальше", "Готово дальше"),
        ("🚀 Запуск", "Запуск"),
    ],
)
def test_emojis_are_removed(text, expected) -> None:
    assert utils.plain_typography(text) == expected


def test_code_keeps_its_typography() -> None:
    text = "лог `a - b` и\n```\n'x' -> y\n```"
    assert utils.plain_typography(text) == text


def test_tidy_model_text_cleans_and_splits() -> None:
    long = " ".join(["Кампания звонит \u2014 в рабочие часы, которые заданы в настройках."] * 9)
    tidy = utils.tidy_model_text(long, "ru")
    assert "\u2014" not in tidy and " - " in tidy
    assert "\n\n" in tidy


# --- ревью: края конвертера и разбивки ---------------------------------------


def test_a_heading_with_bold_inside_is_one_bold() -> None:
    assert utils.md_to_html("## **Как настроить**") == "<b>Как настроить</b>"


def test_a_heading_line_with_a_code_block_does_not_wrap_pre_in_bold() -> None:
    assert "<b><pre>" not in utils.md_to_html("# ```\ncode\n```")


def test_glob_stars_are_not_italic() -> None:
    text = "см. *.py,*.txt и src/*/x"
    assert utils.md_to_html(text) == text


def test_a_paragraph_opening_with_bold_is_still_split() -> None:
    text = "**Важно:** " + " ".join([SENTENCE] * 9)
    assert "\n\n" in utils.split_long_paragraphs(text, "ru")


def test_no_break_inside_bold_or_code() -> None:
    filler = " ".join([SENTENCE] * 4)
    text = f"{filler} **Первое. Второе. Третье.** `a. B` {filler}"
    for part in utils.split_long_paragraphs(text, "ru").split("\n\n"):
        assert part.count("**") % 2 == 0, part
        assert part.count("`") % 2 == 0, part


# --- longest_block (спека chat-text-formatting) ----------------------------


@pytest.mark.parametrize("n", [5, 6])
def test_longest_block_counts_consecutive_lines(n) -> None:
    assert utils.longest_block("\n".join(["строка"] * n)) == n


def test_longest_block_skips_code_and_quotes() -> None:
    text = "раз\nдва\n```\na\nb\nc\nd\n```\nтри\n> q1\n> q2\n> q3\n> q4\nчетыре"
    assert utils.longest_block(text) == 2


def test_a_one_line_fence_does_not_hide_the_rest() -> None:
    assert utils.longest_block("```x```\n" + "\n".join(["a"] * 6)) == 6


def test_an_empty_line_breaks_the_block() -> None:
    assert utils.longest_block("a\nb\nc\n\nd\ne\n  \nf") == 3


def test_a_long_quote_is_not_counted() -> None:
    text = "\n".join(["> q"] * 8 + ["a", "b", "c"])
    assert utils.longest_block(text) == 3


# --- short-answer-followups: экранирование Markdown снимает код ---


@pytest.mark.parametrize(
    "raw, shown",
    [
        (r"\<имя кампании\>", "<имя кампании>"),
        (r"snake\_case", "snake_case"),
        (r"C:\dir", r"C:\dir"),
        ("a \\\\ b", "a \\ b"),
        (r"код `a\_b` тут", r"код `a\_b` тут"),
        ("```\na\\_b\n```", "```\na\\_b\n```"),
    ],
)
def test_markdown_escapes_are_removed_outside_code(raw, shown) -> None:
    assert utils.tidy_model_text(raw, "ru") == shown
