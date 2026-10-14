"""Фейковый OpenAI-совместимый API ступени LLM (design, решение 3).

По умолчанию отвечает `DEFAULT_TEXT`; тест может поставить свою заготовку
поверх (`reply`) - для любого запроса или для промпта с маркером.

Полный вызов (`/responses` с инструментами) по умолчанию сначала просит
`read_file`: ответ без единого прочитанного файла бот помечает
непроверенным, а это не тот путь, который проверяют сценарии."""

import json
import threading
from dataclasses import dataclass

from ._server import JsonServer

# Файл из docs/ репозитория - корень, который агент видит и без клона.
TOOL_READ_PATH = "simple-language.md"
PROVIDER_NAME = "FakeHost"
DEFAULT_TEXT = "Ответ фейка LLM."

@dataclass
class Request:
    path: str
    body: dict
    prompt: str


def _texts(value) -> list[str]:
    """Все строки сообщения: content бывает строкой или списком блоков."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [t for item in value for t in _texts(item)]
    if isinstance(value, dict):
        return [t for key in ("content", "text", "output") for t in _texts(value.get(key))]
    return []


class FakeLLM:
    def __init__(self):
        self._lock = threading.Lock()
        self._overrides: list[tuple[str | None, str, bool]] = []
        self.requests: list[Request] = []
        self.use_tools = True
        self._server = JsonServer(self._handle)

    def start(self) -> "FakeLLM":
        self._server.start()
        return self

    def stop(self) -> None:
        self._server.stop()

    @property
    def base_url(self) -> str:
        return f"{self._server.url}/api/v1"

    def reply(self, answer: str, *, marker: str | None = None, sticky: bool = False) -> None:
        """Следующий запрос с `marker` в промпте (None - любой) получит `answer`;
        `sticky` - не один раз, а до конца теста."""
        with self._lock:
            self._overrides.append((marker, answer, sticky))

    def prompts(self, marker: str | None = None) -> list[str]:
        with self._lock:
            return [r.prompt for r in self.requests if marker is None or marker in r.prompt]

    def _answer(self, prompt: str) -> str:
        with self._lock:
            for i, (marker, answer, sticky) in enumerate(self._overrides):
                if marker is None or marker in prompt:
                    if not sticky:
                        del self._overrides[i]
                    return answer
        return DEFAULT_TEXT

    def _handle(self, verb, path, query, headers, body):
        if path.endswith("/generation"):
            return 200, {"data": {"provider_name": PROVIDER_NAME}}
        data = json.loads(body or b"{}")
        items = data.get("messages") or data.get("input") or []
        prompt = "\n".join(_texts(items))
        with self._lock:
            self.requests.append(Request(path, data, prompt))
        model = data.get("model", "fake-model")
        if path.endswith("/chat/completions"):
            return 200, {
                "id": "gen-chat", "model": model, "provider": PROVIDER_NAME,
                "choices": [{"message": {"role": "assistant", "content": self._answer(prompt)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        if path.endswith("/responses"):
            return 200, self._responses(data, prompt, model)
        return 404, {"error": {"message": f"нет пути {path}"}}

    def _responses(self, data: dict, prompt: str, model: str) -> dict:
        answered_tool = any(
            isinstance(item, dict) and item.get("type") == "function_call_output"
            for item in data.get("input") or []
        )
        base = {
            "id": "resp-fake", "model": model, "provider": PROVIDER_NAME,
            "status": "completed", "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        if self.use_tools and data.get("tools") and not answered_tool:
            return {**base, "output": [{
                "type": "function_call", "call_id": "call-1", "name": "read_file",
                "arguments": json.dumps({"path": TOOL_READ_PATH}),
            }]}
        return {**base, "output": [{
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": self._answer(prompt)}],
        }]}
