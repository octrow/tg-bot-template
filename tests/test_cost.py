import json

from tgbot import db
from tgbot.locales.ru import STRINGS as RU

from .conftest import ADMIN_ID, log_rows, make_context, make_update

ADMIN_REFUSAL = RU["ADMIN_REFUSAL"]


def at(hm: str) -> str:
    """"10:05" -> ISO-метка внутри фиксированных суток: окна сессий и события
    журнала различаются только временем, дата в тестах роли не играет."""
    return f"2026-08-01T{hm}:00+00:00"


def make_session(conn, user_id, hour=10, status="done"):
    """Сессия с окном hour:00..hour:30 - расход привязывается по нему."""
    cur = conn.execute(
        "INSERT INTO sessions (user_id, status, mode, data, created_at, updated_at)"
        " VALUES (?, ?, 'echo', '{}', ?, ?)",
        (user_id, status, at(f"{hour}:00"), at(f"{hour}:30")),
    )
    conn.commit()
    return cur.lastrowid


def add_log(conn, user_id, kind, hm, payload=None):
    conn.execute(
        "INSERT INTO log (ts, user_id, kind, payload) VALUES (?, ?, ?, ?)",
        (at(hm), user_id, kind, json.dumps(payload or {})),
    )
    conn.commit()


PRICED_CALL = {
    "provider": "openrouter",
    "cost_usd": 0.0125,
    "usage": {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 300,
    },
}
# Прямой OpenAI цены не возвращает - в записи вызова нет `cost_usd`.
UNPRICED_CALL = {
    "provider": "openai",
    "usage": {"prompt_tokens": 50, "completion_tokens": 10},
}


def test_session_cost_sums_both_stages(conn):
    sid = make_session(conn, 5)
    add_log(conn, 5, "agent_files_read", "10:05", PRICED_CALL)
    add_log(conn, 5, "agent_call", "10:06", UNPRICED_CALL)
    add_log(conn, 5, "agent_cache_hit", "10:07", {})
    add_log(conn, 5, "agent_failed", "10:08", {})

    cost = db.session_cost(conn, sid)
    assert cost is not None
    assert cost["in_tok"] == 150
    assert cost["out_tok"] == 30
    assert cost["cache_read"] == 900
    assert cost["cache_write"] == 300
    assert cost["usd"] == 0.0125
    assert (cost["calls"], cost["cache_hits"], cost["failed"]) == (2, 1, 1)


def test_neighbour_session_events_are_not_counted(conn):
    first = make_session(conn, 5)
    second = make_session(conn, 5, hour=11)
    add_log(conn, 5, "agent_files_read", "10:05", PRICED_CALL)
    add_log(conn, 5, "agent_call", "11:05", UNPRICED_CALL)
    # Чужой расход в том же окне тоже не наш.
    add_log(conn, 6, "agent_files_read", "10:06", PRICED_CALL)

    first_cost, second_cost = db.session_cost(conn, first), db.session_cost(conn, second)
    assert first_cost is not None and second_cost is not None
    assert first_cost["in_tok"] == 100
    assert second_cost["in_tok"] == 50


def test_session_without_llm_calls_is_zeros_not_none(conn):
    sid = make_session(conn, 5)
    add_log(conn, 5, "agent_cache_hit", "10:05", {})

    cost = db.session_cost(conn, sid)
    assert cost is not None
    assert (cost["calls"], cost["in_tok"], cost["usd"]) == (0, 0, 0.0)
    assert cost["cache_hits"] == 1


def test_missing_session_is_none(conn):
    assert db.session_cost(conn, 999) is None


FULL_NO_FILES = {"tools_offered": True, "files": []}
FULL_WITH_FILES = {"tools_offered": True, "files": ["src/tgbot/db.py"]}
LIGHT_NO_FILES = {"tools_offered": False, "files": []}


def test_unverified_counts_full_calls_without_files(conn):
    sid = make_session(conn, 5)
    add_log(conn, 5, "agent_files_read", "10:05", FULL_NO_FILES)
    add_log(conn, 5, "agent_files_read", "10:06", FULL_NO_FILES)
    add_log(conn, 5, "agent_files_read", "10:07", FULL_WITH_FILES)

    assert db.session_cost(conn, sid)["unverified"] == 2


def test_unverified_light_calls_are_not_counted(conn):
    sid = make_session(conn, 5)
    add_log(conn, 5, "agent_files_read", "10:05", LIGHT_NO_FILES)
    add_log(conn, 5, "agent_files_read", "10:06", LIGHT_NO_FILES)

    assert db.session_cost(conn, sid)["unverified"] == 0


def test_unverified_session_without_calls_is_zero(conn):
    sid = make_session(conn, 5)
    assert db.session_cost(conn, sid)["unverified"] == 0


def test_unverified_neighbour_session_not_counted(conn):
    first = make_session(conn, 5)
    second = make_session(conn, 5, hour=11)
    add_log(conn, 5, "agent_files_read", "10:05", FULL_NO_FILES)
    add_log(conn, 5, "agent_files_read", "11:05", FULL_NO_FILES)

    assert db.session_cost(conn, first)["unverified"] == 1
    assert db.session_cost(conn, second)["unverified"] == 1


def test_unverified_historical_event_without_mode_flag_not_counted(conn):
    """Событие до этого change не несёт `tools_offered` - считается лёгким
    (design, "Migration Plan"), а не непроверенным."""
    sid = make_session(conn, 5)
    add_log(conn, 5, "agent_files_read", "10:05", {"files": []})

    assert db.session_cost(conn, sid)["unverified"] == 0


async def test_cost_reports_last_session(conn, handlers):
    make_session(conn, ADMIN_ID)
    sid = make_session(conn, ADMIN_ID, hour=11)
    add_log(conn, ADMIN_ID, "agent_files_read", "11:05", PRICED_CALL)

    update = make_update(ADMIN_ID, text="/cost")
    await handlers["cost"](update, make_context())

    reply = update.effective_message.replies[0]
    assert f"Сессия {sid}" in reply
    assert "$0.0125" in reply
    # Цена пришла от провайдера - оговорки о непосчитанном быть не должно.
    assert "провайдер не вернул" not in reply
    assert log_rows(conn, "command")[-1]["command"] == "/cost"


async def test_cost_reports_other_users_session_by_id(conn, handlers):
    sid = make_session(conn, 5)
    update = make_update(ADMIN_ID, text=f"/cost {sid}")
    await handlers["cost"](update, make_context(str(sid)))
    assert "user_id 5" in update.effective_message.replies[0]


async def test_cost_without_sessions_and_with_bad_arg(conn, handlers):
    update = make_update(ADMIN_ID, text="/cost")
    await handlers["cost"](update, make_context())
    assert update.effective_message.replies[0] == RU["COST_NO_SESSIONS"]

    for arg in ("999", "не-число"):
        update = make_update(ADMIN_ID, text=f"/cost {arg}")
        await handlers["cost"](update, make_context(arg))
        assert "не найдена" in update.effective_message.replies[0]


async def test_cost_is_admin_only(conn, handlers):
    db.add_user(conn, 5, added_by=ADMIN_ID)
    sid = make_session(conn, 5)
    update = make_update(5, text=f"/cost {sid}")
    await handlers["cost"](update, make_context(str(sid)))
    assert update.effective_message.replies == [ADMIN_REFUSAL]


async def test_help_shows_cost_only_to_admin(conn, handlers):
    db.add_user(conn, 5, added_by=ADMIN_ID)
    for user_id, expected in ((ADMIN_ID, True), (5, False)):
        update = make_update(user_id, text="/help")
        await handlers["help"](update, make_context())
        assert ("/cost" in update.effective_message.replies[0]) is expected


def test_unpriced_calls_are_counted_separately(conn):
    """Ноль долларов не должен читаться как "вызовы были бесплатными": вызовы
    ступени, которая цену не возвращает, считаются отдельно, чтобы `/cost` мог
    об этом сказать (спека llm-provider-selection)."""
    sid = make_session(conn, 5)
    add_log(conn, 5, "agent_call", "10:05", PRICED_CALL)
    add_log(conn, 5, "agent_call", "10:06", UNPRICED_CALL)

    cost = db.session_cost(conn, sid)
    assert cost is not None
    assert cost["unpriced"] == 1
    assert cost["usd"] == 0.0125


def test_all_calls_priced_leaves_no_unpriced(conn):
    sid = make_session(conn, 5)
    add_log(conn, 5, "agent_call", "10:05", PRICED_CALL)

    assert db.session_cost(conn, sid)["unpriced"] == 0


async def test_cost_text_notes_unpriced_calls(conn, handlers):
    """Оговорка печатается только когда цену действительно не вернули."""
    sid = make_session(conn, ADMIN_ID)
    add_log(conn, ADMIN_ID, "agent_call", "10:05", UNPRICED_CALL)

    update = make_update(ADMIN_ID, text=f"/cost {sid}")
    await handlers["cost"](update, make_context(str(sid)))
    assert "провайдер не вернул" in update.effective_message.replies[0]
    assert "Claude" not in update.effective_message.replies[0]


async def test_cost_text_has_no_note_when_everything_is_priced(conn, handlers):
    sid = make_session(conn, ADMIN_ID)
    add_log(conn, ADMIN_ID, "agent_call", "10:05", PRICED_CALL)

    update = make_update(ADMIN_ID, text=f"/cost {sid}")
    await handlers["cost"](update, make_context(str(sid)))
    assert "провайдер не вернул" not in update.effective_message.replies[0]
    assert "$0.0125" in update.effective_message.replies[0]


# --- одно правило "непроверенного ответа" для пометки и для /cost ---


def _open_window_session(conn, user_id) -> int:
    """Сессия, окно которой накрывает события, записанные прямо сейчас."""
    cur = conn.execute(
        "INSERT INTO sessions (user_id, status, mode, data, created_at, updated_at)"
        " VALUES (?, 'done', 'echo', '{}', '2000-01-01T00:00:00+00:00', '2999-01-01T00:00:00+00:00')",
        (user_id,),
    )
    conn.commit()
    return cur.lastrowid


def test_unverified_search_only_call_is_not_counted(conn):
    """Поиск без чтения целиком - обращение к коду: как у пометки под ответом."""
    sid = make_session(conn, 5)
    add_log(conn, 5, "agent_files_read", "10:05", {**FULL_NO_FILES, "tools_used": ["grep"]})
    add_log(conn, 5, "agent_files_read", "10:06", {**FULL_NO_FILES, "tools_used": []})

    assert db.session_cost(conn, sid)["unverified"] == 1


def test_unverified_counts_calls_with_stripped_tools(conn):
    sid = make_session(conn, 5)
    add_log(conn, 5, "agent_files_read", "10:05",
            {**FULL_WITH_FILES, "tools_used": ["read_file"], "tools_stripped": True})

    assert db.session_cost(conn, sid)["unverified"] == 1


async def test_grep_only_answer_has_no_mark_and_is_not_counted(conn, tmp_path, monkeypatch):
    """Настоящий путь вызова: ответ, найденный только grep, не помечен и в
    `/cost` непроверенным не считается."""
    import httpx

    from tgbot.agent import openrouter
    from tgbot.agent.result import is_unverified

    from .fakes.openai_client import GrepOnlyClient, openai_payloads

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("строка про настройки", encoding="utf-8")
    openai_payloads.clear()
    monkeypatch.setattr(httpx, "AsyncClient", GrepOnlyClient)
    sid = _open_window_session(conn, 42)

    answer = await openrouter.ask_openai(
        "как это работает?", api_key="sk-x", model="m", conn=conn, user_id=42,
        context_label="тест", roots=[docs],
    )

    assert answer.files_read == [] and not is_unverified(answer)
    assert db.session_cost(conn, sid)["unverified"] == 0
