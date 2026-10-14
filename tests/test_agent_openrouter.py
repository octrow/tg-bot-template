"""Транспорт OpenRouter (`tgbot.agent.openrouter`): chat completions и
/v1/responses, журнал вызова, хостер, повтор без необязательных полей."""

import asyncio

import httpx
import pytest

from tgbot import db
from tgbot.agent import openrouter, stages, tools
from tgbot.agent.result import is_unverified
from tgbot.locales.ru import STRINGS as RU

from .conftest import log_rows
from .fakes.openai_client import FailingResponse, FakeOpenAIClient, FakeOpenAIResponse, GrepOnlyClient, openai_payloads, recording_client, responses_reply, ToolCallingClient


def test_openai_system_prompt_does_not_forbid_recommendations():
    """Ступень без доступа к коду формулирует по контексту, а не отказывает
    всем полям подряд (иначе фолбэк молчит - openspec
    recommendations-without-code)."""
    prompt = RU["PROMPT_OPENAI_SYSTEM"]
    assert "предлагай по нему формулировки" in prompt
    assert "Если вопрос требует фактов из кода" not in prompt


async def test_ask_openai_logs_model_and_usage_tokens(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    monkeypatch.setattr(httpx, "AsyncClient", FakeOpenAIClient)

    answer = await openrouter.ask_openai(
        "вопрос",
        api_key="sk-x",
        model="gpt-5.6-luna",
        conn=conn,
        user_id=42,
        context_label="тест",
    )

    assert answer == "ответ"
    entries = log_rows(conn, "agent_call")
    assert len(entries) == 1
    assert entries[0]["provider"] == "openrouter"
    assert entries[0]["model"] == "gpt-5.6-luna"
    assert entries[0]["context"] == "тест"
    assert entries[0]["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


async def test_ask_openai_passes_max_tokens_only_when_given(monkeypatch):
    """Потолок ответа уходит в запрос только в лёгком режиме - обычный запрос
    остаётся без потолка, как раньше."""
    payloads: list[dict] = []

    class _Capturing(FakeOpenAIClient):
        async def post(self, url, headers=None, json=None):
            payloads.append(json)
            return await super().post(url, headers=headers, json=json)

    monkeypatch.setattr(httpx, "AsyncClient", _Capturing)

    await openrouter.ask_openai("вопрос", api_key="sk-x", model="m", max_tokens=200)
    await openrouter.ask_openai("вопрос", api_key="sk-x", model="m")

    assert payloads[0]["max_completion_tokens"] == 200
    assert "max_completion_tokens" not in payloads[1]


async def test_ask_openai_without_conn_skips_logging(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", FakeOpenAIClient)

    answer = await openrouter.ask_openai("вопрос", api_key="sk-x", model="gpt-5.6-luna")

    assert answer == "ответ"


async def test_light_mode_uses_max_completion_tokens(tmp_path, monkeypatch):
    """Reasoning-модели отвергают max_tokens (400) - шлём max_completion_tokens."""
    sent: list = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            sent.append(json)
            return FakeResponse()

    monkeypatch.setattr(openrouter.httpx, "AsyncClient", lambda **kw: FakeClient())

    await openrouter.ask_openai("вопрос", api_key="sk", model="gpt-5.6-luna", max_tokens=200)

    assert sent[0]["max_completion_tokens"] == 200
    assert "max_tokens" not in sent[0]


async def test_full_call_reads_files_and_logs_them(tmp_path, monkeypatch):
    """Полный вызов читает клон и docs/ инструментами, прочитанные файлы
    попадают в журнал."""
    conn = db.connect(tmp_path / "db.sqlite")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "spec.md").write_text("тут описано поведение", encoding="utf-8")

    openai_payloads.clear()
    monkeypatch.setattr(httpx, "AsyncClient", ToolCallingClient)

    answer = await openrouter.ask_openai(
        "как работает сейчас?", api_key="sk-x", model="m", conn=conn, user_id=42,
        context_label="тест", roots=[docs],
    )

    assert answer == "ответ по файлу"
    assert "tools" in openai_payloads[0]
    assert answer.files_read == [str(docs / "spec.md")]  # AgentResult несёт файлы
    # Второй круг возвращает результат инструмента в истории (у OpenRouter
    # серверного состояния нет).
    tool_reply = openai_payloads[1]["input"][-1]
    assert tool_reply["type"] == "function_call_output" and "поведение" in tool_reply["output"]
    assert log_rows(conn, "agent_files_read")[0]["files"] == [str(docs / "spec.md")]
    entry = log_rows(conn, "agent_files_read")[0]
    assert entry["tools_offered"] is True
    assert entry["tools_used"] == ["read_file"]


async def test_openai_grep_only_call_reports_tools_used_without_files(tmp_path, monkeypatch):
    """Модель искала, но ничего не прочитала - `files` пуст, но `tools_used`
    отличает этот исход от "инструментов не предлагали" (пункт 8)."""
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

    assert answer == "ответ по поиску"
    entry = log_rows(conn, "agent_files_read")[0]
    assert entry["tools_used"] == ["grep"]
    assert entry["files"] == []
    assert entry["tools_offered"] is True


async def test_openai_full_mode_without_tool_calls_reports_tools_offered(tmp_path, monkeypatch):
    """Инструменты предложены, модель ответила без единого вызова."""
    conn = db.connect(tmp_path / "db.sqlite")
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(httpx, "AsyncClient", recording_client("ответ"))

    await openrouter.ask_openai(
        "вопрос", api_key="sk-x", model="m", conn=conn, user_id=42,
        context_label="тест", roots=[docs],
    )

    entry = log_rows(conn, "agent_files_read")[0]
    assert entry["tools_offered"] is True
    assert entry["tools_used"] == []
    assert entry["files"] == []


async def test_openai_without_roots_sends_no_tools(monkeypatch):
    """Лёгкий режим (закрытый выбор) - без инструментов, как у Claude."""
    openai_payloads.clear()

    monkeypatch.setattr(httpx, "AsyncClient", recording_client())

    await openrouter.ask_openai("вопрос", api_key="sk-x", model="m", max_tokens=200)

    assert "tools" not in openai_payloads[0]


async def test_openai_light_mode_reports_tools_not_offered(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    monkeypatch.setattr(httpx, "AsyncClient", recording_client("ok"))

    await openrouter.ask_openai(
        "вопрос", api_key="sk-x", model="m", conn=conn, user_id=42, context_label="тест"
    )

    entry = log_rows(conn, "agent_files_read")[0]
    assert entry["tools_offered"] is False
    assert entry["tools_used"] == []


async def test_openai_error_text_carries_response_body(monkeypatch):
    """Голый '400 Bad Request' не диагностируется - причина лежит в теле."""

    class _Failing(FakeOpenAIClient):
        async def post(self, url, headers=None, json=None):
            return FailingResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _Failing)

    try:
        await openrouter.ask_openai("вопрос", api_key="sk-x", model="m")
    except RuntimeError as exc:
        assert "Unsupported parameter: tools" in str(exc)
    else:
        raise AssertionError("ожидали ошибку")


async def test_openai_retries_without_tools_on_error(tmp_path, monkeypatch):
    """Модель может не принимать tools - ответ терять из-за этого нельзя, но
    снятие не должно быть молчаливым: журнал + пометка результата
    (spec grounding-verification)."""
    conn = db.connect(tmp_path / "db.sqlite")
    docs = tmp_path / "docs"
    docs.mkdir()
    openai_payloads.clear()

    class _RejectsTools(FakeOpenAIClient):
        async def post(self, url, headers=None, json=None):
            openai_payloads.append("tools" in json)
            if "tools" in json:
                return FailingResponse()
            return FakeOpenAIResponse(responses_reply("ответ"))

    monkeypatch.setattr(httpx, "AsyncClient", _RejectsTools)

    answer = await openrouter.ask_openai(
        "вопрос", api_key="sk-x", model="m", conn=conn, user_id=42,
        context_label="тест", roots=[docs],
    )

    assert answer == "ответ"
    assert openai_payloads == [True, False]  # первый запрос с инструментами, повтор - без
    assert answer.tools_stripped is True  # результат помечен: кода не читали
    assert is_unverified(answer)
    assert log_rows(conn, "tools_stripped")[0]["context"] == "тест"
    # /cost считает непроверенные по этому полю - то же правило, что у пометки.
    assert log_rows(conn, "agent_files_read")[-1]["tools_stripped"] is True
    # llm_response помечен деградированным (пункт 5) - без пометки метка
    # "код не читали" терялась бы в записи ответа.
    assert log_rows(conn, "llm_response")[-1]["degraded"] is True


@pytest.mark.parametrize("status", [401, 429, 502])
async def test_no_toolless_retry_on_non_client_errors(tmp_path, monkeypatch, status):
    """Снимать инструменты есть смысл только на 400/422 (модель не приняла
    поле). 401/429/5xx - сбой провайдера: он уходит вверх по лестнице, а
    ответ без кода не подставляется."""
    conn = db.connect(tmp_path / "db.sqlite")
    docs = tmp_path / "docs"
    docs.mkdir()
    sent: list[bool] = []

    class _Down(FakeOpenAIClient):
        async def post(self, url, headers=None, json=None):
            sent.append("tools" in json)
            response = FailingResponse()
            response.status_code = status
            return response

    monkeypatch.setattr(httpx, "AsyncClient", _Down)

    with pytest.raises(openrouter.ProviderHTTPError) as exc:
        await openrouter.ask_openai(
            "вопрос", api_key="sk-x", model="m", conn=conn, user_id=42,
            context_label="тест", roots=[docs],
        )

    assert exc.value.status == status
    assert sent == [True]
    assert log_rows(conn, "tools_stripped") == []


async def test_toolless_retry_on_422(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    sent: list[bool] = []

    class _Rejects(FakeOpenAIClient):
        async def post(self, url, headers=None, json=None):
            sent.append("tools" in json)
            if "tools" in json:
                response = FailingResponse()
                response.status_code = 422
                return response
            return FakeOpenAIResponse(responses_reply("ответ"))

    monkeypatch.setattr(httpx, "AsyncClient", _Rejects)

    answer = await openrouter.ask_openai("вопрос", api_key="sk-x", model="m", roots=[docs])

    assert answer == "ответ"
    assert sent == [True, False]


async def test_openai_full_request_keeps_reasoning_and_tools(tmp_path, monkeypatch):
    """Полный запрос идёт в /v1/responses с включённым размышлением и
    инструментами: без reasoning модель не зовёт инструменты, а chat
    completions не принимает их вместе (spec llm-stage-selection)."""
    docs = tmp_path / "docs"
    docs.mkdir()
    openai_payloads.clear()

    monkeypatch.setattr(httpx, "AsyncClient", recording_client())

    await openrouter.ask_openai("вопрос", api_key="sk-x", model="m", roots=[docs])

    payload = openai_payloads[0]
    assert payload["reasoning"] == {"effort": openrouter.FULL_REASONING_EFFORT}
    assert payload["tools"] == tools.RESPONSES_TOOLS


async def test_light_request_uses_minimal_reasoning(monkeypatch):
    """Лёгкий режим: короткий ответ (кнопка, разбор ответа) не стоит полного
    прохода reasoning - минимальное усилие, инструментов нет."""
    openai_payloads.clear()

    monkeypatch.setattr(httpx, "AsyncClient", recording_client())

    await openrouter.ask_openai("вопрос", api_key="sk-x", model="m")

    assert openai_payloads[0]["reasoning"] == {"effort": "minimal"}
    assert "tools" not in openai_payloads[0]


async def test_retries_without_reasoning_on_error(monkeypatch):
    """Модель без reasoning может не принять параметр - ответ важнее скорости."""
    openai_payloads.clear()

    class _RejectsEffort(FakeOpenAIClient):
        async def post(self, url, headers=None, json=None):
            openai_payloads.append("reasoning" in json)
            if "reasoning" in json:
                return FailingResponse()
            return FakeOpenAIResponse({"choices": [{"message": {"content": "ответ"}}]})

    monkeypatch.setattr(httpx, "AsyncClient", _RejectsEffort)

    assert await openrouter.ask_openai("вопрос", api_key="sk-x", model="m") == "ответ"
    assert openai_payloads == [True, False]


async def test_system_prompt_lists_nearby_files(tmp_path, monkeypatch):
    """Модель не знает, что рядом лежит клон с доками - без обзора дерева она
    отвечает "подтверждения нет", не сделав ни одного поиска."""
    docs = tmp_path / "docs"
    (docs / "patches").mkdir(parents=True)
    (docs / "patches" / "0001.md").write_text("описание", encoding="utf-8")
    (docs / "code.py").write_text("x = 1", encoding="utf-8")
    openai_payloads.clear()

    monkeypatch.setattr(httpx, "AsyncClient", recording_client())

    await openrouter.ask_openai("вопрос", api_key="sk-x", model="m", roots=[docs])

    system = openai_payloads[0]["input"][0]["content"]
    assert "0001.md" in system
    assert system.index("0001.md") < system.index("code.py")  # доки первыми


async def test_openrouter_full_call_sends_history_instead_of_previous_response_id(
    tmp_path, monkeypatch
):
    """У OpenRouter серверного состояния нет (`previous_response_id` обязан быть
    null), поэтому хвост истории - `function_call` прошлого хода и результат
    инструмента - обязаны уйти в `input` вторым кругом."""
    conn = db.connect(tmp_path / "db.sqlite")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "spec.md").write_text("тут описано поведение", encoding="utf-8")
    openai_payloads.clear()
    monkeypatch.setattr(httpx, "AsyncClient", ToolCallingClient)
    stage = stages.STAGES[stages.PROVIDER_OPENROUTER]

    answer = await openrouter.ask_openai(
        "как работает сейчас?", api_key="sk-or", model="z-ai/glm-5.3-flash",
        conn=conn, user_id=42, context_label="тест", roots=[docs],
        provider=stages.PROVIDER_OPENROUTER, responses_url=stage["responses_url"],
    )

    assert answer == "ответ по файлу"
    second = openai_payloads[1]
    assert "previous_response_id" not in second
    kinds = [item.get("type") for item in second["input"]]
    assert "function_call" in kinds and "function_call_output" in kinds
    # Первый ход истории на месте - иначе модель отвечает без вопроса.
    assert second["input"][0]["role"] == "system"
    tool_reply = second["input"][-1]
    assert tool_reply["call_id"] == "c1" and "поведение" in tool_reply["output"]


async def test_light_payload_reasoning_per_stage(monkeypatch):
    """glm-5.3-flash отвечает на отключённое размышление 400 "Reasoning is
    mandatory" - лёгкий запрос уходит с минимальным усилием, а не с полным."""
    openai_payloads.clear()
    monkeypatch.setattr(httpx, "AsyncClient", recording_client("ok"))
    stage = stages.STAGES[stages.PROVIDER_OPENROUTER]

    await openrouter.ask_openai(
        "вопрос", api_key="sk-or", model="z-ai/glm-5.3-flash", max_tokens=200,
        chat_url=stage["chat_url"], light_reasoning=stage["light_reasoning"],
    )

    assert openai_payloads[0]["reasoning"] == {"effort": "minimal"}
    assert "reasoning_effort" not in openai_payloads[0]
    assert openai_payloads[0]["max_completion_tokens"] == 200


async def test_cost_from_usage_lands_in_agent_call(tmp_path, monkeypatch):
    """OpenRouter отдаёт цену вызова в `usage.cost` - без неё `/cost` показывает
    ноль долларов на живой работе. Ровно одна запись: `_COST_SQL` суммирует
    `$.cost_usd` по всем событиям окна."""
    conn = db.connect(tmp_path / "db.sqlite")

    class _WithCost(FakeOpenAIClient):
        async def post(self, url, headers=None, json=None):
            return FakeOpenAIResponse({
                "choices": [{"message": {"content": "ответ"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 3.62e-05},
            })

    monkeypatch.setattr(httpx, "AsyncClient", _WithCost)

    await openrouter.ask_openai(
        "вопрос", api_key="sk-or", model="z-ai/glm-5.3-flash", conn=conn, user_id=42,
        context_label="тест", provider=stages.PROVIDER_OPENROUTER,
    )

    call = log_rows(conn, "agent_call")[0]
    assert call["cost_usd"] == 3.62e-05
    assert call["provider"] == "openrouter"
    assert call["model"] == "z-ai/glm-5.3-flash"
    assert [r for r in log_rows(conn, "agent_files_read") if r.get("cost_usd")] == []


async def test_answer_without_cost_logs_without_failing(tmp_path, monkeypatch):
    """Прямой OpenAI цену не отдаёт вовсе - вызов обязан пройти, а расход по
    нему считается нулём, как и раньше."""
    conn = db.connect(tmp_path / "db.sqlite")
    monkeypatch.setattr(httpx, "AsyncClient", FakeOpenAIClient)

    answer = await openrouter.ask_openai(
        "вопрос", api_key="sk-x", model="m", conn=conn, user_id=42, context_label="тест"
    )

    assert answer == "ответ"
    assert log_rows(conn, "agent_call")[0]["cost_usd"] is None


@pytest.mark.parametrize("reply", [
    {"choices": [{"message": {"content": "ответ"}}], "provider": "Chutes"},
    {**responses_reply("ответ"), "provider": "Chutes"},
])
async def test_upstream_host_logged(tmp_path, monkeypatch, reply):
    """Хостер из ответа - в `upstream`, ступень остаётся в `provider`."""
    conn = db.connect(tmp_path / "db.sqlite")

    class _WithHost(FakeOpenAIClient):
        async def post(self, url, headers=None, json=None):
            return FakeOpenAIResponse(reply)

    monkeypatch.setattr(httpx, "AsyncClient", _WithHost)
    roots = None if "choices" in reply else [tmp_path]

    await openrouter.ask_openai(
        "вопрос", api_key="sk-or", model="z-ai/glm-5.3-flash", conn=conn, user_id=42,
        context_label="тест", provider=stages.PROVIDER_OPENROUTER, roots=roots,
    )

    for event in ("agent_call", "llm_response"):
        row = log_rows(conn, event)[0]
        assert row["upstream"] == "Chutes" and row["provider"] == "openrouter"


async def test_answer_without_host_has_no_upstream(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    monkeypatch.setattr(httpx, "AsyncClient", FakeOpenAIClient)

    answer = await openrouter.ask_openai(
        "вопрос", api_key="sk-x", model="m", conn=conn, user_id=42, context_label="тест"
    )

    assert answer
    assert "upstream" not in log_rows(conn, "agent_call")[0]
    assert "upstream" not in log_rows(conn, "llm_response")[0]


def _generation_client(replies: list, gets: list):
    """Полный вызов без хостера в ответе; `/generation` отвечает по очереди из `replies`."""

    class _Client(FakeOpenAIClient):
        async def post(self, url, headers=None, json=None):
            return FakeOpenAIResponse({**responses_reply("ответ"), "id": "gen-1"})

        async def get(self, url, params=None, headers=None):
            gets.append(params)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            status, data = reply
            response = FakeOpenAIResponse(data)
            response.status_code = status
            return response

    return _Client


async def _full_openrouter_call(conn, tmp_path):
    await openrouter.ask_openai(
        "вопрос", api_key="sk-or", model="z-ai/glm-5.3-flash", conn=conn, user_id=42,
        context_label="тест", provider=stages.PROVIDER_OPENROUTER, roots=[tmp_path],
    )
    await asyncio.gather(*openrouter._upstream_tasks)


async def test_full_call_host_from_generation(tmp_path, monkeypatch):
    """Responses API хостера не называет - он приходит из `/generation`, со
    второй попытки: сразу после ответа сведений ещё нет (404)."""
    conn = db.connect(tmp_path / "db.sqlite")
    gets: list = []
    replies = [(404, {}), (200, {"data": {"provider_name": "Together"}})]
    monkeypatch.setattr(httpx, "AsyncClient", _generation_client(replies, gets))
    monkeypatch.setattr(openrouter, "UPSTREAM_LOOKUP_DELAYS_S", (0, 0))

    await _full_openrouter_call(conn, tmp_path)

    assert gets == [{"id": "gen-1"}, {"id": "gen-1"}]
    row = log_rows(conn, "llm_upstream")[0]
    assert row["upstream"] == "Together" and row["generation_id"] == "gen-1"
    assert row["provider"] == "openrouter" and row["context"] == "тест"


@pytest.mark.parametrize("replies", [
    [(404, {}), (404, {})],
    [httpx.ConnectError("нет сети")],
])
async def test_generation_failure_is_silent(tmp_path, monkeypatch, replies):
    conn = db.connect(tmp_path / "db.sqlite")
    monkeypatch.setattr(httpx, "AsyncClient", _generation_client(replies, []))
    monkeypatch.setattr(openrouter, "UPSTREAM_LOOKUP_DELAYS_S", (0, 0))

    await _full_openrouter_call(conn, tmp_path)

    assert log_rows(conn, "llm_upstream") == []
    assert log_rows(conn, "llm_response")[0]["text"] == "ответ"


# --- full-traffic-logging: полный LLM-обмен и каждый вызов инструмента ---------


class _TwoToolsClient(FakeOpenAIClient):
    """Первый круг - чтение файла и поиск, второй - ответ."""

    async def post(self, url, headers=None, json=None):
        openai_payloads.append(json)
        if len(openai_payloads) == 1:
            return FakeOpenAIResponse({"id": "r1", "output": [
                {"type": "function_call", "call_id": "c1",
                 "name": "read_file", "arguments": '{"path": "spec.md"}'},
                {"type": "function_call", "call_id": "c2",
                 "name": "grep", "arguments": '{"pattern": "поведение"}'},
            ]})
        return FakeOpenAIResponse(responses_reply("ответ", "r2"))


def _file_events(logs, kind: str) -> list[dict]:
    import json

    return [
        record["payload"]
        for path in sorted(logs.glob("*.jsonl"))
        for record in map(json.loads, path.read_text(encoding="utf-8").splitlines())
        if record["kind"] == kind
    ]


async def test_full_call_logs_messages_and_every_tool_call(tmp_path, monkeypatch):
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(tmp_path / "logs"))
    conn = db.connect(tmp_path / "db.sqlite")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "spec.md").write_text("тут описано поведение", encoding="utf-8")
    openai_payloads.clear()
    monkeypatch.setattr(httpx, "AsyncClient", _TwoToolsClient)

    await openrouter.ask_openai(
        "как работает сейчас?", api_key="sk-x", model="m", conn=conn, user_id=42,
        context_label="тест", roots=[docs],
    )

    request = _file_events(tmp_path / "logs", "llm_request")[-1]
    contents = [m["content"] for m in request["messages"]]
    assert any("как работает сейчас?" in c for c in contents)
    assert request["messages"][0]["role"] == "system"
    assert {"read_file", "grep"} <= set(request["tools"])
    calls = _file_events(tmp_path / "logs", "agent_tool_call")
    assert [c["name"] for c in calls] == ["read_file", "grep"]
    assert calls[0]["args"] == {"path": "spec.md"}
    assert "поведение" in calls[0]["result"]
    assert calls[0]["files"] == [str(docs / "spec.md")]
    assert all(isinstance(c["ms"], int) for c in calls)
    # Тяжёлое - только в файле: у таблицы `log` нет ротации.
    assert "messages" not in log_rows(conn, "llm_request")[-1]
    assert log_rows(conn, "agent_tool_call") == []


async def _system_of(roots, lang, context_label) -> str:
    messages = await openrouter._openai_messages("вопрос", roots, lang, context_label)
    return messages[0]["content"]


async def test_reference_has_its_own_system_prompt(tmp_path):
    """reference-language-and-grounding: Справке не уходит общий промпт
    ("цитируй пути"), а заголовок списка файлов - на языке вызова."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("x", encoding="utf-8")
    tools.reset_overview()

    ru = await _system_of([docs], "ru", "reference_question")
    en = await _system_of([docs], "en", "reference_question")
    chat = await _system_of(None, "ru", "reference_chat")

    general = RU["PROMPT_SYSTEM"]
    assert general not in ru and "a.md" in ru
    assert "Доступные файлы" not in en and "a.md" in en
    assert general not in chat
    assert general in await _system_of([docs], "ru", "тест")  # остальным - как было
