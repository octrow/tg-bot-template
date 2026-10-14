"""Лестница моделей (спека llm-model-ladder).

Пороги в тестах - доли секунды через `config.llm_steps`: тот же цикл гонки,
только быстрее, без подмены часов."""

import asyncio
from types import SimpleNamespace

import pytest

from tgbot import config as cfg, db
from tgbot.agent import cache, failures, ladder, openrouter

from .conftest import log_rows

GLM, LUNA, DEEP = "z-ai/glm-5.3-flash", "openai/gpt-6-luna", "deepseek/deepseek-v4.1-flash"
FAST = (0.05, 0.1, 0.4)


def _config(models=(GLM, LUNA, DEEP), steps=FAST, **over):
    # Оставшийся ключ прямого OpenAI (ADR-0004) - лестница обязана его не видеть.
    base = dict(
        repo_path=None, openai_api_key="sk-openai",
        openrouter_api_key="sk-or", openrouter_models=models, llm_provider="openrouter",
        agent_cache_ttl_s=3600, llm_steps={cls: steps for cls in cfg.LLM_CALL_CLASSES},
    )
    return SimpleNamespace(**{**base, **over})


def _models(script: dict):
    """Подмена `ask_openai`: модель -> (задержка, ответ или исключение)."""
    calls: list[tuple[str, int]] = []

    async def ask(prompt, *, model, rung=None, **kwargs):
        calls.append((model, rung))
        delay, outcome = script[model]
        await asyncio.sleep(delay)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return ask, calls


# --- конфигурация и разметка ----------------------------------------------


def test_steps_default_and_override():
    loaded = cfg.load_config({"TELEGRAM_BOT_TOKEN": "t"}, env_files=())
    assert loaded.llm_steps["reference"] == (120.0, 240.0, 600.0)
    assert loaded.llm_steps["light"] == (20.0, 40.0, 60.0)

    env = {"TELEGRAM_BOT_TOKEN": "t", "TGBOT_LLM_REFERENCE_STEPS_S": "60,120,300"}
    loaded = cfg.load_config(env, env_files=())
    assert loaded.llm_steps["reference"] == (60.0, 120.0, 300.0)
    assert loaded.llm_steps["full"] == (120.0, 240.0, 600.0)


@pytest.mark.parametrize("raw", ["abc", "120,240", "-1,240,600", "120,120,600", "240,120,600"])
def test_bad_steps_fail_start_naming_the_variable(raw):
    env = {"TELEGRAM_BOT_TOKEN": "t", "TGBOT_LLM_FULL_STEPS_S": raw}
    with pytest.raises(cfg.ConfigError, match="TGBOT_LLM_FULL_STEPS_S"):
        cfg.load_config(env, env_files=())


@pytest.mark.parametrize("label, light, cls", [
    ("reference_question", False, "reference"),
    ("reference_chat", True, "light"),
    ("что-то новое", False, "full"),
])
def test_call_class(label, light, cls):
    assert ladder.call_class(label, light) == cls


@pytest.mark.parametrize("models, expected", [
    ((GLM,), [("openrouter", GLM)]),
    ((GLM, LUNA, DEEP), [("openrouter", GLM), ("openrouter", LUNA), ("openrouter", DEEP)]),
])
def test_ladder_rungs_are_openrouter_only(models, expected):
    """Ступени прямого OpenAI нет даже при оставшемся ключе (ADR-0004)."""
    assert ladder.ladder_rungs(_config(models=models)) == expected


# --- гонка ------------------------------------------------------------------


async def test_answer_before_soft_threshold_calls_nobody_else(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    ask, calls = _models({GLM: (0, "ответ glm")})
    monkeypatch.setattr(openrouter, "ask_openai", ask)

    assert await ladder.call_agent(_config(), conn, "вопрос", 42, "reference_question") == "ответ glm"
    assert calls == [(GLM, 0)]
    assert log_rows(conn, "llm_slow") == [] and log_rows(conn, "llm_timeout") == []


async def test_second_model_joins_at_threshold_and_wins(tmp_path, monkeypatch):
    """Основная зависла: на пороге подключается вторая и отвечает первой,
    основная отменена, в кэше ответ второй с её моделью."""
    conn = db.connect(tmp_path / "db.sqlite")
    ask, calls = _models({GLM: (5, "поздний ответ glm"), LUNA: (0, "ответ luna")})
    monkeypatch.setattr(openrouter, "ask_openai", ask)
    steps: list = []

    async def on_step(idx, provider):
        steps.append((idx, provider))

    answer = await ladder.call_agent(
        _config(), conn, "вопрос", 42, "reference_question", on_step=on_step,
    )

    assert answer == "ответ luna"
    assert calls == [(GLM, 0), (LUNA, 1)] and steps == [(1, "openrouter")]
    slow = log_rows(conn, "llm_slow")[0]
    assert slow["reason"] == "threshold" and slow["model"] == LUNA and slow["rung"] == 1
    assert log_rows(conn, "llm_cancelled")[0]["models"] == [GLM]
    key = cache.cache_key(42, "openrouter", False, "вопрос", "ru")
    assert db.cache_get(conn, key, 3600) == ("ответ luna", "openrouter", LUNA)


async def test_rejected_answer_starts_next_rung_without_waiting(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    ask, calls = _models({
        GLM: (0, failures.AnswerRejected("degenerate")), LUNA: (0, "годный ответ"),
    })
    monkeypatch.setattr(openrouter, "ask_openai", ask)

    answer = await ladder.call_agent(
        _config(steps=(30, 60, 90)), conn, "вопрос", 42, "reference_question",
    )

    assert answer == "годный ответ"
    assert log_rows(conn, "llm_slow")[0]["reason"] == "rejected"
    assert log_rows(conn, "llm_slow")[0]["elapsed_s"] < 1


async def test_provider_error_starts_next_model_at_once(tmp_path, monkeypatch):
    """Ошибка провайдера - как любой отказ: следующая модель OpenRouter сразу,
    не дожидаясь порога; другого провайдера нет (ADR-0004)."""
    conn = db.connect(tmp_path / "db.sqlite")
    ask, calls = _models({GLM: (0, RuntimeError("502")), LUNA: (0, "ответ luna")})
    monkeypatch.setattr(openrouter, "ask_openai", ask)

    answer = await ladder.call_agent(
        _config(steps=(30, 60, 90)), conn, "вопрос", 42, "reference_question",
    )

    assert answer == "ответ luna"
    assert calls == [(GLM, 0), (LUNA, 1)]
    assert log_rows(conn, "llm_slow")[0]["reason"] == "error"
    key = cache.cache_key(42, "openrouter", False, "вопрос", "ru")
    assert db.cache_get(conn, key, 3600) == ("ответ luna", "openrouter", LUNA)


async def test_openrouter_down_ends_without_another_provider(tmp_path, monkeypatch):
    """Лежит сам OpenRouter: отказали все модели - шаг кончается отказом, а
    запроса к прямому OpenAI нет, хотя ключ остался в конфиге."""
    conn = db.connect(tmp_path / "db.sqlite")
    ask, calls = _models({m: (0, RuntimeError("502")) for m in (GLM, LUNA, DEEP)})
    monkeypatch.setattr(openrouter, "ask_openai", ask)

    answer = await ladder.call_agent(
        _config(steps=(30, 60, 90)), conn, "вопрос", 42, "reference_question",
    )

    assert answer is None
    assert [c[0] for c in calls] == [GLM, LUNA, DEEP]
    assert log_rows(conn, "llm_timeout")[0]["reason"] == "all_failed"


async def test_hard_deadline_cancels_everything(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    ask, calls = _models({m: (5, "поздно") for m in (GLM, LUNA, DEEP)})
    monkeypatch.setattr(openrouter, "ask_openai", ask)
    answer = await ladder.call_agent(_config(), conn, "вопрос", 42, "reference_question")

    assert answer is None
    assert [c[0] for c in calls] == [GLM, LUNA, DEEP]
    timeout = log_rows(conn, "llm_timeout")[0]
    assert timeout["reason"] == "deadline"
    assert [r["outcome"] for r in timeout["rungs"]] == ["too_slow"] * 3
    assert db.cache_get(conn, cache.cache_key(42, "openrouter", False, "вопрос", "ru"), 3600) is None


async def test_all_rungs_rejected_is_a_rejected_failure(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    ask, _ = _models({m: (0, failures.AnswerRejected("too_long")) for m in (GLM, LUNA, DEEP)})
    monkeypatch.setattr(openrouter, "ask_openai", ask)

    race = ladder._Race(
        _config(), conn, "вопрос", 42, "reference_question", False, "ru",
        rungs=ladder.ladder_rungs(_config()),
    )
    answer, winner, failure = await ladder._run_ladder(race, (30, 60, 90))

    assert answer is None and failure.kind == failures.FAILURE_REJECTED
    timeout = log_rows(conn, "llm_timeout")[0]
    assert timeout["reason"] == "all_failed"
    assert [r["outcome"] for r in timeout["rungs"]] == ["too_long"] * 3


async def test_single_model_waits_to_the_hard_deadline(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    ask, calls = _models({GLM: (5, "поздно")})
    monkeypatch.setattr(openrouter, "ask_openai", ask)
    steps: list = []

    async def on_step(idx, provider):
        steps.append(idx)

    answer = await ladder.call_agent(
        _config(models=(GLM,)), conn, "вопрос", 42, "reference_question",
        on_step=on_step,
    )

    assert answer is None and calls == [(GLM, 0)] and steps == []
    assert log_rows(conn, "llm_slow") == []
    assert log_rows(conn, "llm_timeout")[0]["reason"] == "deadline"


async def test_rung_is_written_to_the_call_records(tmp_path, monkeypatch):
    import httpx

    from .fakes.openai_client import recording_client

    conn = db.connect(tmp_path / "db.sqlite")
    monkeypatch.setattr(httpx, "AsyncClient", recording_client("ок"))

    await ladder.call_agent(
        _config(models=(GLM,)), conn, "вопрос", 42, "тест", light=True,
    )

    assert log_rows(conn, "agent_call")[0]["rung"] == 0
    assert log_rows(conn, "llm_response")[0]["rung"] == 0


async def test_cancelling_the_call_stops_the_running_rungs(tmp_path, monkeypatch):
    """"Да" на быстрое предложение отменяет ответ: запросы ступеней не должны
    жить до жёсткого срока."""
    conn = db.connect(tmp_path / "db.sqlite")
    ask, _ = _models({m: (30, "ответ") for m in (GLM, LUNA, DEEP)})
    monkeypatch.setattr(openrouter, "ask_openai", ask)
    race = ladder._Race(
        _config(), conn, "вопрос", 42, "reference_question", False, "ru",
        rungs=ladder.ladder_rungs(_config()),
    )
    call = asyncio.create_task(ladder._run_ladder(race, (30, 60, 90)))
    await asyncio.sleep(0.01)
    assert race.running
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert not race.running
