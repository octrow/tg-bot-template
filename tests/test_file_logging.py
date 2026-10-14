import json
import logging
import re
import time
from types import SimpleNamespace


from tgbot import db, journal, logs_view
from tgbot.agent import ladder, openrouter, stages

from .conftest import log_rows


def _read_jsonl(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _logs_dir(tmp_path):
    return tmp_path / "logs"


async def test_session_event_written_to_its_own_file(tmp_path, conn, monkeypatch):
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(_logs_dir(tmp_path)))
    session = db.create_session(conn, 42, "_intro", {"fields": {}})

    db.log_event(conn, 42, "message", {"session_id": session["id"], "text": "привет"})

    files = list(_logs_dir(tmp_path).iterdir())
    assert len(files) == 1
    name = files[0].name
    assert name.endswith(f"-u42-s{session['id']}.jsonl")
    lines = _read_jsonl(files[0])
    assert lines[-1]["kind"] == "message"
    assert lines[-1]["payload"]["text"] == "привет"


async def test_session_event_without_explicit_id_found_via_last_session(tmp_path, conn, monkeypatch):
    # Событие сессии (вопрос/ответ/выбор), где вызывающий код не передаёт
    # session_id явно - находится по последней сессии пользователя.
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(_logs_dir(tmp_path)))
    session = db.create_session(conn, 7, "_intro", {"fields": {}})

    db.log_event(conn, 7, "callback", {"data": "field:title"})

    files = list(_logs_dir(tmp_path).iterdir())
    assert len(files) == 1
    assert files[0].name.endswith(f"-u7-s{session['id']}.jsonl")


async def test_event_without_session_written_to_bot_jsonl(tmp_path, conn, monkeypatch):
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(_logs_dir(tmp_path)))

    db.log_event(conn, None, "startup", {"db_path": "x.db"})
    db.log_event(conn, 999, "access_denied", {"replied": True})  # 999 никогда не заводил сессию

    bot_log = _logs_dir(tmp_path) / "bot.jsonl"
    assert bot_log.is_file()
    lines = _read_jsonl(bot_log)
    kinds = [line["kind"] for line in lines]
    assert kinds == ["startup", "access_denied"]


async def test_event_does_not_query_db_to_pick_its_file(tmp_path, conn, monkeypatch):
    """Имя файла-зеркала стоило двух запросов к БД на КАЖДОЕ событие журнала -
    а сессия за это время не меняется."""
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(_logs_dir(tmp_path)))
    session = db.create_session(conn, 42, "_intro", {"fields": {}})
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    db.log_event(conn, 42, "question_asked", {"session_id": session["id"]})
    db.log_event(conn, 42, "message", {"text": "привет"})  # session_id не передан

    conn.set_trace_callback(None)
    assert [s for s in statements if s.lstrip().upper().startswith("SELECT")] == []
    files = list(_logs_dir(tmp_path).iterdir())
    assert len(files) == 1  # оба события всё равно легли в файл своей сессии
    assert files[0].name.endswith(f"-u42-s{session['id']}.jsonl")



class _FakeResponse:
    def __init__(self, data: dict):
        self._data = data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._data


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_llm_request_and_response_logged_with_full_texts(tmp_path, conn, monkeypatch):
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(_logs_dir(tmp_path)))

    class _Answering(_FakeClient):
        async def post(self, url, headers=None, json=None):
            return _FakeResponse({
                "model": "z-ai/glm-5.3-flash",
                "choices": [{"message": {"content": "ответ агента"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.01},
            })

    monkeypatch.setattr(openrouter.httpx, "AsyncClient", _Answering)

    await openrouter.ask_openai(
        "полный текст промпта",
        api_key="sk-or-test",
        model="z-ai/glm-5.3-flash",
        provider=stages.PROVIDER_OPENROUTER,
        conn=conn,
        user_id=42,
        context_label="reference_question",
    )

    requests = log_rows(conn, "llm_request")
    responses = log_rows(conn, "llm_response")
    assert requests[-1]["provider"] == "openrouter"
    assert requests[-1]["prompt"] == "полный текст промпта"
    assert responses[-1]["provider"] == "openrouter"
    assert responses[-1]["model"] == "z-ai/glm-5.3-flash"
    assert responses[-1]["text"] == "ответ агента"
    assert responses[-1]["cached"] is False
    assert log_rows(conn, "agent_call")[-1]["cost_usd"] == 0.01


async def test_cache_hit_logs_llm_response_cached_without_request(tmp_path, conn, monkeypatch):
    async def ask(prompt, **kwargs):
        return "ответ агента"

    monkeypatch.setattr("tgbot.agent.openrouter.ask_openai", ask)

    config = SimpleNamespace(
        repo_path=None,
        openrouter_api_key="sk-or-test",
        openrouter_models=("z-ai/glm-5.3-flash",),
        agent_cache_ttl_s=60.0,
    )

    await ladder.call_agent(config, conn, "вопрос", 42, "тест")  # заполняет кэш
    await ladder.call_agent(config, conn, "вопрос", 42, "тест")  # кэш-попадание

    responses = log_rows(conn, "llm_response")
    assert len(responses) == 1  # только на кэш-попадании, не на первом (реальном) вызове
    assert responses[0]["cached"] is True
    requests = log_rows(conn, "llm_request")
    assert requests == []  # у фейкового ask нет своего llm_request - его тут и не должно быть


async def test_rotation_deletes_oldest_and_spares_active_session(tmp_path, conn, monkeypatch):
    logs_dir = _logs_dir(tmp_path)
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(logs_dir))
    monkeypatch.setenv("TGBOT_LOGS_MAX_BYTES", "20")
    logs_dir.mkdir(parents=True)

    # Активная сессия пользователя 1 - её файл должен уцелеть.
    active_session = db.create_session(conn, 1, "_intro", {})
    active_file = logs_dir / f"20200101-000000-u1-s{active_session['id']}.jsonl"
    active_file.write_text("x" * 15, encoding="utf-8")

    old_file = logs_dir / "20190101-000000-u2-s9999.jsonl"
    old_file.write_text("y" * 15, encoding="utf-8")
    import os
    import time

    old_time = time.time() - 1000
    os.utime(old_file, (old_time, old_time))

    # Новая сессия запускает ротацию: суммарный размер (30) > лимита (20).
    db.create_session(conn, 3, "_intro", {})

    assert not old_file.exists()  # старейший неактивный файл удалён
    assert active_file.exists()  # файл активной сессии не тронут


async def test_rotation_truncates_bot_jsonl_as_last_resort(tmp_path, conn, monkeypatch):
    logs_dir = _logs_dir(tmp_path)
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(logs_dir))
    monkeypatch.setenv("TGBOT_LOGS_MAX_BYTES", "5")
    logs_dir.mkdir(parents=True)

    bot_log = logs_dir / "bot.jsonl"
    bot_log.write_text("z" * 50, encoding="utf-8")

    db.create_session(conn, 5, "_intro", {})

    assert bot_log.exists()
    assert bot_log.stat().st_size == 0  # усечён, а не удалён


async def test_write_error_does_not_raise_and_warns_once(tmp_path, conn, monkeypatch, caplog):
    # Каталог логов упирается в обычный файл - mkdir(parents=True) не сможет
    # создать промежуточный компонент пути, запись должна тихо провалиться.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("занято", encoding="utf-8")
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(blocker / "logs"))

    with caplog.at_level(logging.WARNING):
        db.log_event(conn, 1, "startup", {"ok": True})  # не должно бросить исключение

    assert any("file-log mirror failed" in r.message for r in caplog.records)
    # событие всё равно осталось в SQLite - файловый лог вторичен
    assert log_rows(conn, "startup")[-1]["ok"] is True


# --- 2. Перечисление файлов журнала (task 2.1, 2.2) ----------------------


def test_logs_dir_is_public(tmp_path):
    # Задача 2.1: путь к каталогу логов должен быть доступен без обращения к
    # приватному имени - единственный источник истины о каталоге.
    logs_dir = _logs_dir(tmp_path)
    assert journal.logs_dir() == logs_dir


def test_list_log_files_missing_dir_is_empty(tmp_path):
    assert journal.list_log_files() == []


def test_list_log_files_newest_first_with_name_time_size(tmp_path):
    logs_dir = _logs_dir(tmp_path)
    logs_dir.mkdir(parents=True)

    import os

    old = logs_dir / "20190101-000000-u1-s1.jsonl"
    old.write_text("a" * 5, encoding="utf-8")
    new = logs_dir / "20250101-000000-u2-s2.jsonl"
    new.write_text("b" * 9, encoding="utf-8")
    bot_log = logs_dir / "bot.jsonl"
    bot_log.write_text("c", encoding="utf-8")

    # Явные времена изменения: порядок не должен зависеть от разрешения
    # часов файловой системы и порядка записи в тесте.
    now = time.time()
    os.utime(old, (now - 2000, now - 2000))
    os.utime(bot_log, (now - 1000, now - 1000))
    os.utime(new, (now, now))

    files = journal.list_log_files()

    names = [f.name for f in files]
    assert names[0] == "20250101-000000-u2-s2.jsonl"  # свежий - первым
    assert "bot.jsonl" in names  # bot.jsonl тоже входит в список
    assert old.name == names[-1]  # старейший - последним

    newest = next(f for f in files if f.name == new.name)
    assert newest.size == 9
    assert newest.mtime > 0


def test_list_log_files_skips_non_journal_files(tmp_path):
    """В каталоге логов живут не только журналы: `deploy/restart.sh` кладёт
    туда `tg-bot.pid` и `restart.log`. В списке `/logs` им не место -
    отрендерить их как журнал всё равно нельзя."""
    logs_dir = _logs_dir(tmp_path)
    logs_dir.mkdir(parents=True)
    (logs_dir / "bot.jsonl").write_text("{}\n", encoding="utf-8")
    (logs_dir / "tg-bot.pid").write_text("12345", encoding="utf-8")
    (logs_dir / "restart.log").write_text("запуск", encoding="utf-8")

    names = [f.name for f in journal.list_log_files()]

    assert names == ["bot.jsonl"]


# --- 3. Сборка читаемого .md (task 3.1, 3.2, 3.3, 3.4) -------------------


def test_render_log_file_orders_events_and_shows_time_and_kind(tmp_path):
    logs_dir = _logs_dir(tmp_path)
    logs_dir.mkdir(parents=True)
    path = logs_dir / "session.jsonl"
    path.write_text(
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "kind": "message", "payload": {"text": "первый"}})
        + "\n"
        + json.dumps({"ts": "2026-01-01T00:00:05+00:00", "kind": "callback", "payload": {"data": "x"}})
        + "\n",
        encoding="utf-8",
    )

    rendered = logs_view.render_log_file(path, "ru")

    first_pos = rendered.index("2026-01-01T00:00:00+00:00")
    second_pos = rendered.index("2026-01-01T00:00:05+00:00")
    assert first_pos < second_pos  # порядок событий сохранён
    assert "message" in rendered
    assert "callback" in rendered


def test_render_log_file_wraps_payload_text_in_code_block(tmp_path):
    logs_dir = _logs_dir(tmp_path)
    logs_dir.mkdir(parents=True)
    path = logs_dir / "session.jsonl"
    # Текст промпта содержит markdown-разметку, которая не должна сломать вывод.
    tricky_prompt = "# заголовок\n```\nсломай разметку\n* пункт"
    path.write_text(
        json.dumps(
            {
                "ts": "2026-01-01T00:00:00+00:00",
                "kind": "llm_request",
                "payload": {"prompt": tricky_prompt},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rendered = logs_view.render_log_file(path, "ru")

    # JSON экранирует переносы строк ("\n"), поэтому сравниваем с тем, как
    # промпт выглядит именно внутри JSON, а не с исходной многострочной строкой.
    encoded_prompt = json.dumps(tricky_prompt, ensure_ascii=False)[1:-1]
    assert "```" in rendered  # блок кода есть
    assert encoded_prompt in rendered  # текст промпта цел

    # Собственные ``` внутри промпта не должны преждевременно закрыть блок -
    # ограда длиннее любой цепочки обратных кавычек в содержимом.
    fence_match = re.search(r"(`{3,})json\n(.*?)\n\1", rendered, re.DOTALL)
    assert fence_match is not None
    assert encoded_prompt in fence_match.group(2)


def test_render_log_file_survives_truncated_last_line(tmp_path):
    # Задача 1.3/3.2: файл дописывается на ходу - последняя строка может быть
    # неполным JSON-фрагментом.
    logs_dir = _logs_dir(tmp_path)
    logs_dir.mkdir(parents=True)
    path = logs_dir / "session.jsonl"
    good = json.dumps({"ts": "2026-01-01T00:00:00+00:00", "kind": "message", "payload": {"text": "ok"}})
    broken = '{"ts": "2026-01-01T00:00:05+00:00", "kind": "message", "payload": {"text": "обрыв'
    path.write_text(good + "\n" + broken, encoding="utf-8")

    rendered = logs_view.render_log_file(path, "ru")  # не должно бросить исключение

    assert "ok" in rendered
    assert "00:00:05" not in rendered  # битая строка пропущена, а не показана частично


def test_render_log_file_handles_unknown_event_kind(tmp_path):
    logs_dir = _logs_dir(tmp_path)
    logs_dir.mkdir(parents=True)
    path = logs_dir / "session.jsonl"
    path.write_text(
        json.dumps(
            {
                "ts": "2026-01-01T00:00:00+00:00",
                "kind": "some_future_kind_nobody_wrote_yet",
                "payload": {"anything": [1, 2, {"x": "y"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rendered = logs_view.render_log_file(path, "ru")  # неизвестный вид не должен уронить сборку

    assert "some_future_kind_nobody_wrote_yet" in rendered
    assert "anything" in rendered


def test_split_for_telegram_keeps_whole_text_when_under_limit():
    text = "маленький файл, меньше лимита"

    parts = logs_view.split_for_telegram(text, "ru", limit_bytes=1024)

    assert parts == [text]


def test_split_for_telegram_splits_over_limit_and_labels_each_part():
    # Лимит меньше суммарного текста - должно получиться больше одной части,
    # и в каждой виден номер части (решение 7 design.md).
    text = "x" * 500
    parts = logs_view.split_for_telegram(text, "ru", limit_bytes=100)

    assert len(parts) > 1
    total_content = "".join(parts).count("x")
    assert total_content == 500  # содержимое не потеряно и не обрезано
    for i, part in enumerate(parts, start=1):
        assert f"{i}/{len(parts)}" in part  # номер части и их общее число видны
        assert len(part.encode("utf-8")) <= 100  # часть целиком укладывается в лимит


# --- log-diagnosis-outside-a-session ----------------------------------------
#
# Подстановка "последней сессии" не проверяла статус, поэтому завершённая
# сессия продолжала забирать себе события, случившиеся после её конца.
# Дежурный читал чужие события в журнале старой сессии, а в общем файле не
# находил их вовсе.


def _finish(conn, session):
    session["status"] = "done"
    db.save_session(conn, session)


async def test_an_event_after_a_finished_session_goes_to_the_common_file(
    tmp_path, conn, monkeypatch,
):
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(_logs_dir(tmp_path)))
    session = db.create_session(conn, 42, "_intro", {"fields": {}})
    _finish(conn, session)

    db.log_event(conn, 42, "later_event", {"identifier": "x-1"})

    names = {p.name for p in _logs_dir(tmp_path).iterdir()}
    assert "bot.jsonl" in names, "событие без сессии должно идти в общий файл"
    # Файла сессии может не быть вовсе - в неё ничего не писали; это тоже
    # верный исход, поэтому проверяем все файлы, кроме общего, а не один.
    for path in _logs_dir(tmp_path).iterdir():
        if path.name == "bot.jsonl":
            continue
        kinds = [line["kind"] for line in _read_jsonl(path)]
        assert "later_event" not in kinds, (
            f"журнал завершённой сессии ({path.name}) забрал чужое событие"
        )


async def test_an_event_during_an_active_session_still_goes_to_its_file(
    tmp_path, conn, monkeypatch,
):
    """Страж против перестарания: пока сессия жива, подстановка нужна - шаг
    режима без явного session_id всё равно относится к идущему разговору."""
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(_logs_dir(tmp_path)))
    session = db.create_session(conn, 43, "_intro", {"fields": {}})

    db.log_event(conn, 43, "callback", {"data": "field:title"})

    session_file = next(p for p in _logs_dir(tmp_path).iterdir() if "-s" in p.name)
    assert session_file.name.endswith(f"-u43-s{session['id']}.jsonl")
    assert "callback" in [line["kind"] for line in _read_jsonl(session_file)]


async def test_an_explicit_session_reaches_its_file_even_when_finished(
    tmp_path, conn, monkeypatch,
):
    """Событие с явным session_id принадлежит своей сессии - в том числе
    после её завершения."""
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(_logs_dir(tmp_path)))
    session = db.create_session(conn, 44, "_intro", {"fields": {}})
    _finish(conn, session)

    db.log_event(conn, 44, "late_answer", {"session_id": session["id"]})

    session_file = next(p for p in _logs_dir(tmp_path).iterdir() if "-s" in p.name)
    assert "late_answer" in [line["kind"] for line in _read_jsonl(session_file)]


# --- full-traffic-logging ---------------------------------------------------


async def test_secrets_are_masked_in_every_file_event(tmp_path, conn, monkeypatch):
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(_logs_dir(tmp_path)))
    token = "123456789:" + "A" * 35

    db.log_event(conn, None, "startup", {"note": f"url /bot{token}/x key sk-{'b' * 20}"})

    line = (_logs_dir(tmp_path) / "bot.jsonl").read_text(encoding="utf-8")
    assert token not in line and "sk-bbbb" not in line
    assert "***" in line
    assert json.loads(line)["payload"]["note"].startswith("url /bot***")


async def test_log_file_event_writes_file_only(tmp_path, conn, monkeypatch):
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(_logs_dir(tmp_path)))
    session = db.create_session(conn, 51, "_intro", {"fields": {}})

    db.log_file_event(conn, 51, "tg_update", {"text": "привет"})

    session_file = next(p for p in _logs_dir(tmp_path).iterdir() if "-s" in p.name)
    assert session_file.name.endswith(f"-u51-s{session['id']}.jsonl")
    assert _read_jsonl(session_file)[-1]["payload"] == {"text": "привет"}
    assert log_rows(conn, "tg_update") == []


async def test_bind_update_sets_current_user():
    from tgbot import logging_setup

    update = SimpleNamespace(effective_user=SimpleNamespace(id=77), update_id=1)
    await logging_setup.bind_update(update, None)
    assert logging_setup.current_user.get() == 77


async def test_secrets_are_masked_in_sqlite_too(tmp_path, conn, monkeypatch):
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(_logs_dir(tmp_path)))
    db.log_event(conn, None, "startup", {"note": f"key sk-{'c' * 20}"})
    [row] = log_rows(conn, "startup")
    assert "sk-cccc" not in row["note"] and "***" in row["note"]


async def test_file_only_fields_skip_sqlite(tmp_path, conn, monkeypatch):
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(_logs_dir(tmp_path)))
    db.log_event(conn, None, "startup", {"a": 1}, file_only={"big": "x" * 100})
    assert log_rows(conn, "startup")[0].get("big") is None
    line = _read_jsonl(_logs_dir(tmp_path) / "bot.jsonl")[-1]
    assert line["payload"] == {"a": 1, "big": "x" * 100}


async def test_raw_traffic_triggers_rotation_without_new_sessions(tmp_path, conn, monkeypatch):
    monkeypatch.setenv("TGBOT_LOGS_DIR", str(_logs_dir(tmp_path)))
    calls = []
    monkeypatch.setattr(db, "_ROTATE_EVERY", 2)
    monkeypatch.setattr(db, "_rotate_logs", lambda c: calls.append(c))
    monkeypatch.setattr(db, "_file_events", 0)
    for _ in range(4):
        db.log_file_event(conn, None, "tg_update", {})
    assert len(calls) == 2
