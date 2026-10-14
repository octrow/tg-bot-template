"""Промпты требуют подачу Простого языка (спека chat-text-formatting):
Справка на русском и английском.

Запуск: .venv/bin/python -m pytest tests/test_readable_prompts.py -q
"""

import re

import pytest

from tgbot import i18n, reference_mode

# Опорные слова правил подачи в каждом языке: суть первой строкой, абзац на
# одну мысль, плоский список, запреты.
STYLE_MARKERS = {
    "ru": ["первая строка", "одна мысль", "без вложенности", "канцелярит"],
    "en": ["first line", "one idea", "no nesting", "bureaucratese"],
}
LANGS = ["ru", "en"]


@pytest.mark.parametrize("lang", LANGS)
def test_the_chat_style_has_the_delivery_rules(lang) -> None:
    style = i18n.t(lang, "PROMPT_CHAT_STYLE")
    for marker in STYLE_MARKERS[lang]:
        assert marker in style, marker


@pytest.mark.parametrize("lang", LANGS)
def test_the_chat_style_no_longer_forbids_bold_and_headings(lang) -> None:
    style = i18n.t(lang, "PROMPT_CHAT_STYLE")
    assert "без жирного" not in style
    assert "no bold" not in style


@pytest.mark.parametrize("lang", LANGS)
def test_reference_prompts_carry_the_chat_style(lang) -> None:
    style = i18n.t(lang, "PROMPT_CHAT_STYLE")
    assert style in reference_mode.prompt("вопрос", [], lang=lang)
    assert style in reference_mode.retry_prompt("вопрос", "ответ", lang=lang)


# --- спека chat-text-formatting: блоки до 5 строк, без путей (docs/simple-language.md) ---

BLOCK_MARKERS = {
    "ru": ["5 строк", "3-4 блоков", "жирным", "> ", "путей к файлам", "расширений файлов", "имён функций", "слагов"],
    "en": ["5 lines", "3-4 blocks", "bold", "> ", "file paths", "file extensions", "function names", "slugs"],
}
EXAMPLE_MARKERS = {"ru": ["Было:", "Стало:"], "en": ["Before:", "After:"]}
NO_SELF_TALK = {"ru": "не рассказывай", "en": "do not talk about yourself"}


@pytest.mark.parametrize("lang", LANGS)
def test_reference_prompts_require_short_blocks_without_paths(lang) -> None:
    for prompt in (
        reference_mode.prompt("вопрос", [], lang=lang),
        reference_mode.retry_prompt("вопрос", "ответ", lang=lang),
    ):
        for marker in BLOCK_MARKERS[lang] + EXAMPLE_MARKERS[lang] + [NO_SELF_TALK[lang]]:
            assert marker in prompt, marker


@pytest.mark.parametrize("lang", LANGS)
def test_the_reference_prompt_no_longer_asks_for_a_few_sentences(lang) -> None:
    prompt = reference_mode.prompt("вопрос", [], lang=lang)
    assert ("несколько предложений" if lang == "ru" else "a few sentences") not in prompt
    assert ("дружелюбно" if lang == "ru" else "kindly") not in prompt


# --- short-answer-followups: первая строка не выдаёт догадку за факт ---

FIRST_LINE_RULE = {"ru": "- первая строка", "en": "- the first line"}
NO_SOURCE = {"ru": "в источниках этого нет", "en": "the sources do not have this"}


@pytest.mark.parametrize("lang", LANGS)
def test_the_first_line_rule_says_when_the_sources_have_no_answer(lang) -> None:
    for prompt in (
        reference_mode.prompt("вопрос", [], lang=lang),
        reference_mode.retry_prompt("вопрос", "ответ", lang=lang),
    ):
        rule = prompt.split(FIRST_LINE_RULE[lang], 1)[1].split(";\n", 1)[0]
        assert NO_SOURCE[lang] in rule


# --- шаблон без чужого домена: примеры в промптах нейтральные (docs/simple-language.md) ---

# "call" - отдельным словом: в "automatically" оно тоже есть.
DOMAIN_WORDS = [r"звон", r"кампани", r"patch[1]", r"v4_d[b]", r"agent_hangup", r"\bcalls?\b", r"campaign"]


@pytest.mark.parametrize("lang", LANGS)
def test_prompts_have_no_examples_from_another_domain(lang) -> None:
    from tgbot import vision
    from tgbot.locales import en, ru

    texts = {"ru": ru.STRINGS, "en": en.STRINGS}[lang]
    prompts = [v for k, v in texts.items() if k.startswith("PROMPT_")] + [vision.PROMPT]
    for prompt in prompts:
        for word in DOMAIN_WORDS:
            assert not re.search(word, prompt.lower()), (word, prompt[:60])
