"""Каталог переводов и определение языка пользователя (спека localization)."""

import re
import string

import pytest

from tgbot.handlers import common as tx
from tgbot import db, i18n
from tgbot.locales import en, ru

from .conftest import log_rows


def _placeholders(text: str) -> set[str]:
    return {f[1] for f in string.Formatter().parse(text) if f[1] is not None}


def test_catalogs_have_the_same_keys():
    assert set(ru.STRINGS) == set(en.STRINGS)


def test_no_empty_translation():
    for catalog in (ru.STRINGS, en.STRINGS):
        for key, value in catalog.items():
            # Пустой словарь - группа без элементов, а не пропущенный перевод.
            assert value or value == {}, key
            if isinstance(value, dict):
                assert all(v is not None for v in value.values()), key
            if isinstance(value, str):
                assert value.strip(), key


def test_same_placeholders_and_shapes():
    for key, value in ru.STRINGS.items():
        other = en.STRINGS[key]
        assert type(value) is type(other), key
        if isinstance(value, str):
            assert _placeholders(value) == _placeholders(other), key
        elif isinstance(value, dict):
            # Ключи вложенных словарей - стабильные коды, они не переводятся.
            assert set(value) == set(other), key
            for inner, text in value.items():
                if isinstance(text, str):
                    assert _placeholders(text) == _placeholders(other[inner]), (key, inner)
        elif isinstance(value, tuple):
            assert other, key


def test_command_descriptions_cover_the_menu():
    for lang in i18n.LANGS:
        descriptions = i18n.texts(lang).CMD_DESCRIPTIONS
        for name in tx.LIMITED_COMMAND_NAMES + tx.USER_COMMAND_NAMES + tx.ADMIN_COMMAND_NAMES:
            assert name in descriptions, (lang, name)


def test_detect_english_profile():
    assert i18n.detect("en") == "en"
    assert i18n.detect("en-GB") == "en"
    assert i18n.detect("EN_US") == "en"


def test_detect_falls_back_to_russian():
    assert i18n.detect("ru") == "ru"
    assert i18n.detect(None) == "ru"
    assert i18n.detect("") == "ru"
    assert i18n.detect("de") == "ru"


def test_lang_for_uses_stored_choice(conn):
    db.set_lang(conn, 7, "en")
    assert i18n.lang_for(conn, 7) == "en"
    # Профиль Telegram явный выбор не переписывает.
    assert i18n.lang_for(conn, 7, "ru") == "en"


def test_lang_for_autodetects_without_choice(conn):
    assert i18n.lang_for(conn, 7, "en-GB") == "en"
    assert i18n.lang_for(conn, 7, None) == "ru"


def test_remember_user_stores_detected_language_once(conn):
    db.remember_user(conn, 7, "nick", lang="en")
    assert db.get_lang(conn, 7) == "en"
    # Повторный контакт с другим профилем язык не меняет.
    db.remember_user(conn, 7, "nick", lang="ru")
    assert db.get_lang(conn, 7) == "en"


def test_explicit_choice_wins_over_autodetect(conn):
    db.remember_user(conn, 7, "nick", lang="en")
    db.set_lang(conn, 7, "ru")
    db.remember_user(conn, 7, "nick", lang="en")
    assert i18n.lang_for(conn, 7, "en") == "ru"


def test_existing_users_stay_russian(conn):
    """Пользователь из БД до внедрения языков: колонка пуста - остаётся русский."""
    db.remember_user(conn, 7, "nick")
    assert db.get_lang(conn, 7) is None
    assert i18n.lang_for(conn, 7) == "ru"


def test_broken_stored_language_falls_back_and_is_logged(conn):
    conn.execute(
        "INSERT INTO seen_users (user_id, username, last_seen, lang)"
        " VALUES (?, NULL, ?, ?)",
        (7, db.now(), "klingon"),
    )
    conn.commit()
    assert i18n.lang_for(conn, 7) == "ru"
    assert log_rows(conn, "lang_invalid")[0]["value"] == "klingon"


def test_unknown_language_falls_back_to_russian_catalog():
    assert i18n.texts("klingon").lang == "ru"
    assert i18n.t("klingon", "START") == ru.STRINGS["START"]


def test_missing_key_is_loud():
    try:
        i18n.t("ru", "NO_SUCH_KEY")
    except AttributeError as exc:
        assert "NO_SUCH_KEY" in str(exc)
    else:  # pragma: no cover - забытый ключ обязан падать
        raise AssertionError("отсутствующий ключ должен ломать вызов")


# --- текст, ждущий ответа, оформлен вопросом (spec bot-text-clarity) -------

WAITING_FOR_ANSWER = (
    "LANGUAGE_PROMPT",
    "ACCESS_QUESTION",
    "REFERENCE_HUMAN_PROMPT",
)


@pytest.mark.parametrize("catalog", [ru.STRINGS, en.STRINGS], ids=["ru", "en"])
@pytest.mark.parametrize("key", WAITING_FOR_ANSWER)
def test_text_waiting_for_an_answer_ends_with_a_question(catalog, key):
    # Вопрос карточки - жирным последней строкой (simple-language).
    assert re.sub(r"</?b>", "", catalog[key]).strip().endswith("?"), catalog[key]
