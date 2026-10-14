"""Файловый журнал: JSONL-зеркало событий `log` по файлам сессий и общий
`bot.jsonl`, их перечисление для `/logs` и ротация по размеру каталога.

Источник правды - SQLite (`tgbot.db`); здесь вторичный вывод, и любая его
ошибка гасится предупреждением. Модуль не импортирует `db`: сессии он читает
прямым запросом, а какие сессии ещё идут, передаёт `db` (`rotate`)."""

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, NamedTuple

from .logging_setup import redact

_LOG = logging.getLogger(__name__)

_DEFAULT_LOGS_DIR = "./logs"
_DEFAULT_LOGS_MAX_BYTES = 1024 * 1024 * 1024
BOT_LOG_NAME = "bot.jsonl"
_SESSION_FILE_RE = re.compile(r"-s(\d+)\.jsonl$")


def logs_dir() -> Path:
    """Каталог журнала - единственный источник истины о пути (используется и
    ротацией, и зеркалированием событий, и перечислением файлов для `/logs`)."""
    return Path(os.environ.get("TGBOT_LOGS_DIR") or _DEFAULT_LOGS_DIR)


class LogFile(NamedTuple):
    """Один файл журнала: имя, время последнего изменения (эпоха, `st_mtime`)
    и размер в байтах."""

    name: str
    mtime: float
    size: int


def list_log_files() -> list[LogFile]:
    """Файлы журнала, свежие первыми. Отсутствующий каталог (ещё не было ни
    одной записи) - пустой список, а не исключение.

    Только `*.jsonl`: в том же каталоге лежат `tg-bot.pid` и
    `restart.log` от `deploy/restart.sh`, а журналом они не являются и
    отрендерить их как журнал нельзя."""
    directory = logs_dir()
    if not directory.is_dir():
        return []
    entries = []
    for p in directory.glob("*.jsonl"):
        if not p.is_file():
            continue
        stat = p.stat()
        entries.append(LogFile(p.name, stat.st_mtime, stat.st_size))
    entries.sort(key=lambda f: f.mtime, reverse=True)
    return entries


def _logs_max_bytes() -> int:
    raw = os.environ.get("TGBOT_LOGS_MAX_BYTES")
    return int(raw) if raw and raw.strip() else _DEFAULT_LOGS_MAX_BYTES


# Имя файла-зеркала по сессии и последняя сессия пользователя. Без них каждое
# событие журнала стоило двух лишних запросов к БД, хотя сессия за это время не
# меняется. Кэш живёт при соединении - `db.connect` его сбрасывает (`forget`).
_log_files: dict[int, str] = {}
_last_sessions: dict[int, int] = {}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def forget() -> None:
    """Новое соединение - кэш файлов и последних сессий с нуля."""
    _log_files.clear()
    _last_sessions.clear()


def release(user_id: int) -> None:
    """Сессия пользователя ушла из активных: события без явной сессии больше
    не её (`db.save_session`)."""
    _last_sessions.pop(user_id, None)


def _log_filename(session_id: int, user_id: int, created_at: str) -> str:
    started = datetime.fromisoformat(created_at)
    return f"{started.strftime('%Y%m%d-%H%M%S')}-u{user_id}-s{session_id}.jsonl"


def session_filename(session: dict[str, Any]) -> str:
    return _log_filename(session["id"], session["user_id"], session["created_at"])


def remember_target(session_id: int, user_id: int, created_at: str) -> None:
    """Запоминает, в какой файл пишутся события этой сессии."""
    _log_files[session_id] = _log_filename(session_id, user_id, created_at)
    _last_sessions[user_id] = session_id


def _log_file_for(conn: sqlite3.Connection, session_id: int) -> str | None:
    """Имя файла сессии: из кэша, а при промахе (перезапуск процесса) - разово
    из БД."""
    cached = _log_files.get(session_id)
    if cached is not None:
        return cached
    session = conn.execute(
        "SELECT user_id, created_at FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if session is None:
        return None
    remember_target(session_id, session["user_id"], session["created_at"])
    return _log_files[session_id]


def mirror(
    conn: sqlite3.Connection,
    user_id: int | None,
    kind: str,
    payload: dict[str, Any],
    session_id: int | None,
) -> None:
    """Дописывает событие JSONL-строкой в файл сессии или `bot.jsonl`.

    Вторичный (зеркальный) вывод: SQLite-журнал выше уже записан и остаётся
    источником правды. Любая ошибка тут (нет места, нет прав, битая сессия)
    гасится одним warning - диалог не должен зависеть от файловых логов.
    """
    try:
        effective_id = session_id if session_id is not None else payload.get("session_id")
        if effective_id is None and user_id is not None:
            # Только АКТИВНАЯ сессия, и только из кэша. Раньше бралась
            # последняя любая, и завершённая сессия забирала себе всё, что
            # случалось после её конца: команды, заявки - дежурный читал
            # чужие события в журнале старой сессии, а в общем файле не
            # находил их вовсе.
            # Запрос к БД тут запрещён (`test_event_does_not_query_db_to_pick_
            # its_file`): кэш ведёт `remember_target` при старте сессии и
            # `release` (из `db.save_session`) при уходе из активных статусов.
            effective_id = _last_sessions.get(user_id)
        filename = (
            _log_file_for(conn, effective_id) if effective_id is not None else None
        ) or BOT_LOG_NAME
        path = logs_dir() / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": _now(), "user_id": user_id, "kind": kind, "payload": payload}
        with path.open("a", encoding="utf-8") as f:
            # Маска по готовой строке: секрет мог прийти в любом поле payload.
            f.write(redact(json.dumps(record, ensure_ascii=False)) + "\n")
    except Exception as exc:  # noqa: BLE001 - файловый лог вторичен, не роняем бота
        _LOG.warning("file-log mirror failed (kind=%s): %s", kind, exc)


def rotate(conn: sqlite3.Connection, open_statuses: tuple[str, ...]) -> None:
    """Держит суммарный размер каталога логов под лимитом.

    Вызывается при старте новой сессии (не на каждой записи - лишний stat
    каталога в горячем пути ради события, которое случается раз в недели).
    Удаляются старейшие по mtime файлы, кроме `bot.jsonl` и файлов активных
    сессий; если лимит всё ещё превышен - `bot.jsonl` усекается до нуля.
    """
    try:
        directory = logs_dir()
        if not directory.is_dir():
            return
        limit = _logs_max_bytes()
        files = [p for p in directory.iterdir() if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        if total <= limit:
            return

        placeholders = ",".join("?" for _ in open_statuses)
        active_ids = {
            row["id"]
            for row in conn.execute(
                f"SELECT id FROM sessions WHERE status IN ({placeholders})", open_statuses
            )
        }

        def protected(p: Path) -> bool:
            if p.name == BOT_LOG_NAME:
                return True
            m = _SESSION_FILE_RE.search(p.name)
            return bool(m) and int(m.group(1)) in active_ids

        deletable = sorted((p for p in files if not protected(p)), key=lambda p: p.stat().st_mtime)
        for p in deletable:
            if total <= limit:
                break
            total -= p.stat().st_size
            p.unlink(missing_ok=True)

        if total > limit:
            bot_log = directory / BOT_LOG_NAME
            if bot_log.is_file():
                total -= bot_log.stat().st_size
                bot_log.open("w").close()
    except Exception as exc:  # noqa: BLE001 - ротация вторична, не роняем бота
        _LOG.warning("log rotation failed: %s", exc)
