"""Подмена `httpx.AsyncClient` для транспорта OpenRouter в тестах агента:
ответы chat completions и /v1/responses без сети."""

from types import SimpleNamespace

import httpx


class FakeOpenAIResponse:
    def __init__(self, data: dict):
        self._data = data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._data


class FakeOpenAIClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        return FakeOpenAIResponse(
            {
                "choices": [{"message": {"content": "ответ"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )


openai_payloads: list = []


def responses_reply(text: str, response_id: str = "resp-1") -> dict:
    """Ответ /v1/responses с одним текстовым блоком."""
    return {
        "id": response_id,
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def recording_client(reply="ответ"):
    """Клиент, складывающий отправленные payload'ы в `openai_payloads`.

    Формат ответа зависит от URL: полный режим ходит в /v1/responses,
    лёгкий - в chat completions."""

    class _Recording(FakeOpenAIClient):
        async def post(self, url, headers=None, json=None):
            openai_payloads.append(dict(json))
            if "responses" in url:
                return FakeOpenAIResponse(responses_reply(reply))
            return FakeOpenAIResponse({"choices": [{"message": {"content": reply}}]})

    return _Recording


class ToolCallingClient(FakeOpenAIClient):
    """OpenAI (/v1/responses) сначала просит файл, на втором круге отвечает."""

    async def post(self, url, headers=None, json=None):
        assert "responses" in url  # полный режим обязан ходить в /v1/responses
        openai_payloads.append(json)
        if len(openai_payloads) == 1:
            return FakeOpenAIResponse({
                "id": "resp-1",
                "output": [{
                    "type": "function_call", "call_id": "c1",
                    "name": "read_file", "arguments": '{"path": "spec.md"}',
                }],
            })
        return FakeOpenAIResponse(responses_reply("ответ по файлу", "resp-2"))


class GrepOnlyClient(FakeOpenAIClient):
    """OpenAI зовёт только grep и отвечает без единого чтения файла."""

    async def post(self, url, headers=None, json=None):
        assert "responses" in url
        openai_payloads.append(json)
        if len(openai_payloads) == 1:
            return FakeOpenAIResponse({
                "id": "resp-1",
                "output": [{
                    "type": "function_call", "call_id": "c1",
                    "name": "grep", "arguments": '{"pattern": "CSAT"}',
                }],
            })
        return FakeOpenAIResponse(responses_reply("ответ по поиску", "resp-2"))


class FailingResponse:
    status_code = 400
    text = '{"error": {"message": "Unsupported parameter: tools"}}'

    def raise_for_status(self):
        raise httpx.HTTPStatusError("Client error '400 Bad Request'", request=None, response=None)

    def json(self):
        raise AssertionError("тело не парсим, когда статус ошибочный")


# --- Ступень OpenRouter (спека llm-provider-selection) ---------------------


def openrouter_config(**over):
    """Дефолтная конфигурация бота: единственная настроенная ступень - OpenRouter."""
    base = dict(
        repo_path=None,
        openrouter_api_key="sk-or", openrouter_models=("z-ai/glm-5.3-flash",),
        llm_provider="openrouter", agent_cache_ttl_s=0,
    )
    return SimpleNamespace(**{**base, **over})
