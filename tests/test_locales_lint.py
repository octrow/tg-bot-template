"""Статичные тексты бота по `docs/simple-language.md` (simple-language-chat-texts).

Проверяется только то, что ловится без ложных срабатываний. Воду и "не X, а Y"
ловит ревью. Вопрос в конце текстов, ждущих ответа, проверяет `test_i18n.py`.
"""

import re

import pytest

from tgbot.locales import bilingual, en, ru

# Промпты модели читает модель, а не человек.
MODEL_KEYS: set[str] = set()
MODE_ICONS = "\U0001F4AC\U0001F501"  # 💬 🔁 - только в шапке
ALLOWED_ICONS = "\u2b50\u2713"  # ⭐ рекомендую, ✓ выбрано
ICON_RE = re.compile(
    "[\U0001F000-\U0001FFFF\u2190-\u21ff\u2300-\u23ff\u2600-\u27bf\u2b00-\u2bff\ufe0f]"
)
DASH_RE = re.compile("[\u2014\u2013]")
CAPS_RE = re.compile(r"(?<![\w{])[A-ZА-ЯЁ]{4,}(?![\w}])")
CAPS_OK = {"JSON", "CSAT", "HTML", "UUID", "HTTP", "HTTPS", "ERROR", "WARNING"}  # ERROR - уровень лога
TAG_RE = re.compile(r"<[^<>]*>")
TAG_OK = re.compile(r"</?(b|i|code|pre|blockquote)>|<blockquote expandable>|<a href=[^<>]*>|</a>")
MAX_LINES = 5


def _texts(catalog: dict, prefix: str = ""):
    for key, value in catalog.items():
        name = f"{prefix}{key}"
        if not prefix and (key.startswith("PROMPT_") or key in MODEL_KEYS):
            continue
        if isinstance(value, dict):
            yield from _texts(value, f"{name}.")
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                if isinstance(item, str):
                    yield f"{name}[{i}]", item
        elif isinstance(value, str):
            yield name, value


def _longest_block(text: str) -> int:
    best = run = 0
    for line in text.split("\n"):
        run = run + 1 if line.strip() else 0
        best = max(best, run)
    return best


def _problems(key: str, text: str) -> list[str]:
    out = []
    for ch in sorted(set(ICON_RE.findall(text))):
        if ch in ALLOWED_ICONS:
            continue
        if ch in MODE_ICONS and key.startswith("MODE_HEADERS."):
            continue
        out.append(f"значок {ch!r}")
    if DASH_RE.search(text):
        out.append("длинное тире")
    caps = [w for w in CAPS_RE.findall(text) if w not in CAPS_OK]
    if caps:
        out.append(f"капс {caps}")
    if _longest_block(text) > MAX_LINES:
        out.append(f"{_longest_block(text)} строк подряд")
    tags = [t for t in TAG_RE.findall(text) if not TAG_OK.fullmatch(t)]
    if tags:
        out.append(f"теги {tags}")
    return out


CATALOGS = [("ru", ru.STRINGS), ("en", en.STRINGS), ("bilingual", bilingual.STRINGS)]


@pytest.mark.parametrize(("name", "catalog"), CATALOGS, ids=[c[0] for c in CATALOGS])
def test_static_texts_follow_simple_language(name, catalog) -> None:
    found = [
        f"{key}: {', '.join(p)}"
        for key, text in _texts(catalog)
        if (p := _problems(key, text))
    ]
    assert not found, f"{name}: {len(found)}\n" + "\n".join(found)


@pytest.mark.parametrize(
    ("key", "text", "bad"),
    [
        ("START", "Справка \U0001F4AC", True),
        ("MODE_HEADERS.reference", "\U0001F4AC Справка", False),
        ("OWN_BUTTON", "\u2b50 Новый", False),
        ("X", "a \u2014 b", True),
        ("X", "ВНИМАНИЕ", True),
        ("X", "формат JSON", False),
        ("X", "\n".join("abcdef"), True),
        ("X", "<b>суть</b> и <code>x</code>", False),
        ("X", "/ask <вопрос>", True),
        ("X", "{user_id}", False),
    ],
)
def test_the_lint_catches_what_it_should(key, text, bad) -> None:
    assert bool(_problems(key, text)) is bad
