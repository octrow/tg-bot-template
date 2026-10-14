"""Матрица ролей: каждая команда и свободный текст под каждой ролью.

Ожидания (R - REFUSAL один раз, потом тишина; A - ADMIN_REFUSAL; + - ответ
по существу; 0 - тишина):

| команда                                   | админ | разрешённый | чужой/снятый | группа |
|-------------------------------------------|-------|-------------|--------------|--------|
| whoami, language, request_access          |   +   |      +      |      +       |   0    |
| start help support                        |   +   |      +      |      R       |   0    |
| allow deny users cost cache_clear logs    |   +   |      A      |      R       |   0    |
| feedback                                  |   +   |      A      |      R       |   0    |
| свободный текст                           |   +   |      +      |      R       |   0    |
| стикер, голосовое, видео, файл не-картинка |   H   |      H      |      R       |   0    |
| фото, картинка файлом                     |   S   |      S      |      R       |   0    |

H - подсказка `NON_TEXT_HINT` (писать текстом). S - скриншот распознаётся и
идёт дальше текстом (screenshot-understanding).
"""

import pytest

from tgbot import i18n
from tgbot.bot import COMMAND_HANDLER_NAMES

from ..fakes.llm_api import DEFAULT_TEXT
from .conftest import ADMIN, ALLOWED, GROUP_CHAT, STRANGER, buttons, user

T = i18n.texts("ru")
OPEN_TO_ALL = ("whoami", "language", "request_access")
ADMIN_ONLY = ("allow", "deny", "users", "cost", "cache_clear", "logs", "feedback")
USER_COMMANDS = tuple(
    c for c in COMMAND_HANDLER_NAMES if c not in OPEN_TO_ALL + ADMIN_ONLY
)


def test_matrix_covers_every_command():
    """Новая команда без строки в матрице - красный тест, а не пробел покрытия."""
    assert set(OPEN_TO_ALL + ADMIN_ONLY + USER_COMMANDS) == set(COMMAND_HANDLER_NAMES)
    assert set(USER_COMMANDS) == {"start", "help", "support", "echo"}


def _is_refusal(text: str) -> bool:
    return text in (T.REFUSAL, T.ADMIN_REFUSAL)


@pytest.mark.parametrize("command", COMMAND_HANDLER_NAMES)
async def test_admin_gets_every_command(bot, command):
    reply = await bot.ask(ADMIN, f"/{command}")
    assert not _is_refusal(reply.params["text"])


@pytest.mark.parametrize("command", USER_COMMANDS + OPEN_TO_ALL)
async def test_allowed_user_gets_user_commands(bot, command):
    reply = await bot.ask(ALLOWED, f"/{command}")
    assert not _is_refusal(reply.params["text"])


@pytest.mark.parametrize("command", ADMIN_ONLY)
async def test_allowed_user_is_refused_admin_commands(bot, command):
    reply = await bot.ask(ALLOWED, f"/{command}")
    assert reply.params["text"] == T.ADMIN_REFUSAL


@pytest.mark.parametrize("command", USER_COMMANDS + ADMIN_ONLY)
async def test_stranger_refused_once_then_silence(bot, command):
    first = await bot.ask(STRANGER, f"/{command}")
    assert first.params["text"] == T.REFUSAL

    mark = bot.send(STRANGER, f"/{command}")
    await bot.silent(STRANGER, mark)


@pytest.mark.parametrize("command", OPEN_TO_ALL)
async def test_stranger_gets_open_commands_even_after_refusal(bot, command):
    await bot.ask(STRANGER, "/help")  # отказ уже показан
    reply = await bot.ask(STRANGER, f"/{command}")
    assert not _is_refusal(reply.params["text"])


async def test_stranger_whoami_shows_ids(bot):
    reply = await bot.ask(STRANGER, "/whoami")
    assert reply.params["text"] == T.WHOAMI.format(user_id=STRANGER, chat_id=STRANGER)


@pytest.mark.parametrize("command", COMMAND_HANDLER_NAMES)
async def test_group_chat_is_silent(bot, command):
    mark = bot.send(ADMIN, f"/{command}", chat_id=GROUP_CHAT)
    await bot.silent(GROUP_CHAT, mark)


async def test_group_free_text_is_silent(bot):
    mark = bot.send(ALLOWED, "как это работает?", chat_id=GROUP_CHAT)
    await bot.silent(GROUP_CHAT, mark)


@pytest.mark.parametrize("uid", [ADMIN, ALLOWED])
async def test_free_text_goes_to_reference(bot, uid):
    mark = bot.send(uid, "как это работает?")
    answer = await bot.wait("sendMessage", chat=uid, after=mark, contains=DEFAULT_TEXT)
    assert answer.params.get("parse_mode") == "HTML"


async def test_stranger_free_text_refused_once(bot):
    first = await bot.ask(STRANGER, "как это работает?")
    assert first.params["text"] == T.REFUSAL
    mark = bot.send(STRANGER, "ну пожалуйста")
    await bot.silent(STRANGER, mark)


async def test_help_lists_admin_commands_only_for_admin(bot):
    admin_help = (await bot.ask(ADMIN, "/help")).params["text"]
    user_help = (await bot.ask(ALLOWED, "/help")).params["text"]
    assert "/allow" in admin_help and "/allow" not in user_help
    assert "/support" in admin_help and "/support" in user_help


async def test_allow_takes_effect_immediately(bot):
    await bot.ask(ADMIN, f"/allow {STRANGER}")
    reply = await bot.ask(STRANGER, "/start")
    assert T.START in reply.params["text"]


async def test_deny_takes_effect_immediately(bot):
    await bot.ask(ADMIN, f"/deny {ALLOWED}")
    reply = await bot.ask(ALLOWED, "/start")
    assert reply.params["text"] == T.REFUSAL


async def test_request_access_for_allowed_user_says_already(bot):
    reply = await bot.ask(ALLOWED, "/request_access")
    assert reply.params["text"] == T.ACCESS_ALREADY


# Нетекстовые сообщения (core.telegram.org: отвечать на каждое личное сообщение -
# BotFather шлёт алерт "Too few replies"). Разрешённому - подсказка писать
# текстом (фото - распознаётся, см. ниже), чужому - тот же единственный отказ,
# потом тишина.
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
MEDIA = {
    "photo": {"photo": [{"file_id": "p1", "file_unique_id": "u1", "width": 9, "height": 9}]},
    "captioned_photo": {
        "photo": [{"file_id": "p2", "file_unique_id": "u2", "width": 9, "height": 9}],
        "caption": "Что на скрине?",
    },
    "sticker": {"sticker": {"file_id": "s1", "file_unique_id": "u3", "type": "regular",
                            "width": 9, "height": 9, "is_animated": False, "is_video": False}},
    "voice": {"voice": {"file_id": "v1", "file_unique_id": "u4", "duration": 2}},
    "document": {"document": {"file_id": "d1", "file_unique_id": "u5", "file_name": "a.txt"}},
    "video": {"video": {"file_id": "w1", "file_unique_id": "u6", "width": 9, "height": 9,
                        "duration": 2}},
}


def _push_media(bot, uid: int, extra: dict) -> int:
    mark = bot.tg.mark()
    bot.tg.push_update({"message": {
        "message_id": bot.tg.next_message_id(), "date": 0,
        "chat": {"id": uid, "type": "private"}, "from": user(uid), **extra,
    }})
    return mark


HINTED = ("sticker", "voice", "document", "video")


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_the_hint_does_not_claim_screenshots_are_not_read(lang):
    """Фото и картинки файлом распознаются, поэтому подсказка зовёт прислать
    скриншот, а не говорит, что фото бот не читает."""
    hint = i18n.texts(lang).NON_TEXT_HINT.lower()
    assert ("скриншот" if lang == "ru" else "screenshot") in hint
    assert ("фото" if lang == "ru" else "photo") not in hint


@pytest.mark.parametrize("kind", HINTED)
@pytest.mark.parametrize("uid", [ADMIN, ALLOWED])
async def test_non_text_message_gets_text_only_hint(bot, uid, kind):
    mark = _push_media(bot, uid, MEDIA[kind])
    hint = await bot.wait("sendMessage", chat=uid, after=mark)
    assert hint.params["text"] == T.NON_TEXT_HINT


async def test_a_captioned_screenshot_is_recognized_and_answered_by_reference(bot):
    """Фото скачивается через getFile, распознаётся фейком LLM и уходит в
    Справку текстом: подпись плюс помеченный распознанный скриншот."""
    bot.tg.files["p2"] = PNG
    bot.llm.reply("1. ТЕКСТ: Start date is in the past", marker="Опиши его")
    mark = _push_media(bot, ALLOWED, MEDIA["captioned_photo"])
    await bot.wait("sendMessage", chat=ALLOWED, after=mark, contains=DEFAULT_TEXT)
    vision_calls = [r for r in bot.llm.requests if "Опиши его" in r.prompt]
    assert vision_calls[0].body["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    reference = [p for p in bot.llm.prompts("Что на скрине?") if "Опиши его" not in p]
    assert reference and T.IMAGE_MARK in reference[0]
    assert "Start date is in the past" in reference[0]


@pytest.mark.packs
async def test_a_text_and_a_screenshot_in_a_row_get_one_answer(bot):
    """Пачка: 'вот звонок' и следом скриншот - один ответ, учитывающий оба."""
    bot.tg.files["p1"] = PNG
    bot.llm.reply("1. ТЕКСТ: Start date is in the past", marker="Опиши его")
    mark = bot.send(ALLOWED, "вот звонок, почему так?")
    _push_media(bot, ALLOWED, MEDIA["photo"])
    await bot.wait("sendMessage", chat=ALLOWED, after=mark, contains=DEFAULT_TEXT)
    # Каждый вызов модели (лестница их может сделать несколько) видит оба.
    reference = [p for p in bot.llm.prompts("вот звонок") if "Опиши его" not in p]
    assert reference and all("Start date is in the past" in p for p in reference)
    answers = [c for c in bot.tg.calls[mark:] if c.method == "sendMessage"
               and DEFAULT_TEXT in c.params.get("text", "")]
    assert len(answers) == 1


async def test_an_unreadable_screenshot_gets_an_incident_code(bot):
    """Файла нет у Telegram (скачать нельзя) и подписи нет - отказ с кодом, не молчание."""
    mark = _push_media(bot, ALLOWED, MEDIA["photo"])
    refusal = await bot.wait("sendMessage", chat=ALLOWED, after=mark, contains=T.IMAGE_FAILED)
    assert any(data.startswith("fail:") for _, data in buttons(refusal))


async def test_non_text_hints_and_text_still_goes_to_reference(bot):
    mark = _push_media(bot, ALLOWED, MEDIA["sticker"])
    hint = await bot.wait("sendMessage", chat=ALLOWED, after=mark)
    assert hint.params["text"] == T.NON_TEXT_HINT
    mark = bot.send(ALLOWED, "как это работает?")
    await bot.wait("sendMessage", chat=ALLOWED, after=mark, contains=DEFAULT_TEXT)


@pytest.mark.parametrize("kind", MEDIA)
async def test_stranger_non_text_refused_once_then_silence(bot, kind):
    mark = _push_media(bot, STRANGER, MEDIA[kind])
    first = await bot.wait("sendMessage", chat=STRANGER, after=mark)
    assert first.params["text"] == T.REFUSAL
    mark = _push_media(bot, STRANGER, MEDIA[kind])
    await bot.silent(STRANGER, mark)
