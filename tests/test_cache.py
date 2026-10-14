from datetime import datetime, timedelta, UTC
from types import SimpleNamespace

import pytest

from tgbot import db
from tgbot.agent import cache, failures, ladder
from tgbot.agent.result import AgentResult

from .conftest import log_rows


def _config(ttl_s: float, **overrides) -> SimpleNamespace:
    base = dict(
        repo_path=None,
        openrouter_api_key="sk-or-test",
        openrouter_models=("z-ai/glm-5.3-flash",),
        llm_provider="openrouter",
        agent_cache_ttl_s=ttl_s,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_cache_hit_skips_ask_and_logs(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    calls = []

    async def ask(prompt, **kwargs):
        calls.append(prompt)
        return "ответ агента"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    config = _config(60.0)

    answer1 = await ladder.call_agent(config, conn, "вопрос", 42, "тест")
    answer2 = await ladder.call_agent(config, conn, "вопрос", 42, "тест")

    assert answer1 == "ответ агента"
    assert answer2 == "ответ агента"
    assert len(calls) == 1  # второй запрос обошёлся кэшем
    assert len(log_rows(conn, "agent_cache_hit")) == 1


async def test_same_short_answer_of_two_users_is_not_shared(tmp_path, monkeypatch):
    """На разборе коротких ответов промпт у разных пользователей совпадает ("да",
    "не знаю") - чужой ответ выдавать нельзя, пользователь входит в ключ."""
    conn = db.connect(tmp_path / "db.sqlite")
    asked = []

    async def ask(prompt, **kwargs):
        asked.append(kwargs["user_id"])
        return f"разбор для {kwargs['user_id']}"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    config = _config(60.0)

    first = await ladder.call_agent(config, conn, "поле title: да", 1, "тест")
    second = await ladder.call_agent(config, conn, "поле title: да", 2, "тест")

    assert first == "разбор для 1"
    assert second == "разбор для 2"
    assert asked == [1, 2]  # второму спросили заново, а не отдали чужое


async def test_repeated_step_of_same_user_still_comes_from_cache(tmp_path, monkeypatch):
    """Плата за изоляцию - только межпользовательские попадания: повтор шага
    у одного человека кэш обслуживает по-прежнему."""
    conn = db.connect(tmp_path / "db.sqlite")
    asked = []

    async def ask(prompt, **kwargs):
        asked.append(kwargs["user_id"])
        return "ответ агента"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    config = _config(60.0)

    assert await ladder.call_agent(config, conn, "поле title: да", 7, "тест") == "ответ агента"
    assert await ladder.call_agent(config, conn, "поле title: да", 7, "тест") == "ответ агента"

    assert asked == [7]
    assert len(log_rows(conn, "agent_cache_hit")) == 1


async def test_cache_expired_entry_recalled_and_overwritten(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    calls = []

    async def ask(prompt, **kwargs):
        calls.append(prompt)
        return "новый ответ"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    config = _config(60.0)

    # Ключ несёт версию кэша, ступень и режим - собираем его тем же способом.
    prompt_hash = cache.cache_key(42, "openrouter", False, "вопрос", "ru")
    stale = (datetime.now(UTC) - timedelta(seconds=120)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO agent_cache (prompt_hash, answer, created_at) VALUES (?, ?, ?)",
        (prompt_hash, "старый ответ", stale),
    )
    conn.commit()

    answer = await ladder.call_agent(config, conn, "вопрос", 42, "тест")

    assert answer == "новый ответ"
    assert len(calls) == 1
    row = conn.execute(
        "SELECT answer FROM agent_cache WHERE prompt_hash = ?", (prompt_hash,)
    ).fetchone()
    assert row["answer"] == "новый ответ"


async def test_cache_ttl_zero_disables_cache(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    calls = []

    async def ask(prompt, **kwargs):
        calls.append(prompt)
        return "ответ агента"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    config = _config(0.0)

    await ladder.call_agent(config, conn, "вопрос", 42, "тест")
    await ladder.call_agent(config, conn, "вопрос", 42, "тест")

    assert len(calls) == 2  # кэш выключен - каждый раз ask
    assert conn.execute("SELECT COUNT(*) AS n FROM agent_cache").fetchone()["n"] == 0


async def test_agent_error_not_cached(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")

    async def ask_fails(prompt, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask_fails)

    config = _config(60.0)

    answer = await ladder.call_agent(config, conn, "вопрос", 42, "тест")

    assert answer is None
    assert conn.execute("SELECT COUNT(*) AS n FROM agent_cache").fetchone()["n"] == 0


async def test_empty_answer_is_not_cached(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")

    async def ask(prompt, **kwargs):
        return "   "

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    await ladder.call_agent(_config(60.0), conn, "вопрос", 42, "тест")

    assert conn.execute("SELECT COUNT(*) AS n FROM agent_cache").fetchone()["n"] == 0


async def test_cache_version_change_invalidates_entries(tmp_path, monkeypatch):
    """Поднятая версия кэша не даёт подхватить ответы прошлых промптов."""
    conn = db.connect(tmp_path / "db.sqlite")
    calls = []

    async def ask(prompt, **kwargs):
        calls.append(prompt)
        return "ответ агента"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    config = _config(60.0)
    await ladder.call_agent(config, conn, "вопрос", 42, "тест")

    monkeypatch.setattr(cache, "CACHE_VERSION", cache.CACHE_VERSION + 1)
    await ladder.call_agent(config, conn, "вопрос", 42, "тест")

    assert len(calls) == 2


async def test_light_mode_answer_is_not_reused_by_full_mode(tmp_path, monkeypatch):
    """Лёгкий режим отвечает без репозитория - его ответ не ответ полного."""
    conn = db.connect(tmp_path / "db.sqlite")
    calls = []

    async def ask(prompt, **kwargs):
        # Лёгкий режим виден ступени отсутствием каталогов для чтения.
        calls.append(kwargs.get("roots") is None)
        return "ответ агента"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    config = _config(60.0)
    await ladder.call_agent(config, conn, "вопрос", 42, "тест", light=True)
    await ladder.call_agent(config, conn, "вопрос", 42, "тест", light=False)

    assert calls == [True, False]


async def test_full_call_without_files_is_not_cached(tmp_path, monkeypatch):
    """Полный вызов без прочитанных файлов не подтверждён кодом - в кэш не
    попадает, повторный запрос снова идёт к провайдеру (spec agent-cache-hygiene)."""
    conn = db.connect(tmp_path / "db.sqlite")
    calls = []

    async def ask(prompt, **kwargs):
        calls.append(prompt)
        return AgentResult("ответ без чтения кода", files_read=[])

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    config = _config(60.0)

    await ladder.call_agent(config, conn, "вопрос", 42, "тест")
    await ladder.call_agent(config, conn, "вопрос", 42, "тест")

    assert len(calls) == 2  # второй раз снова спросили провайдера
    assert conn.execute("SELECT COUNT(*) AS n FROM agent_cache").fetchone()["n"] == 0
    assert log_rows(conn, "agent_cache_hit") == []


async def test_full_call_with_files_is_cached(tmp_path, monkeypatch):
    """Полный вызов, прочитавший хотя бы один файл, - подтверждённый ответ,
    кэшируется как раньше."""
    conn = db.connect(tmp_path / "db.sqlite")
    calls = []

    async def ask(prompt, **kwargs):
        calls.append(prompt)
        return AgentResult("ответ по коду", files_read=["/repo/main.py"])

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    config = _config(60.0)

    answer1 = await ladder.call_agent(config, conn, "вопрос", 42, "тест")
    answer2 = await ladder.call_agent(config, conn, "вопрос", 42, "тест")

    assert answer1 == answer2 == "ответ по коду"
    assert len(calls) == 1  # второй раз обошлись кэшем


async def test_light_call_without_files_is_cached(tmp_path, monkeypatch):
    """Лёгкий вызов инструментов чтения не предлагает - пустой `files_read`
    ему не в минус, кэшируется как раньше."""
    conn = db.connect(tmp_path / "db.sqlite")
    calls = []

    async def ask(prompt, **kwargs):
        calls.append(prompt)
        return AgentResult("выбор кнопки", files_read=[])

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    config = _config(60.0)

    answer1 = await ladder.call_agent(config, conn, "вопрос", 42, "тест", light=True)
    answer2 = await ladder.call_agent(config, conn, "вопрос", 42, "тест", light=True)

    assert answer1 == answer2 == "выбор кнопки"
    assert len(calls) == 1


async def test_previous_cache_version_entries_not_reused(tmp_path, monkeypatch):
    """Записи прежней версии критерия кэширования не должны подхватываться -
    иначе деградированный ответ, накопленный до фикса, доживёт до конца TTL."""
    conn = db.connect(tmp_path / "db.sqlite")
    calls = []

    async def ask(prompt, **kwargs):
        calls.append(prompt)
        return "новый ответ"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    config = _config(60.0)

    old_raw = f"{cache.CACHE_VERSION - 1}|42|claude|0|вопрос"
    import hashlib as _hashlib

    prompt_hash = _hashlib.sha256(old_raw.encode()).hexdigest()
    conn.execute(
        "INSERT INTO agent_cache (prompt_hash, answer, created_at) VALUES (?, ?, datetime('now'))",
        (prompt_hash, "старый деградированный ответ"),
    )
    conn.commit()

    answer = await ladder.call_agent(config, conn, "вопрос", 42, "тест")

    assert answer == "новый ответ"
    assert len(calls) == 1


async def test_cache_hit_logs_provider_of_original_answer(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")

    async def ask(prompt, **kwargs):
        return "ответ агента"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    config = _config(60.0)
    await ladder.call_agent(config, conn, "вопрос", 42, "тест")
    await ladder.call_agent(config, conn, "вопрос", 42, "тест")

    cached = [r for r in log_rows(conn, "llm_response") if r.get("cached")]
    assert len(cached) == 1
    assert cached[0]["provider"] == "openrouter"


# --- Кэшируется только прошедший проверки ответ (спека agent-cache-hygiene) ---


def _cache_rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM agent_cache").fetchone()[0]


@pytest.mark.parametrize("defect", ["truncated", "degenerate", "too_long"])
async def test_rejected_answer_is_never_cached(tmp_path, monkeypatch, defect):
    """Брак любого рода не создаёт запись, повтор снова идёт к провайдеру."""
    conn = db.connect(tmp_path / "db.sqlite")
    calls = []

    async def ask(prompt, **kwargs):
        calls.append(prompt)
        raise failures.AnswerRejected(defect)

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    for _ in range(2):
        assert await ladder.call_agent(_config(3600.0), conn, "вопрос", 42, "тест", light=True) is None

    assert _cache_rows(conn) == 0
    assert len(calls) == 2 and log_rows(conn, "agent_cache_hit") == []


async def test_good_answer_stays_cached_when_delivery_failed(tmp_path, monkeypatch):
    """Кэш пишется после проверок, а не после доставки: сетевой сбой отправки
    не выбрасывает годный ответ, повтор берёт его из кэша."""
    conn = db.connect(tmp_path / "db.sqlite")
    calls = []

    async def ask(prompt, **kwargs):
        calls.append(prompt)
        return "годный ответ"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    # Ответ получен, а `reply_text` с ним упал на сети - агент об этом не знает.
    await ladder.call_agent(_config(3600.0), conn, "вопрос", 42, "тест", light=True)

    again = await ladder.call_agent(_config(3600.0), conn, "вопрос", 42, "тест", light=True)

    assert again == "годный ответ" and len(calls) == 1
    assert log_rows(conn, "agent_cache_hit")


async def test_record_from_version_10_is_not_served(tmp_path, monkeypatch):
    """Мусор, закэшированный до версии 11 (версия 10), после релиза не выдаётся."""
    conn = db.connect(tmp_path / "db.sqlite")
    assert cache.CACHE_VERSION > 10
    monkeypatch.setattr(cache, "CACHE_VERSION", 10)
    db.cache_put(conn, cache.cache_key(42, "openrouter", False, "вопрос", "ru"),
                 "мусор s126", provider="openrouter")
    monkeypatch.undo()

    async def ask(prompt, **kwargs):
        return "новый ответ"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    assert await ladder.call_agent(_config(3600.0), conn, "вопрос", 42, "тест") == "новый ответ"
