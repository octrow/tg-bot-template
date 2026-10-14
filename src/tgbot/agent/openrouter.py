"""Транспорт OpenRouter (OpenAI-совместимый API поверх httpx): лёгкий запрос в
chat completions, полный - диалог /v1/responses с инструментами чтения;
журнал вызова, хостер генерации и отбраковка ответа (обрезан, выродился,
длиннее сообщения Telegram). Имена `ask_openai`/`_openai_*` - про формат API,
а не про провайдера."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection

import httpx
from telegram.constants import MessageLimit

from .. import db, degeneration, i18n
from ..reference_mode import CHAT_CONTEXT, REFERENCE_CONTEXT
from .failures import DEFECT_DEGENERATE, DEFECT_TOO_LONG, DEFECT_TRUNCATED, AnswerRejected
from .result import AgentResult
from .stages import CHAT_URL, LIGHT_REASONING, PROVIDER_OPENROUTER, RESPONSES_URL
from . import tools

log = logging.getLogger(__name__)

# `reasoning_effort` - параметр chat completions, `reasoning` - /v1/responses.
OPTIONAL_PAYLOAD_KEYS = ("tools", "reasoning_effort", "reasoning")

# Ответ в чат длиннее лимита Telegram - брак: нормальные ответы по логам не
# длиннее 2 933 символов.
CHAT_ANSWER_LIMIT = MessageLimit.MAX_TEXT_LENGTH
# Вызовы Справки: свой системный промпт (reference-language-and-grounding).
REFERENCE_CONTEXTS = frozenset({REFERENCE_CONTEXT, CHAT_CONTEXT})
# Сколько начала выродившегося ответа пишется в журнал (полный текст уже в
# `llm_response`).
REJECTED_HEAD_CHARS = 300


def duration_ms(started: float) -> int:
    """Длительность обращения к провайдеру от `time.monotonic()`-метки."""
    return int((time.monotonic() - started) * 1000)


@dataclass
class _CallLog:
    """Сквозной контекст одного обращения к провайдеру: куда журналировать и что
    случилось по дороге (снятие инструментов провайдером помечает результат)."""

    conn: Connection | None = None
    user_id: int | None = None
    context_label: str = ""
    tools_stripped: bool = False
    # Ступень лестницы моделей (0 - основная); None - вызов вне лестницы.
    rung: int | None = None


# Усилие размышления полных запросов: без reasoning модель практически не
# инициирует вызовы инструментов (замер в исходном боте: 8078 токенов промпта, 7 токенов
# ответа, 0 прочитанных файлов). "low" достаточно для tool-calls и не разгоняет
# задержку ответа.
# ponytail: константа; вынести в конфиг, если латентность на проде заставит
FULL_REASONING_EFFORT = "low"


async def ask_openai(
    prompt: str, *, api_key: str, model: str, conn=None, user_id: int | None = None,
    context_label: str = "", max_tokens: int | None = None, roots: list[Path] | None = None,
    lang: str = i18n.DEFAULT_LANG, provider: str = PROVIDER_OPENROUTER,
    fallback_models: tuple[str, ...] = (), chat_url: str = CHAT_URL,
    responses_url: str = RESPONSES_URL, light_reasoning: dict | None = None,
    routing: dict | None = None, judge_key: str | None = None, rung: int | None = None,
) -> AgentResult:
    """Запрос к OpenAI-совместимому API (OpenRouter); с `roots` - с чтением файлов.

    `roots` - каталоги, которые модель может читать инструментами read_file /
    grep / glob (`knowledge_roots`).
    Полный запрос (с `roots`) идёт в /v1/responses: chat completions не
    принимает инструменты вместе с reasoning, а без reasoning модель не зовёт
    инструменты (см. spec llm-stage-selection). Лёгкий (без `roots`) - один
    запрос в chat completions с выключенным размышлением: короткий ответ
    (выбор кнопки, разбор ответа пользователя) не стоит полного прохода.
    `conn` - опционален (тесты подменяют всю функцию); если передан, пишет в
    журнал модель, usage-токены и стоимость из ответа.
    `max_tokens` - потолок ответа в лёгком режиме (закрытые выборы). `lang` -
    язык системного промпта. `fallback_models` - дублёры основной модели
    для `models` провайдера; лестница их не передаёт
    (ступень - одна модель), параметр остался для прямых вызовов. Остальное - описание ступени
    (`STAGES`): куда ходим и чем размышляем в лёгком режиме, `routing` - выбор
    хостера у провайдера (фильтр квантизации OpenRouter); дефолты - адреса
    OpenRouter без фильтра. `judge_key` - ключ
    классификатора пограничных выродившихся ответов (`degeneration`).
    """
    messages = await _openai_messages(prompt, roots, lang, context_label)
    call = _CallLog(conn=conn, user_id=user_id, context_label=context_label, rung=rung)
    _log_request(call, provider, model, prompt, messages, roots)
    payload = _payload(model, messages, max_tokens, roots, fallback_models, light_reasoning, routing)
    started = time.monotonic()
    data, answer, files_read, tools_used = await _exchange(api_key, payload, roots, call, chat_url,
                                                           responses_url)
    # Модель из ответа: иначе ответ дублёра неотличим от ответа основной.
    served = data.get("model") or model
    _log_openai_answer(
        call, served, data.get("usage"), answer, files_read, duration_ms(started),
        tools_offered=bool(roots), tools_used=tools_used, provider=provider,
        # Хостер, исполнивший вызов: OpenRouter называет его в `provider` ответа.
        upstream=data.get("provider"),
    )
    if roots and provider == PROVIDER_OPENROUTER and conn is not None and not data.get("provider"):
        _spawn_upstream_lookup(api_key, data.get("id"), call, served, _generation_url(responses_url))
    await _reject_degenerate(answer, call, served, judge_key)
    _reject_too_long(answer, call, served)
    return AgentResult(answer, files_read=files_read, tools_used=tools_used,
                       tools_stripped=call.tools_stripped)


def _log_request(call: _CallLog, provider: str, model: str, prompt: str, messages, roots) -> None:
    if call.conn is None:
        return
    db.log_event(
        call.conn, call.user_id, "llm_request",
        {"provider": provider, "model": model, "prompt": prompt},
        # Всё, что ушло модели, и что ей предложено - только в файл
        # (full-traffic-logging): системный промпт на каждый вызов раздул бы SQLite.
        file_only={
            "messages": messages,
            "tools": [tool["name"] for tool in tools.RESPONSES_TOOLS] if roots else [],
        },
    )


def _payload(model, messages, max_tokens, roots, fallback_models, light_reasoning, routing) -> dict:
    """Тело запроса: полный (с `roots`) или лёгкий, плюс выбор хостера ступени."""
    payload = (
        _full_payload(model, messages, max_tokens, fallback_models) if roots
        else _light_payload(model, messages, max_tokens, light_reasoning, fallback_models)
    )
    return {**payload, **(routing or {})}


async def _exchange(api_key: str, payload: dict, roots, call: _CallLog, chat_url: str,
                    responses_url: str) -> tuple[dict, str, list[str], list[str]]:
    """(ответ API, текст, прочитанные файлы, вызванные инструменты). Полный
    запрос (с `roots`) - диалог /v1/responses с инструментами, лёгкий - один
    запрос в chat completions."""
    async with httpx.AsyncClient(timeout=120) as client:
        if roots:
            data, files_read, tools_used = await _responses_exchange(
                client, api_key, payload, roots, call, url=responses_url,
            )
            return data, _responses_answer(data), files_read, tools_used
        data = await _openai_post(client, api_key, payload, url=chat_url, call=call)
        return data, _answer_of(data), [], []


def _generation_url(responses_url: str) -> str:
    """Сосед `/responses` - так переопределённый хост действует и здесь."""
    return responses_url.rsplit("/responses", 1)[0] + "/generation"


# Сведения о генерации OpenRouter: Responses API хостера не называет, а здесь
# он есть (`provider_name`), но появляется через несколько секунд после ответа.
GENERATION_URL = "https://openrouter.ai/api/v1/generation"
UPSTREAM_LOOKUP_DELAYS_S = (5.0, 5.0)
_upstream_tasks: set[asyncio.Task] = set()


def _spawn_upstream_lookup(
    api_key: str, generation_id, call: "_CallLog", model: str, url: str = GENERATION_URL
) -> None:
    """Фоном узнаёт хостера полного вызова - ответ пользователю его не ждёт."""
    if not generation_id:
        return
    task = asyncio.create_task(_lookup_upstream(api_key, generation_id, call, model, url))
    _upstream_tasks.add(task)  # держим ссылку: иначе GC соберёт задачу на полпути
    task.add_done_callback(_upstream_tasks.discard)


async def _lookup_upstream(
    api_key: str, generation_id: str, call: "_CallLog", model: str, url: str = GENERATION_URL
) -> None:
    """Пишет `llm_upstream`, если OpenRouter назвал хостера; иначе молча сдаётся:
    журнал хостера - диагностика, а не часть ответа (спека llm-stage-selection)."""
    try:
        host = await _poll_upstream(api_key, generation_id, url)
    except (httpx.HTTPError, ValueError) as exc:
        log.info("Хостер генерации %s не получен: %s", generation_id, exc)
        return
    if host:
        db.log_event(
            call.conn, call.user_id, "llm_upstream",
            {"generation_id": generation_id, "upstream": host,
             "provider": PROVIDER_OPENROUTER, "model": model,
             "context": call.context_label},
        )


async def _poll_upstream(api_key: str, generation_id: str, url: str) -> str | None:
    """Хостер из сведений о генерации; None - OpenRouter его не назвал."""
    async with httpx.AsyncClient(timeout=30) as client:
        for delay in UPSTREAM_LOOKUP_DELAYS_S:
            await asyncio.sleep(delay)
            resp = await client.get(
                url, params={"id": generation_id},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                return (resp.json().get("data") or {}).get("provider_name")
    return None


async def _openai_messages(
    prompt: str, roots: list[Path] | None, lang: str = i18n.DEFAULT_LANG,
    context_label: str = "",
) -> list[dict]:
    if context_label in REFERENCE_CONTEXTS:
        system = i18n.t(lang, "PROMPT_REFERENCE_SYSTEM")
    elif roots:
        system = i18n.t(lang, "PROMPT_SYSTEM") + i18n.t(lang, "PROMPT_OPENAI_TOOLS_SUFFIX")
    else:
        system = i18n.t(lang, "PROMPT_OPENAI_SYSTEM")
    overview = await tools.overview(roots, lang) if roots else ""
    return [
        {"role": "system", "content": f"{system}\n\n{overview}" if overview else system},
        {"role": "user", "content": prompt},
    ]


def _fallback_fields(model: str, fallback_models: tuple[str, ...]) -> dict:
    """Поля дублёров для payload; без дублёров - пусто.

    Провайдер сам переключает модель при недоступности основной. Пустой словарь
    при одной модели важен: запрос остаётся ровно таким, каким был до того, как
    переменную стали разбирать списком.
    """
    if not fallback_models:
        return {}
    # Без `route: "fallback"`: OpenRouter объявил его устаревшим, `models` хватает.
    return {"models": [model, *fallback_models]}


def _full_payload(
    model: str, messages: list[dict], max_tokens: int | None,
    fallback_models: tuple[str, ...] = (),
) -> dict:
    payload = {
        "model": model,
        "input": messages,
        "tools": list(tools.RESPONSES_TOOLS),
        "reasoning": {"effort": FULL_REASONING_EFFORT},
        **_fallback_fields(model, fallback_models),
    }
    if max_tokens is not None:
        payload["max_output_tokens"] = max_tokens
    return payload


def _light_payload(
    model: str, messages: list[dict], max_tokens: int | None, reasoning: dict | None = None,
    fallback_models: tuple[str, ...] = (),
) -> dict:
    """Лёгкий запрос: размышление сведено к минимуму, доступному у провайдера
    (`STAGES[...]["light_reasoning"]`)."""
    payload = {
        "model": model, "messages": messages,
        **(reasoning or LIGHT_REASONING),
        **_fallback_fields(model, fallback_models),
    }
    if max_tokens is not None:
        # Именно `max_completion_tokens`: reasoning-модели (в ответе приходит
        # `reasoning_tokens`) отвечают на `max_tokens` ошибкой 400, а у
        # остальных моделей это сегодня синоним.
        payload["max_completion_tokens"] = max_tokens
    return payload


def _answer_of(data: dict) -> str:
    return data["choices"][0]["message"].get("content") or ""


def _responses_answer(data: dict) -> str:
    """Текст ответа /v1/responses: конкатенация output_text-блоков."""
    parts = [
        block.get("text", "")
        for item in data.get("output", [])
        if item.get("type") == "message"
        for block in item.get("content", [])
        if block.get("type") == "output_text"
    ]
    return "".join(parts) or (data.get("output_text") or "")


async def _responses_exchange(
    client, api_key: str, payload: dict, roots: list[Path], call: _CallLog,
    *, url: str = RESPONSES_URL,
) -> tuple[dict, list[str], list[str]]:
    """Диалог /v1/responses до ответа: пока модель просит файлы - отдаём.

    Возвращает (последний ответ API, прочитанные файлы, вызванные инструменты -
    включая grep/glob без единого чтения). Серверного состояния у OpenRouter
    нет, поэтому историю ведём сами - дописываем `output` прошлого хода вместе
    с результатами инструментов (спека llm-provider-selection).
    """
    files_read: list[str] = []
    tools_used: list[str] = []
    spent = 0
    for _ in range(tools.MAX_TOOL_ROUNDS):
        data = await _openai_post(client, api_key, payload, url=url, call=call)
        _reject_truncated(data, payload, call)
        calls = [item for item in data.get("output", []) if item.get("type") == "function_call"]
        if not calls:
            return data, files_read, tools_used
        outputs = []
        for tool_call in calls:
            output, files = await _tool_output(tool_call, roots, call, tools_used)
            files_read.extend(files)
            spent += len(output["output"])
            outputs.append(output)
        # Без серверного состояния хвост истории обязаны прислать мы: без
        # `function_call` прошлого хода провайдер не примет
        # `function_call_output` - ему не к чему его привязать.
        payload["input"] = [*payload["input"], *data.get("output", []), *outputs]
        if spent >= tools.TOOL_BUDGET_CHARS and "tools" in payload:
            _budget_spent(payload, spent)
    # Круги кончились, а модель всё ещё зовёт инструменты - просим ответ словами,
    # иначе запрос остался бы вовсе без ответа.
    payload["input"] = [
        *payload["input"], {"role": "user", "content": "Отвечай сейчас, без инструментов."}
    ]
    payload.pop("tools", None)
    data = await _openai_post(client, api_key, payload, url=url, call=call)
    _reject_truncated(data, payload, call)
    return data, files_read, tools_used


async def _tool_output(tool_call: dict, roots: list[Path], call: _CallLog,
                       tools_used: list[str]) -> tuple[dict, list[str]]:
    """Один вызов инструмента моделью: (`function_call_output`, прочитанные
    файлы); имя инструмента дописывается в `tools_used`."""
    name = tool_call.get("name", "")
    if name not in tools_used:
        tools_used.append(name)
    try:
        args = json.loads(tool_call.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    tool_started = time.monotonic()
    content, files = await tools.run_tool(name, roots, args)
    if call.conn is not None:
        db.log_file_event(call.conn, call.user_id, "agent_tool_call", {
            "context": call.context_label, "name": name, "args": args,
            "result": content, "files": files, "ms": duration_ms(tool_started),
        })
    output = {"type": "function_call_output", "call_id": tool_call.get("call_id"), "output": content}
    return output, files


def _budget_spent(payload: dict, spent: int) -> None:
    """Бюджет исчерпан - снимаем инструменты, модель отвечает по прочитанному.
    Это НЕ снятие провайдером (тот же `payload.pop`, но в обход
    `_openai_post`) - `call.tools_stripped` тут не выставляется и
    событие снятия инструментов не пишется (пункт 5, сценарий
    "бюджет чтения исчерпан")."""
    log.info("Бюджет чтения исчерпан (%s символов), просим ответ", spent)
    payload["input"].append({"role": "user", "content": tools.BUDGET_SPENT})
    payload.pop("tools")


async def _reject_degenerate(answer: str, call: _CallLog, model: str, judge_key) -> None:
    """Выродившийся ответ - брак `degenerate`: до кэша и до отправки."""
    places = await degeneration.is_degenerate(
        answer, conn=call.conn, user_id=call.user_id,
        context_label=call.context_label, judge_key=judge_key,
    )
    if not places:
        return
    if call.conn is not None:
        db.log_event(
            call.conn, call.user_id, "llm_rejected",
            {"defect": DEFECT_DEGENERATE, "model": model, "context": call.context_label,
             "places": places, "head": answer[:REJECTED_HEAD_CHARS]},
        )
    raise AnswerRejected(DEFECT_DEGENERATE)


def _reject_too_long(answer: str, call: _CallLog, model: str) -> None:
    if len(answer) <= CHAT_ANSWER_LIMIT:
        return
    if call.conn is not None:
        db.log_event(
            call.conn, call.user_id, "llm_rejected",
            {"defect": DEFECT_TOO_LONG, "model": model, "context": call.context_label,
             "length": len(answer)},
        )
    raise AnswerRejected(DEFECT_TOO_LONG)


def _reject_truncated(data: dict, payload: dict, call: _CallLog) -> None:
    """Ответ, упёршийся в потолок, - брак `truncated`, даже с текстом.

    Проверяется каждый раунд до поиска `function_call`: обрезанный запрос
    инструментов тоже не продолжается. Другие причины `incomplete` (фильтр
    контента) остаются ошибкой провайдера. Хостер OpenRouter может не прислать
    `incomplete_details` - тогда решают выходные токены против потолка."""
    if data.get("status") != "incomplete":
        return
    reason = (data.get("incomplete_details") or {}).get("reason")
    usage = data.get("usage") or {}
    cap = payload.get("max_output_tokens")
    hit_cap = reason == "max_output_tokens" or (
        reason is None and cap is not None and (usage.get("output_tokens") or 0) >= cap
    )
    if not hit_cap:
        return
    if call.conn is not None:
        db.log_event(
            call.conn, call.user_id, "llm_rejected",
            {"defect": DEFECT_TRUNCATED, "model": data.get("model") or payload.get("model"),
             "context": call.context_label, "usage": data.get("usage")},
        )
    raise AnswerRejected(DEFECT_TRUNCATED)


async def _openai_post(
    client, api_key: str, payload: dict, *, url: str = CHAT_URL, call: _CallLog | None = None
) -> dict:
    """Запрос к провайдеру; при 400/422 из-за необязательных полей - повтор без них.

    Не всякая модель принимает `tools` или reasoning-параметры, а узнаём мы
    это только по 400/422. Терять из-за этого весь ответ нельзя, но молчаливым
    снятие быть не должно: ответ без инструментов - ответ без доступа к коду,
    поэтому факт уходит в журнал бота (`tools_stripped`) и в `meta`, а
    вызывающий код помечает результат непроверенным (spec grounding-verification).
    """
    try:
        return await _post_openai(client, api_key, url, payload)
    except ProviderHTTPError as exc:
        dropped = [k for k in OPTIONAL_PAYLOAD_KEYS if k in payload]
        # 401/429/5xx - не про поля запроса: без инструментов ответ был бы
        # только хуже, а сбой должен уйти вверх по лестнице как есть.
        if not dropped or exc.status not in PAYLOAD_REJECTED_STATUSES:
            raise
        log.warning("Провайдер не принял %s, повторяю без них: %s", ", ".join(dropped), exc)
        if "tools" in dropped and call is not None:
            call.tools_stripped = True
            if call.conn is not None:
                db.log_event(
                    call.conn,
                    call.user_id,
                    "tools_stripped",
                    {"context": call.context_label, "dropped": dropped, "error": str(exc)},
                )
        for key in dropped:
            payload.pop(key)
        return await _post_openai(client, api_key, url, payload)


# Статусы, которыми провайдер отвергает сам запрос (неизвестное поле, формат).
PAYLOAD_REJECTED_STATUSES = frozenset({400, 422})


class ProviderHTTPError(RuntimeError):
    """Ошибочный HTTP-статус провайдера; `status` решает, есть ли смысл
    повторять запрос без необязательных полей."""

    def __init__(self, message: str, status: int | None):
        super().__init__(message)
        self.status = status


async def _post_openai(client, api_key: str, url: str, payload: dict) -> dict:
    resp = await client.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
    )
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # Голый "400 Bad Request" не диагностируется: причина (модель, параметр,
        # инструменты) лежит в теле ответа, а его raise_for_status не показывает.
        raise ProviderHTTPError(
            f"{exc}. Ответ провайдера: {_body_excerpt(resp)}",
            getattr(resp, "status_code", None),
        ) from exc
    return resp.json()


def _body_excerpt(resp, limit: int = 400) -> str:
    try:
        return resp.text[:limit]
    except (RuntimeError, UnicodeDecodeError):  # тело не дочиталось (httpx.StreamError)
        return "(тело ответа недоступно)"


def _log_openai_answer(
    call: _CallLog, model: str, usage, answer: str,
    files_read: list[str], elapsed_ms: int | None = None,
    tools_offered: bool = False,
    tools_used: list[str] | None = None,
    provider: str = "openai",
    upstream: str | None = None,
) -> None:
    """Журнал ступени OpenAI-совместимого провайдера - те же записи, что у
    ступени, включая прочитанные файлы: иначе по логу не видно, на чём основан
    ответ. `provider` - ступень, исполнившая вызов (`rung` - ступень лестницы); `tools_offered`/`tools_used` - честная семантика вызова;
    `degraded` в `llm_response` - инструменты сняты отказом провайдера
    (`call.tools_stripped`), а не исчерпанием бюджета.

    Стоимость вызова (OpenRouter отдаёт её в `usage.cost`) пишется ровно в одну запись - `agent_call`: `_COST_SQL`
    суммирует `$.cost_usd` по всем событиям окна, и второе поле дало бы двойной
    счёт в `/cost`."""
    conn, user_id, context_label = call.conn, call.user_id, call.context_label
    if conn is None:
        return
    tools_used = tools_used or []
    cost_usd = (usage or {}).get("cost") if isinstance(usage, dict) else None
    # `provider` занят ступенью; хостер - отдельным полем и только если назван.
    host = {"upstream": upstream} if upstream else {}
    if call.rung is not None:
        host["rung"] = call.rung
    db.log_event(
        conn, user_id, "agent_files_read",
        {"files": files_read, "context": context_label, "provider": provider,
         "usage": usage,
         "tools_offered": tools_offered, "tools_used": tools_used,
         "tools_stripped": bool(call.tools_stripped)},
    )
    db.log_event(
        conn, user_id, "agent_call",
        {"provider": provider, "model": model, "context": context_label, "usage": usage,
         "cost_usd": cost_usd, "tools_offered": tools_offered,
         "tools_used": tools_used, **host},
    )
    db.log_event(
        conn, user_id, "llm_response",
        {"provider": provider, "model": model, "text": answer, "usage": usage,
         "cached": False, "duration_ms": elapsed_ms, "degraded": bool(call.tools_stripped),
         **host},
    )
