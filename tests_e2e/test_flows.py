"""Сценарии True E2E: живой Telegram (тестовый DC), живой процесс бота.

Тестовый пользователь - админ бота (`ADMIN_USER_IDS`), поэтому меню и /help
у него полные. Бот один на весь прогон: каждый сценарий начинает с /start,
который возвращает в Справку."""

import asyncio
import json

import httpx
import pytest
import pytest_asyncio

from tgbot import i18n
from tgbot.handlers.common import admin_commands, user_commands

from .conftest import api_base_url, e2e_env
from .helpers import RAW_TAG, assert_rendered, find, press, say, settle

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.usefixtures("bot_process")]

T = i18n.texts("ru")
EN = i18n.texts("en")


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def russian(bot_process, user, bot_username):
    """Язык закрепляется явно: до выбора бот берёт `language_code` аккаунта,
    а у тестового аккаунта он может быть любым (живой прогон: `en`)."""
    prompt = (await say(user, bot_username, "/language"))[-1]
    await press(user, bot_username, prompt, "lang:ru")


async def reset(user, bot):
    await say(user, bot, "/start")


async def test_start_and_help_render(user, bot_username):
    start = find(await say(user, bot_username, "/start"), RAW_TAG.sub("", T.START)[:30])
    assert_rendered(start)
    help_msg = find(await say(user, bot_username, "/help"), "/support")
    assert "/allow" in help_msg.raw_text  # тестовый пользователь - админ
    assert_rendered(help_msg)


async def test_command_menu_matches_bot(user, bot_username, env):
    from telethon.tl.functions.users import GetFullUserRequest

    full = await user(GetFullUserRequest(bot_username))
    names = {c.command for c in full.full_user.bot_info.commands}
    assert {name for name, _ in user_commands("ru")} <= names
    # GetFullUser отдаёт только меню по умолчанию (живой прогон 2026-10-03),
    # меню админа задано областью чата - его спрашиваем у Bot API.
    me = await user.get_me()
    url = api_base_url(env).format(token=env["E2E_BOT_TOKEN"]) + "/getMyCommands"
    scope = json.dumps({"type": "chat", "chat_id": me.id})
    resp = await asyncio.to_thread(httpx.post, url, data={"scope": scope}, timeout=30)
    assert resp.status_code == 200, resp.text[:200]
    admin_names = {c["command"] for c in resp.json()["result"]}
    assert {name for name, _ in user_commands("ru") + admin_commands("ru")} <= admin_names


async def test_language_switch_sends_new_message(user, bot_username):
    await reset(user, bot_username)
    prompt = find(await say(user, bot_username, "/language"), T.LANGUAGE_PROMPT[:20])
    _, new = await press(user, bot_username, prompt, "lang:en")
    find(new, EN.LANGUAGE_SET[:20])
    prompt = find(await say(user, bot_username, "/language"), EN.LANGUAGE_PROMPT[:20])
    _, new = await press(user, bot_username, prompt, "lang:ru")  # вернуть язык соседям
    find(new, T.LANGUAGE_SET[:20])


async def test_long_reference_answer_arrives_whole(fakes, user, bot_username):
    llm = fakes
    question = "Расскажи подробно про кампании"
    paragraphs = [f"Абзац {i}: " + "слово " * 60 for i in range(10)]
    llm.reply("\n\n".join(paragraphs), marker=question)
    await reset(user, bot_username)
    replies = await say(user, bot_username, question)
    text = "\n".join(m.raw_text for m in replies)
    assert "Абзац 0:" in text and "Абзац 9:" in text
    for message in replies:
        assert_rendered(message)


async def test_support(user, bot_username):
    await reset(user, bot_username)
    find(await say(user, bot_username, "/support живая проверка E2E"), T.SUPPORT_SAVED[:20])


@pytest_asyncio.fixture(loop_scope="session")
async def stranger():
    """Второй тестовый аккаунт, не в allowlist; без него сценарий пропускается."""
    env = e2e_env()
    if not env.get("E2E_SESSION_2", "").strip():
        pytest.skip("нет E2E_SESSION_2 - второго тестового аккаунта")
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(env["E2E_SESSION_2"]), int(env["E2E_API_ID"]),
                            env["E2E_API_HASH"], lang_code="ru", system_lang_code="ru")
    await client.connect()
    yield client
    await client.disconnect()


async def test_stranger_refused_then_approved_live(stranger, user, bot_username):
    find(await say(stranger, bot_username, "/help"), T.REFUSAL[:30])
    find(await say(stranger, bot_username, "/request_access"), T.ACCESS_QUESTION[:20])
    last_admin = (await user.get_messages(bot_username, limit=1))[0].id
    await say(stranger, bot_username, "Тестовый Пользователь, E2E")
    me = await stranger.get_me()
    card = find(await settle(user, bot_username, last_admin), str(me.id))
    await press(user, bot_username, card, "access:approve:")
    find(await say(stranger, bot_username, "/start"), T.START[:30])
