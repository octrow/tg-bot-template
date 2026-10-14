"""Ступень OmniRoute и режим "бот только для владельца" (TGBOT_OWNER_ONLY)."""

import pytest

from tgbot import db, vision
from tgbot.access import Access, gate
from tgbot.agent import ladder, stages
from tgbot.config import ConfigError, load_config

from .conftest import ADMIN_ID, make_update

OMNI_ENV = {
    "TELEGRAM_BOT_TOKEN": "t", "TGBOT_LLM_PROVIDER": "omniroute",
    "OMNIROUTE_API_KEY": "sk-omni", "OMNIROUTE_MODEL": "if/qwen3, gh/gpt-5",
}


def test_omniroute_config_selects_its_key_and_models():
    config = load_config(OMNI_ENV)
    assert stages.effective_provider(config) == "omniroute"
    assert ladder.ladder_rungs(config) == [("omniroute", "if/qwen3"), ("omniroute", "gh/gpt-5")]


def test_omniroute_without_models_fails_startup():
    with pytest.raises(ConfigError, match="OMNIROUTE_MODEL"):
        load_config({**OMNI_ENV, "OMNIROUTE_MODEL": ""})


def test_omniroute_request_has_no_openrouter_fields_and_honours_base_url():
    config = load_config({**OMNI_ENV, "TGBOT_LLM_BASE_URL": "http://gw:20128/v1"})
    request = ladder._stage_request(
        config, stages.STAGES["omniroute"], provider="omniroute", model="if/qwen3",
        rung=0, light=True,
    )
    assert request["chat_url"] == "http://gw:20128/v1/chat/completions"
    assert request["responses_url"] == "http://gw:20128/v1/responses"
    assert request["routing"] == {}


def test_omniroute_vision_payload_sends_one_model_without_openrouter_fields():
    payload = vision._payload(b"\x89PNG\r\n\x1a\n", "png", ("a", "b"), provider="omniroute")
    assert payload["model"] == "a"
    assert "models" not in payload and "provider" not in payload


def test_owner_only_without_admins_fails_startup():
    with pytest.raises(ConfigError, match="ADMIN_USER_IDS"):
        load_config({"TELEGRAM_BOT_TOKEN": "t", "TGBOT_OWNER_ONLY": "1"})


async def test_owner_only_ignores_allowlist_and_stays_silent(conn):
    db.add_user(conn, 5, added_by=ADMIN_ID)
    access = Access(conn, [ADMIN_ID], owner_only=True)
    assert access.is_allowed(ADMIN_ID)
    stranger = make_update(5, text="привет")
    assert not await gate(stranger, access)
    assert stranger.effective_message.replies == []
