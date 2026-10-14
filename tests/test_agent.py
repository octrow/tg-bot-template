"""Агент целиком: лестница (`call_agent`), ступень OpenRouter, кэш, брак ответа,
непроверенный ответ (`tgbot.agent.ladder`, `stages`, `result`)."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tgbot import db
from tgbot.agent import cache, failures, ladder, openrouter, stages
from tgbot.agent.result import AgentResult, is_unverified
from tgbot.locales.ru import STRINGS as RU

from .conftest import log_rows, make_update
from .fakes.openai_client import FakeOpenAIClient, FakeOpenAIResponse, GrepOnlyClient, openai_payloads, openrouter_config, recording_client, responses_reply, ToolCallingClient



async def test_openai_answer_found_only_by_grep_is_not_unverified(tmp_path, monkeypatch):
    """То же на ветке OpenAI: grep без единого read_file - не непроверено."""
    conn = db.connect(tmp_path / "db.sqlite")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("строка про CSAT", encoding="utf-8")
    openai_payloads.clear()
    monkeypatch.setattr(httpx, "AsyncClient", GrepOnlyClient)

    answer = await openrouter.ask_openai(
        "как это работает?", api_key="sk-x", model="m", conn=conn, user_id=42,
        context_label="тест", roots=[docs],
    )

    assert answer.files_read == []
    assert not is_unverified(answer)


async def test_openai_answer_without_any_tool_call_is_still_unverified(tmp_path, monkeypatch):
    """OpenAI-ветка: инструменты предложены, но ни один не вызван - пометка
    "не подтверждён" сохраняется (сценарий "обращения к коду не было")."""
    conn = db.connect(tmp_path / "db.sqlite")
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(httpx, "AsyncClient", recording_client("ответ"))

    answer = await openrouter.ask_openai(
        "вопрос", api_key="sk-x", model="m", conn=conn, user_id=42,
        context_label="тест", roots=[docs],
    )

    assert is_unverified(answer)


async def test_tools_stripped_stays_unverified_even_if_tools_were_used_before_strip():
    """Снятые инструменты (отказ провайдера) помечают ответ непроверенным
    независимо от того, что было найдено до снятия - `tools_stripped`
    первичен над `tools_used` (сценарий "инструменты были сняты")."""
    answer = AgentResult(
        "ответ", files_read=[], tools_used=["grep"], tools_stripped=True
    )
    assert is_unverified(answer)


async def test_call_agent_without_repo_path_uses_docs_not_process_dir(tmp_path, monkeypatch):
    """Путь клона не задан - каталог процесса агенту не подсовываем."""
    seen: list = []

    async def capturing_ask(prompt, **kwargs):
        seen.append(kwargs["roots"])
        return "ответ"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", capturing_ask)
    conn = db.connect(tmp_path / "db.sqlite")
    config = SimpleNamespace(
        repo_path=None, agent_cache_ttl_s=0,
        openrouter_api_key="sk-or",
    )

    assert await ladder.call_agent(config, conn, "вопрос", 42, "тест") == "ответ"
    assert seen == [[Path("docs").resolve()]]  # только докам, каталог процесса не отдаём


def test_knowledge_roots_are_existing_dirs_and_the_clone(tmp_path):
    from tgbot.config import Config

    kb = tmp_path / "kb"
    kb.mkdir()
    (tmp_path / "repo").mkdir()
    config = Config(
        telegram_bot_token="t", admin_user_ids=frozenset(), db_path=tmp_path / "bot.db",
        knowledge_dirs=(kb, tmp_path / "missing"), repo_url="https://example.com/kb.git",
    )
    assert stages.knowledge_roots(config) == [kb, tmp_path / "repo"]


async def test_call_agent_stage_down_reports_without_provider_text(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")

    async def claude_fails(*args, **kwargs):
        raise RuntimeError("нет подписки")

    async def openai_fails(*args, **kwargs):
        raise RuntimeError("openai 429: rate limit")

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", openai_fails)
    config = SimpleNamespace(
        repo_path=None, anthropic_api_key=None,
        openrouter_api_key="sk-or-test",
        openrouter_models=("z-ai/glm-5.3-flash",),
    )
    message = make_update(42).effective_message

    assert await ladder.call_agent(config, conn, "вопрос", 42, "тест", message=message) is None

    assert len(message.replies) == 1
    # Текст провайдера - в журнале под кодом инцидента, а не у пользователя
    # (спека failure-messages).
    assert "openai 429" not in message.replies[0]
    assert RU["FAILURE_UNAVAILABLE"] in message.replies[0]
    failure = log_rows(conn, "llm_failure")[0]
    assert failure["error"] == "openai 429: rate limit"


async def test_light_mode_limits_openai_response(tmp_path, monkeypatch):
    """OpenAI-ступень лёгкого режима ограничена по размеру ответа."""
    seen: list = []

    async def claude_fails(*args, **kwargs):
        raise RuntimeError("no subscription")

    async def fake_openai(prompt, *, api_key, model, max_tokens=None, **kwargs):
        seen.append(max_tokens)
        return '{"option": "Баг"}'

    conn = db.connect(tmp_path / "db.sqlite")
    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", fake_openai)
    config = SimpleNamespace(
        repo_path=None, anthropic_api_key=None,
        openrouter_api_key="sk-or-test",
        openrouter_models=("z-ai/glm-5.3-flash",),
    )

    await ladder.call_agent(config, conn, "вопрос", 42, "тест", light=True)
    await ladder.call_agent(config, conn, "вопрос", 42, "тест")

    assert seen == [stages.LIGHT_MAX_TOKENS, stages.FULL_MAX_TOKENS]


async def test_call_agent_does_not_cache_degraded_answer(tmp_path, monkeypatch):
    """Деградированный (инструменты сняты отказом провайдера) ответ не
    кэшируется - иначе следующий идентичный запрос отдал бы его ещё сутки,
    хотя причина отказа могла уже быть исправлена (пункт 5)."""
    conn = db.connect(tmp_path / "db.sqlite")
    calls: list[int] = []

    async def degraded(prompt, **kwargs):
        calls.append(1)
        return AgentResult("деградированный ответ", files_read=[], tools_stripped=True)

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", degraded)
    config = SimpleNamespace(
        repo_path=None, openrouter_api_key="sk-or",
        openrouter_models=("m",), llm_provider="openrouter", agent_cache_ttl_s=60,
    )

    answer1 = await ladder.call_agent(config, conn, "вопрос", 42, "тест")
    answer2 = await ladder.call_agent(config, conn, "вопрос", 42, "тест")

    assert answer1 == "деградированный ответ" == answer2
    assert len(calls) == 2  # оба раза ушли к провайдеру - кэш пуст


async def test_openrouter_stage_is_the_only_one_called(tmp_path, monkeypatch):
    """Ступень одна: вызов уходит на вход OpenRouter с его моделью, а записи о
    переходе на другую ступень в журнале нет - переходов больше не бывает."""
    conn = db.connect(tmp_path / "db.sqlite")
    seen: list[dict] = []

    async def fake_openai(prompt, **kwargs):
        seen.append(kwargs)
        return "ответ через OpenRouter"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", fake_openai)

    answer = await ladder.call_agent(openrouter_config(), conn, "вопрос", 42, "тест")

    assert answer == "ответ через OpenRouter"
    assert seen[0]["api_key"] == "sk-or"
    assert seen[0]["model"] == "z-ai/glm-5.3-flash"
    assert seen[0]["provider"] == stages.PROVIDER_OPENROUTER
    assert "openrouter.ai" in seen[0]["chat_url"] and "openrouter.ai" in seen[0]["responses_url"]
    assert log_rows(conn, "agent_fallback_openai") == []


async def test_llm_base_url_overrides_stage_host(tmp_path, monkeypatch):
    """TGBOT_LLM_BASE_URL меняет хост ступени, путь остаётся прежним."""
    conn = db.connect(tmp_path / "db.sqlite")
    seen: list[dict] = []

    async def fake_openai(prompt, **kwargs):
        seen.append(kwargs)
        return "ответ заглушки"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", fake_openai)
    config = openrouter_config(llm_base_url="http://127.0.0.1:9/api/v1")

    await ladder.call_agent(config, conn, "вопрос", 42, "тест")

    assert seen[0]["chat_url"] == "http://127.0.0.1:9/api/v1/chat/completions"
    assert seen[0]["responses_url"] == "http://127.0.0.1:9/api/v1/responses"


async def test_stage_failure_without_other_rungs_reports(tmp_path, monkeypatch):
    """Отказ OpenRouter, а других ступеней нет (ключа OpenAI нет) - сообщение
    о недоступности."""
    conn = db.connect(tmp_path / "db.sqlite")

    async def openrouter_fails(prompt, **kwargs):
        assert kwargs["provider"] == stages.PROVIDER_OPENROUTER
        raise RuntimeError("502 от провайдера")

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", openrouter_fails)
    message = make_update(42).effective_message

    result = await ladder.call_agent(
        openrouter_config(), conn, "вопрос", 42, "тест", message=message,
    )

    assert result is None
    assert RU["FAILURE_UNAVAILABLE"] in message.replies[0]
    assert "OpenRouter" not in message.replies[0] and "502" not in message.replies[0]
    failed = log_rows(conn, "openrouter_failed")[0]
    assert failed["error"] == "502 от провайдера"
    assert failed["stage"] == "openrouter" and failed["kind"] == failures.FAILURE_PROVIDER
    assert log_rows(conn, "agent_fallback_openai") == []


async def test_stage_is_part_of_the_cache_key(tmp_path, monkeypatch):
    """Ответ прямого OpenAI лежит в кэше, дефолт сменился - выдавать его за
    ответ glm нельзя: ступень входит в ключ кэша."""
    conn = db.connect(tmp_path / "db.sqlite")
    db.cache_put(conn, cache.cache_key(42, "openai", False, "вопрос", "ru"),
                 "старый ответ OpenAI", provider="openai")

    async def fake_openai(prompt, **kwargs):
        return "свежий ответ OpenRouter"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", fake_openai)

    answer = await ladder.call_agent(
        openrouter_config(agent_cache_ttl_s=60), conn, "вопрос", 42, "тест"
    )

    assert answer == "свежий ответ OpenRouter"
    assert db.cache_get(
        conn, cache.cache_key(42, "openrouter", False, "вопрос", "ru"), 60
    )[0] == "свежий ответ OpenRouter"


# --- Хостер и квантизация OpenRouter (спека llm-stage-selection) ---

QUANT = ["fp8", "fp16", "bf16", "fp32", "unknown"]


@pytest.mark.parametrize("light", [True, False])
async def test_openrouter_payload_filters_quantization(tmp_path, monkeypatch, light):
    """Полный и лёгкий вызов OpenRouter не уходят fp4-хостерам. Дублёров в
    запросе нет: их переключает лестница, а не провайдер (спека llm-model-ladder)."""
    conn = db.connect(tmp_path / "db.sqlite")
    openai_payloads.clear()
    monkeypatch.setattr(httpx, "AsyncClient", recording_client("ок"))
    config = openrouter_config(openrouter_models=("z-ai/glm-5.3-flash", "openai/gpt-6-luna"))

    await ladder.call_agent(config, conn, "вопрос", 42, "тест", light=light)

    payload = openai_payloads[0]
    assert payload["provider"] == {"quantizations": QUANT}
    assert payload["model"] == "z-ai/glm-5.3-flash"
    assert "models" not in payload and "route" not in payload


# --- Потолок полного запроса и брак truncated (спека llm-stage-selection) ---


@pytest.mark.parametrize("context, cap", [("reference_question", 4000)])
async def test_full_call_carries_output_cap(tmp_path, monkeypatch, context, cap):
    conn = db.connect(tmp_path / "db.sqlite")
    openai_payloads.clear()
    monkeypatch.setattr(httpx, "AsyncClient", recording_client("ок"))

    await ladder.call_agent(openrouter_config(), conn, "вопрос", 42, context)

    assert openai_payloads[0]["max_output_tokens"] == cap


async def test_output_cap_kept_in_every_tool_round(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    openai_payloads.clear()
    monkeypatch.setattr(httpx, "AsyncClient", ToolCallingClient)

    await ladder.call_agent(openrouter_config(), conn, "вопрос", 42, "reference_question")

    assert [p["max_output_tokens"] for p in openai_payloads] == [4000, 4000]


def test_rejected_answer_is_its_own_failure_kind():
    failure = failures.failure_of("openrouter", failures.AnswerRejected("truncated"))
    assert failure.kind == failures.FAILURE_REJECTED and failure.detail == "truncated"
    assert "сломанный ответ" in failures.failure_text("ru", failure)


def _truncated_client(first_round_calls_tools: bool, posts: list):
    """Ответ /v1/responses, обрезанный потолком; при `first_round_calls_tools`
    обрезан раунд с запросом инструмента."""

    class _Client(FakeOpenAIClient):
        async def post(self, url, headers=None, json=None):
            posts.append(json)
            reply = responses_reply("обрезанный текст...")
            if first_round_calls_tools:
                reply["output"] = [{
                    "type": "function_call", "call_id": "c1",
                    "name": "read_file", "arguments": '{"path": "spec.md"}',
                }]
            return FakeOpenAIResponse({
                **reply, "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 10, "output_tokens": 4000},
            })

    return _Client


@pytest.mark.parametrize("tools_round", [False, True])
async def test_truncated_answer_is_rejected_not_sent_not_cached(tmp_path, monkeypatch, tools_round):
    """Обрезанный ответ - брак: пользователь видит 'сломанный ответ', в кэше
    пусто, повтор снова идёт в модель."""
    conn = db.connect(tmp_path / "db.sqlite")
    posts: list = []
    monkeypatch.setattr(httpx, "AsyncClient", _truncated_client(tools_round, posts))
    config = openrouter_config(agent_cache_ttl_s=3600)
    message = make_update(42).effective_message

    result = await ladder.call_agent(
        config, conn, "вопрос", 42, "reference_question", message=message,
    )

    assert result is None
    assert len(posts) == 1  # обрезанный раунд инструментов не продолжается
    assert "сломанный ответ" in message.replies[0]
    assert "обрезанный" not in message.replies[0]
    rejected = log_rows(conn, "llm_rejected")[0]
    assert rejected["defect"] == "truncated" and rejected["context"] == "reference_question"
    assert rejected["usage"]["output_tokens"] == 4000
    assert log_rows(conn, "openrouter_failed")[0]["kind"] == failures.FAILURE_REJECTED

    await ladder.call_agent(config, conn, "вопрос", 42, "reference_question")

    assert len(posts) == 2
    assert log_rows(conn, "agent_cache_hit") == []


async def test_incomplete_for_other_reason_is_not_truncation(tmp_path, monkeypatch):
    """`incomplete` из-за фильтра контента - не брак truncated."""
    conn = db.connect(tmp_path / "db.sqlite")

    class _Filtered(FakeOpenAIClient):
        async def post(self, url, headers=None, json=None):
            return FakeOpenAIResponse({
                **responses_reply("часть"), "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
            })

    monkeypatch.setattr(httpx, "AsyncClient", _Filtered)

    await ladder.call_agent(openrouter_config(), conn, "вопрос", 42, "reference_question")

    assert log_rows(conn, "llm_rejected") == []


async def test_incomplete_without_reason_at_cap_is_truncation(tmp_path, monkeypatch):
    """Хостер не прислал `incomplete_details` - решают токены против потолка."""
    conn = db.connect(tmp_path / "db.sqlite")

    class _NoReason(FakeOpenAIClient):
        async def post(self, url, headers=None, json=None):
            return FakeOpenAIResponse({
                **responses_reply("часть"), "status": "incomplete",
                "usage": {"input_tokens": 1, "output_tokens": 4000},
            })

    monkeypatch.setattr(httpx, "AsyncClient", _NoReason)

    result = await ladder.call_agent(openrouter_config(), conn, "вопрос", 42, "reference_question")

    assert result is None
    assert log_rows(conn, "llm_rejected")[0]["defect"] == "truncated"
