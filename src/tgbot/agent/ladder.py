"""Лестница моделей (спека llm-model-ladder, ADR 0003): гонка
ступеней по порогам класса вызова, кэш и сообщение об отказе."""

import asyncio
import logging
import time
from dataclasses import dataclass
from sqlite3 import Connection

from .. import db, i18n
from . import openrouter
from .cache import cache_hit, cache_key, cache_store
from .failures import (
    FAILURE_NO_KEY, FAILURE_REJECTED, FAILURE_TIMEOUT, FAILURE_UNKNOWN, LADDER_DEADLINE,
    StageFailure, failure_of, notify_agent_down,
)
from .stages import (
    FULL_MAX_TOKENS, LIGHT_MAX_TOKENS, PROVIDER_OPENROUTER, STAGES, effective_provider,
    knowledge_roots, stage_models, stage_url,
)

log = logging.getLogger(__name__)

# Класс вызова по `context_label`: от него зависят пороги лестницы
# (`config.llm_steps`). Неизвестный префикс - класс `full`.
CALL_CLASS_PREFIXES = (
    ("reference_question", "reference"),
)


def call_class(context_label: str, light: bool = False) -> str:
    if light:
        return "light"
    for prefix, cls in CALL_CLASS_PREFIXES:
        if context_label.startswith(prefix):
            return cls
    return "full"


def ladder_rungs(config) -> list[tuple[str, str]]:
    """Ступени лестницы `(провайдер, модель)` по порядку: модели OpenRouter.
    Запасного провайдера нет (ADR 0004)."""
    provider = effective_provider(config)
    return [(provider, model) for model in stage_models(config, provider)]


def _steps(config, context_label: str, light: bool) -> tuple[float, ...]:
    from ..config import LLM_STEPS_FULL, LLM_STEPS_LIGHT

    cls = call_class(context_label, light)
    steps = getattr(config, "llm_steps", None) or {}
    return steps.get(cls) or (LLM_STEPS_LIGHT if light else LLM_STEPS_FULL)


@dataclass
class _Race:
    """Ход одной гонки: что запущено, чем кончилось, куда журналировать."""

    config: object
    conn: Connection
    prompt: str
    user_id: int
    context_label: str
    light: bool
    lang: str
    rungs: list[tuple[str, str]]
    on_step: object = None
    started: float = 0.0

    def __post_init__(self):
        self.running: dict[asyncio.Task, int] = {}
        self.outcomes: dict[int, str] = {}
        self.failures: list[StageFailure] = []

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def call(self, idx: int):
        provider, model = self.rungs[idx]
        return _try_stage(
            self.config, self.conn, self.prompt, self.user_id, self.context_label,
            self.light, lang=self.lang, provider=provider, model=model, rung=idx,
        )

    async def launch(self, idx: int, reason: str | None) -> None:
        provider, model = self.rungs[idx]
        task = asyncio.create_task(self.call(idx))
        self.running[task] = idx
        self.outcomes[idx] = "running"
        if idx == 0:
            return
        db.log_event(self.conn, self.user_id, "llm_slow", {
            "context": self.context_label, "rung": idx, "provider": provider, "model": model,
            "reason": reason, "elapsed_s": round(self.elapsed(), 1),
        })
        if self.on_step is not None:
            try:
                await self.on_step(idx, provider)
            except Exception:  # noqa: BLE001 - статус вторичен, гонка важнее
                log.warning("Статус ступени %s не обновлён", idx, exc_info=True)

    async def stop_running(self, outcome: str) -> list[str]:
        """Отменяет идущие запросы у нас; их поздний ответ никуда не попадёт."""
        tasks = list(self.running)
        for task in tasks:
            task.cancel()
            self.outcomes[self.running[task]] = outcome
        await asyncio.gather(*tasks, return_exceptions=True)
        models = [self.rungs[self.running[t]][1] for t in tasks]
        self.running.clear()
        return models

    def log_timeout(self, reason: str) -> None:
        db.log_event(self.conn, self.user_id, "llm_timeout", {
            "context": self.context_label, "reason": reason,
            "elapsed_s": round(self.elapsed(), 1),
            "rungs": [
                {"rung": i, "provider": p, "model": m, "outcome": self.outcomes.get(i, "not_started")}
                for i, (p, m) in enumerate(self.rungs)
            ],
        })


def _deadline_failure(rungs, hard: float) -> StageFailure:
    """Отказ по жёсткому порогу; `detail` - сам порог в секундах: текст
    пользователю называет настоящее время ожидания, а оно своё у класса вызова."""
    return StageFailure(rungs[0][0], FAILURE_TIMEOUT, detail=f"{hard:g}", exception=LADDER_DEADLINE)


async def _run_ladder(race: _Race, steps: tuple[float, ...]):
    """`_ladder`, чьи ступени-задачи не переживают отмену вызова: "Да" на
    быстрое предложение справки отменяет ответ, и без этого запросы жили бы до
    жёсткого срока - тратили токены и писали `llm_*` после ухода человека."""
    try:
        return await _ladder(race, steps)
    except asyncio.CancelledError:
        await race.stop_running("cancelled")
        raise


async def _single_rung(race: _Race, hard: float):
    """Одна ступень - без задач и `asyncio.wait`: те же правила, но ни одного
    лишнего оборота цикла."""
    rungs = race.rungs
    race.outcomes[0] = "running"
    try:
        async with asyncio.timeout(hard):
            answer, failure = await race.call(0)
    except TimeoutError:
        race.outcomes[0] = "too_slow"
        race.log_timeout("deadline")
        return None, None, _deadline_failure(rungs, hard)
    race.outcomes[0] = "won" if answer is not None else failure.kind
    return (answer, rungs[0], None) if answer is not None else (None, None, failure)


async def _first_answer(race: _Race, timeout: float):
    """Ждёт идущие ступени не дольше `timeout`: победитель `(ответ, ступень,
    None)` либо None; отказы ступеней остаются в гонке."""
    done = set()
    if race.running:
        done, _ = await asyncio.wait(
            race.running, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    for task in done:
        idx = race.running.pop(task)
        answer, failure = task.result()
        if answer is not None:
            race.outcomes[idx] = "won"
            cancelled = await race.stop_running("cancelled")
            if cancelled:
                db.log_event(race.conn, race.user_id, "llm_cancelled", {
                    "context": race.context_label, "winner": race.rungs[idx][1],
                    "models": cancelled,
                })
            return answer, race.rungs[idx], None
        race.outcomes[idx] = failure.detail if failure.kind == FAILURE_REJECTED else failure.kind
        race.failures.append(failure)
    return None


async def _ladder(race: _Race, steps: tuple[float, ...]):
    """Гонка ступеней: (ответ, (провайдер, модель) победителя, отказ).

    Следующая ступень подключается параллельно: по порогу или сразу - когда
    все идущие запросы дали брак или ошибку (в том числе ошибку самого
    OpenRouter: другого провайдера нет, ADR 0004).
    Побеждает первый ответ, прошедший проверки; брак - это `AnswerRejected`,
    его `_try_stage` уже превратил в отказ рода `rejected`."""
    *soft, hard = steps
    rungs = race.rungs
    race.started = time.monotonic()
    if len(rungs) == 1:
        return await _single_rung(race, hard)
    await race.launch(0, None)
    nxt = 1

    def timed(idx: int) -> bool:  # у ступени есть порог по времени
        return idx < len(rungs) and idx <= len(soft)

    while True:
        until = soft[nxt - 1] if timed(nxt) else hard
        won = await _first_answer(race, max(min(until, hard) - race.elapsed(), 0))
        if won is not None:
            return won
        if race.elapsed() >= hard:
            await race.stop_running("too_slow")
            race.log_timeout("deadline")
            return None, None, _deadline_failure(rungs, hard)
        if race.running and timed(nxt) and race.elapsed() >= soft[nxt - 1]:
            await race.launch(nxt, "threshold")
            nxt += 1
            continue
        if race.running:
            continue
        if nxt >= len(rungs):
            if len(rungs) > 1:
                race.log_timeout("all_failed")
            return None, None, race.failures[-1]
        # Все идущие кончились отказом - следующая ступень сразу.
        last = race.failures[-1]
        await race.launch(nxt, "rejected" if last.kind == FAILURE_REJECTED else "error")
        nxt += 1


async def call_agent(
    config, conn, prompt: str, user_id: int, context_label: str,
    message=None, light: bool = False,
    lang: str = i18n.DEFAULT_LANG, on_step=None, notify: bool = True,
    question: str = "",
) -> str | None:
    """Вызов агента по лестнице моделей - либо ответ, либо None с сообщением.

    Лестница (ADR 0003, спека llm-model-ladder): модели OpenRouter по
    порядку, запасного провайдера нет (ADR 0004); следующая подключается параллельно
    по порогу, по браку или ошибке, побеждает первый годный ответ. Пороги -
    по классу вызова (`call_class`, `config.llm_steps`). `on_step(idx,
    provider)` - колбэк подключения ступени: им статус ожидания объясняет
    пользователю, что происходит.

    Если ни одна ступень не ответила и передан `message` (и `notify`),
    пользователь получает сообщение, где назван род отказа (см.
    `notify_agent_down`). `notify=False` - исход решает вызывающая сторона
    (облегчённая Справка). `light` - дешёвый режим без инструментов.
    `question` - вопрос пользователя для Отзыва и пуша о сбое.
    """
    ttl_s = getattr(config, "agent_cache_ttl_s", 0)
    provider = effective_provider(config)

    # Ключ задаёт действующая ступень, а не победитель: иначе следующий такой
    # же вопрос промахнулся бы мимо кэша, потому что в прошлый раз ответила
    # запасная модель.
    key = cache_key(user_id, provider, light, prompt, lang) if ttl_s else None
    if key is not None:
        cached = cache_hit(conn, user_id, key, ttl_s, context_label)
        if cached is not None:
            return cached

    race = _Race(
        config, conn, prompt, user_id, context_label, light, lang,
        rungs=ladder_rungs(config), on_step=on_step,
    )
    answer, winner, failure = await _run_ladder(race, _steps(config, context_label, light))
    if answer is not None:
        # Непроверенный ответ полного вызова не кэшируем (`cacheable`), брак
        # сюда не доходит вовсе. В записи - реальные ступень и модель.
        cache_store(conn, key, answer, provider=winner[0], model=winner[1], light=light)
        return answer
    if notify:
        await notify_agent_down(
            conn, message, failure, lang=lang, user_id=user_id,
            context_label=context_label, question=question,
            models=[race.rungs[i][1] for i in sorted(race.outcomes)],
        )
    return None


def _stage_request(config, stage: dict, *, provider: str, model: str, rung, light: bool) -> dict:
    """Аргументы `ask_openai` из описания ступени (`STAGES`) и конфига."""
    return {
        "model": model,
        "provider": provider,
        "rung": rung,
        "chat_url": stage_url(config, stage["chat_url"]),
        "responses_url": stage_url(config, stage["responses_url"]),
        "light_reasoning": stage["light_reasoning"],
        "routing": stage["routing"],
        "judge_key": getattr(config, "typesafe_api_key", None),
        "max_tokens": LIGHT_MAX_TOKENS if light else FULL_MAX_TOKENS,
        # В лёгком режиме инструментов нет: выбор из четырёх кнопок не
        # стоит прогона по репозиторию.
        "roots": None if light else knowledge_roots(config),
    }


def _log_stage_failed(conn, user_id: int, context_label: str, failure: StageFailure) -> None:
    db.log_event(
        conn, user_id, f"{failure.stage}_failed",
        {"context": context_label, **failure.as_log()},
    )


async def _try_stage(
    config, conn, prompt: str, user_id: int, context_label: str, light: bool = False,
    lang: str = i18n.DEFAULT_LANG, provider: str = PROVIDER_OPENROUTER,
    model: str | None = None, rung: int | None = None,
) -> tuple[str | None, StageFailure]:
    """Действующая ступень: (ответ, причина отказа).

    Ответ None - ступень не сработала, и причина названа родом, а не пустой
    строкой: раньше отсутствие ключа и таймаут были
    неотличимы у получателя, и оба показывались как чужая проблема.
    `provider` - ступень из `STAGES` (вход, ключ, модели, размышление лёгкого
    запроса, выбор хостера описаны там).
    `model` - модель ступени лестницы: запрос уходит с ней одной, без
    `models`/`route` - дублёров переключает лестница, а не провайдер (ADR 0003).
    Без `model` - основная модель ступени.
    """
    stage = STAGES[provider]
    api_key = getattr(config, stage["key_attr"], None)
    if not api_key:
        # Лечится конфигурацией, а не повтором: называем переменную окружения,
        # выводя её имя из `STAGES`, чтобы второй список имён не разъехался.
        failure = StageFailure(
            provider, FAILURE_NO_KEY, detail=stage["key_attr"].upper()
        )
        log.warning("Ступень %s не настроена (%s)", provider, context_label)
        _log_stage_failed(conn, user_id, context_label, failure)
        return None, failure
    request = _stage_request(
        config, stage, provider=provider, rung=rung, light=light,
        model=model or stage_models(config, provider)[0],
    )
    try:
        answer = await openrouter.ask_openai(
            prompt, api_key=api_key, conn=conn, user_id=user_id,
            context_label=context_label, lang=lang, **request,
        )
    except Exception as exc:
        failure = failure_of(provider, exc)
        log.warning(
            "Ступень %s недоступна (%s): %s (%s)",
            provider, context_label, failure.kind, failure.exception,
        )
        _log_stage_failed(conn, user_id, context_label, failure)
        return None, failure
    return answer, StageFailure(provider, FAILURE_UNKNOWN)
