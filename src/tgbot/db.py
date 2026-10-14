import json
import logging
import sqlite3
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from . import journal
from .logging_setup import redact

_LOG = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id  INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT NOT NULL
);

-- Сессия - пребывание в режиме: вход открывает, выход закрывает. `mode` -
-- значение `modes.Mode`, `step` - этап режима (у `/echo` - число ответов).
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    status     TEXT NOT NULL,
    mode       TEXT NOT NULL,
    step       INTEGER NOT NULL DEFAULT 0,
    data       TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_users (
    user_id   INTEGER PRIMARY KEY,
    username  TEXT,
    last_seen TEXT NOT NULL,
    -- Язык пользователя (спека localization). NULL - выбор не сделан:
    -- тогда работает автоопределение по профилю Telegram.
    lang      TEXT
);

CREATE TABLE IF NOT EXISTS log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    user_id INTEGER,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    user_id    INTEGER NOT NULL,
    -- NULL: у пользователя не было ни одной сессии на момент отзыва.
    session_id INTEGER,
    text       TEXT NOT NULL,
    -- Прочитанность глобальная (не per-admin): read_at IS NULL = непрочитан,
    -- read_by - админ, закрывший отзыв.
    read_by    INTEGER,
    read_at    TEXT
);

CREATE TABLE IF NOT EXISTS access_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    -- draft: ждём ФИО/причину; pending: у админов; approved/denied: решено.
    status      TEXT NOT NULL,
    name_reason TEXT,
    created_at  TEXT NOT NULL,
    decided_by  INTEGER,
    decided_at  TEXT,
    -- Текст карточки, ушедшей админам: профиль Telegram берётся из апдейта
    -- заявителя и после решения недоступен - без снимка пуш не отредактировать.
    card        TEXT,
    -- JSON [{"chat_id":..,"message_id":..}] пушей админам: после решения
    -- эти сообщения редактируются (убрать кнопки, дописать вердикт).
    admin_messages TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS agent_cache (
    prompt_hash TEXT PRIMARY KEY,
    answer      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    -- Чей ответ показан из кэша: без этого попадание в журнале анонимно.
    provider    TEXT,
    model       TEXT
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    # Соединение одно на процесс и однопоточное: в рабочие потоки
    # (`asyncio.to_thread`) уходит только то, что БД не трогает - git и чтение
    # файлов агентом. Поэтому режим журнала оставлен по умолчанию.
    # Условие пересмотра одно и жёсткое: как только `conn` начнут трогать из
    # потока или отдельного воркера, в том же коммите нужны WAL и busy_timeout
    # (design enable-concurrent-sessions, решения 3 и 4).
    path = Path(path)
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)
    journal.forget()
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Схема с нуля, миграций нет: старых баз у шаблона нет (PLAN, решение 9).
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def log_event(
    conn: sqlite3.Connection,
    user_id: int | None,
    kind: str,
    payload: dict[str, Any] | None = None,
    session_id: int | None = None,
    *,
    file_only: dict[str, Any] | None = None,
) -> int:
    """Пишет событие и возвращает id строки - на него ссылаются кнопки, которым
    не хватает 64 байт callback_data на сами данные.

    `file_only` - тяжёлые поля (полный обмен с LLM): только в файл,
    у таблицы `log` ротации нет (full-traffic-logging). Секреты маскируются
    и в SQLite, и в файле."""
    payload = payload or {}
    cursor = conn.execute(
        "INSERT INTO log (ts, user_id, kind, payload) VALUES (?, ?, ?, ?)",
        (now(), user_id, kind, redact(json.dumps(payload, ensure_ascii=False))),
    )
    conn.commit()
    journal.mirror(conn, user_id, kind, {**payload, **(file_only or {})}, session_id)
    return int(cursor.lastrowid)


# ponytail: счётчик в памяти процесса - точность "раз в N" не важна.
_ROTATE_EVERY = 1000
_file_events = 0


def log_file_event(
    conn: sqlite3.Connection,
    user_id: int | None,
    kind: str,
    payload: dict[str, Any],
    session_id: int | None = None,
) -> None:
    """Событие только в файловый журнал, без SQLite: сырой трафик Telegram и
    внешних сервисов (full-traffic-logging). У таблицы `log` нет ротации, а
    агрегации (`/cost`) этот трафик не читают.

    Ротация обычно идёт на старте сессии, но сырой трафик (группы, чужие)
    копится в `bot.jsonl` и без сессий - поэтому раз в `_ROTATE_EVERY`
    событий проверяем размер каталога и здесь."""
    global _file_events
    journal.mirror(conn, user_id, kind, payload, session_id)
    _file_events += 1
    if _file_events % _ROTATE_EVERY == 0:
        _rotate_logs(conn)


def last_event_ts(conn: sqlite3.Connection, user_id: int, kind: str) -> str | None:
    """Время последнего события `kind` пользователя (ISO, UTC) или None."""
    row = conn.execute(
        "SELECT max(ts) AS ts FROM log WHERE user_id = ? AND kind = ?", (user_id, kind)
    ).fetchone()
    return row["ts"]


def log_payload(
    conn: sqlite3.Connection, entry_id: int, *, kind: str, user_id: int
) -> dict[str, Any] | None:
    """Payload события по id - только своего вида и своего пользователя.

    id приходит из callback_data, то есть от клиента: без сверки вида и автора
    подделанный id записал бы оценку чужого разбора или по чужому событию.
    """
    row = conn.execute(
        "SELECT payload FROM log WHERE id = ? AND kind = ? AND user_id = ?",
        (entry_id, kind, user_id),
    ).fetchone()
    return json.loads(row["payload"]) if row is not None else None


def failure_reported(conn: sqlite3.Connection, user_id: int, log_id: int) -> bool:
    """Отзыв по этому коду инцидента уже создан кнопкой под сообщением о сбое."""
    row = conn.execute(
        "SELECT 1 FROM log WHERE kind = 'failure_reported' AND user_id = ?"
        " AND json_extract(payload, '$.log_id') = ? LIMIT 1",
        (user_id, log_id),
    ).fetchone()
    return row is not None




def create_session(
    conn: sqlite3.Connection,
    user_id: int,
    mode: str,
    data: dict[str, Any] | None = None,
    status: str = "active",
) -> dict[str, Any]:
    """Новая сессия и её файл журнала.

    `status` - потому что сессии бывают разные: у Справки она своя
    (`REFERENCE_STATUS`), и отличается именно статусом - отдельная таблица
    дала бы вторую нумерацию, а имя файла журнала строится по id сессии.
    """
    _rotate_logs(conn)
    ts = now()
    cur = conn.execute(
        "INSERT INTO sessions (user_id, status, mode, data, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, status, mode, json.dumps(data or {}, ensure_ascii=False), ts, ts),
    )
    conn.commit()
    journal.remember_target(cur.lastrowid, user_id, ts)
    return {
        "id": cur.lastrowid,
        "user_id": user_id,
        "status": status,
        "mode": mode,
        "step": 0,
        "data": data or {},
    }


# Сессия режима (кроме Справки): открыта, пока человек в режиме.
ACTIVE_STATUSES = ("active",)
DONE_STATUS = "done"


def close_mode_session(conn: sqlite3.Connection, user_id: int) -> None:
    """Выход из режима: открытая сессия режима закрывается."""
    session = active_session(conn, user_id)
    if session is not None:
        save_session(conn, {**session, "status": DONE_STATUS})


def open_mode_session(conn: sqlite3.Connection, user_id: int, mode: str) -> dict[str, Any]:
    """Вход в режим: прежняя сессия режима закрывается, открывается новая."""
    close_mode_session(conn, user_id)
    return create_session(conn, user_id, mode)

# Сессии, которые ещё идут: их файл журнала защищён от ротации, и события без
# явной сессии зеркалятся в него (`journal.mirror`).
# Сессия Справки - только ради журнала: без неё вопросы и ответы тонули в
# общем `bot.jsonl` и не попадали в `/logs`.
REFERENCE_STATUS = "reference"
REFERENCE_DONE_STATUS = "reference_done"

OPEN_STATUSES = ACTIVE_STATUSES + (REFERENCE_STATUS,)


def _rotate_logs(conn: sqlite3.Connection) -> None:
    """Ротация файлового журнала (`journal.rotate`): файлы идущих сессий
    (`OPEN_STATUSES`) не удаляются."""
    journal.rotate(conn, OPEN_STATUSES)


def reference_log_session(conn: sqlite3.Connection, user_id: int, *, new: bool) -> int:
    """Сессия Справки под журнал вопроса; делает её целью событий без явной
    сессии. `new` - начался новый диалог (контекст Справки истёк): прошлую
    сессию закрываем и открываем свою, иначе продолжаем последнюю открытую."""
    if not new:
        row = conn.execute(
            "SELECT id, created_at FROM sessions WHERE user_id = ? AND status = ?"
            " ORDER BY id DESC LIMIT 1",
            (user_id, REFERENCE_STATUS),
        ).fetchone()
        if row is not None:
            journal.remember_target(row["id"], user_id, row["created_at"])
            return row["id"]
    conn.execute(
        "UPDATE sessions SET status = ?, updated_at = ? WHERE user_id = ? AND status = ?",
        (REFERENCE_DONE_STATUS, now(), user_id, REFERENCE_STATUS),
    )
    return create_session(conn, user_id, "reference", status=REFERENCE_STATUS)["id"]


def _row_to_session(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "status": row["status"],
        "mode": row["mode"],
        "step": row["step"],
        "data": json.loads(row["data"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _last_session_with_status(
    conn: sqlite3.Connection, user_id: int, statuses: tuple[str, ...]
) -> dict[str, Any] | None:
    placeholders = ",".join("?" for _ in statuses)
    row = conn.execute(
        "SELECT id, user_id, status, mode, step, data, created_at, updated_at FROM sessions"
        f" WHERE user_id = ? AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        (user_id, *statuses),
    ).fetchone()
    return None if row is None else _row_to_session(row)


def active_session(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    """Незавершённая сессия режима пользователя или None."""
    return _last_session_with_status(conn, user_id, ACTIVE_STATUSES)


def get_session(conn: sqlite3.Connection, session_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, user_id, status, mode, step, data, created_at, updated_at FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    return None if row is None else _row_to_session(row)


def last_session_id(conn: sqlite3.Connection, user_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM sessions WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return None if row is None else row["id"]


# Непроверенный ответ - то же правило, что у пометки под ответом
# (`agent.result.is_unverified`): полный вызов (`tools_offered`), в котором не
# вызван ни один инструмент чтения, включая поиск grep/glob, или инструменты
# сняты. Запись без `tools_used` решается по прочитанным файлам, как и у
# пометки. Событие без признака режима считается лёгким, а не непроверенным.
_UNVERIFIED_SQL = (
    "kind = 'agent_files_read'"
    " AND json_extract(payload, '$.tools_offered') = 1"
    " AND (coalesce(json_extract(payload, '$.tools_stripped'), 0) = 1"
    " OR json_array_length(coalesce(json_extract(payload, '$.tools_used'),"
    " json_extract(payload, '$.files'))) = 0)"
)

# Токены разных провайдеров (input_tokens / prompt_tokens) складываем в одни
# счётчики. `unpriced` - вызовы, по которым провайдер цену не вернул: без этого
# счётчика ноль в долларах читался бы как "вызовы были бесплатными".
_COST_SQL = f"""
SELECT
  sum(kind IN ('agent_files_read', 'agent_call'))              AS calls,
  sum(kind = 'agent_cache_hit')                                AS cache_hits,
  sum(kind = 'agent_failed')                                   AS failed,
  sum(coalesce(json_extract(payload, '$.cost_usd'), 0))        AS usd,
  sum(coalesce(json_extract(payload, '$.usage.input_tokens'),
               json_extract(payload, '$.usage.prompt_tokens'), 0))     AS in_tok,
  sum(coalesce(json_extract(payload, '$.usage.output_tokens'),
               json_extract(payload, '$.usage.completion_tokens'), 0)) AS out_tok,
  sum(coalesce(json_extract(payload, '$.usage.cache_read_input_tokens'), 0))
                                                               AS cache_read,
  sum(coalesce(json_extract(payload, '$.usage.cache_creation_input_tokens'), 0))
                                                               AS cache_write,
  sum(kind = 'agent_call'
      AND json_extract(payload, '$.cost_usd') IS NULL)         AS unpriced,
  sum({_UNVERIFIED_SQL})                                       AS unverified
FROM log
WHERE user_id = ? AND ts BETWEEN ? AND ?
"""


def session_cost(conn: sqlite3.Connection, session_id: int) -> dict[str, Any] | None:
    """Расход LLM по сессии; None - такой сессии нет (нули != нет сессии).

    В log нет session_id, поэтому события привязываются к сессии по user_id и
    окну created_at..updated_at.
    # ponytail: окно вместо колонки session_id - у пользователя одна активная
    # сессия за раз (active_session); заводить колонку, если станет иначе.
    """
    session = conn.execute(
        "SELECT id, user_id, status, created_at, updated_at FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if session is None:
        return None
    totals = conn.execute(
        _COST_SQL, (session["user_id"], session["created_at"], session["updated_at"])
    ).fetchone()
    return _rows_to_cost(session, totals)


_COST_COUNTERS = (
    "calls", "cache_hits", "failed", "in_tok", "out_tok", "cache_read", "cache_write",
    "unpriced", "unverified",
)


def _rows_to_cost(session: sqlite3.Row, totals: sqlite3.Row) -> dict[str, Any]:
    """Пустое окно даёт NULL в каждой сумме - поэтому все счётчики через `or 0`."""
    cost: dict[str, Any] = {
        "session_id": session["id"],
        "user_id": session["user_id"],
        "status": session["status"],
        "usd": float(totals["usd"] or 0.0),
    }
    for key in _COST_COUNTERS:
        cost[key] = int(totals[key] or 0)
    return cost


def save_session(conn: sqlite3.Connection, session: dict[str, Any]) -> None:
    """Пишет снимок сессии в БД (вызывается после каждого изменения).

    Здесь же кончается право сессии забирать себе события без явного
    `session_id`: это единственное место, где меняется статус, поэтому уход из
    активных статусов снимает её с поста (`journal.release`). Проверять статус
    в `journal.mirror` нельзя - там запрещён запрос к БД на каждое событие.
    """
    if session["status"] not in ACTIVE_STATUSES:
        journal.release(session["user_id"])
    conn.execute(
        "UPDATE sessions SET status = ?, step = ?, data = ?, updated_at = ?"
        " WHERE id = ?",
        (
            session["status"],
            session["step"],
            json.dumps(session["data"], ensure_ascii=False),
            now(),
            session["id"],
        ),
    )
    conn.commit()


def advance_session(conn: sqlite3.Connection, session: dict[str, Any]) -> bool:
    """Шаг активной сессии: пишет `step`/`data`, только пока она ещё активна.

    Снимок сессии у шага мог устареть: между чтением и записью выход из
    режима её закрыл. Обычный `save_session` вернул бы ей статус "active";
    здесь условие в самом UPDATE, и False значит "сессия уже закрыта"."""
    placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
    cursor = conn.execute(
        "UPDATE sessions SET step = ?, data = ?, updated_at = ?"
        f" WHERE id = ? AND status IN ({placeholders})",
        (
            session["step"],
            json.dumps(session["data"], ensure_ascii=False),
            now(),
            session["id"],
            *ACTIVE_STATUSES,
        ),
    )
    conn.commit()
    return cursor.rowcount > 0


def remember_user(
    conn: sqlite3.Connection,
    user_id: int,
    username: str | None,
    lang: str | None = None,
) -> None:
    """Запоминает написавшего боту: даёт /allow и /deny работать по @username.

    `lang` - язык, определённый по профилю Telegram: записывается только если
    у пользователя ещё нет своего значения. Явный выбор через /language
    автоопределение не переписывает (спека localization).
    """
    conn.execute(
        "INSERT INTO seen_users (user_id, username, last_seen, lang)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET username = excluded.username,"
        " last_seen = excluded.last_seen,"
        " lang = coalesce(seen_users.lang, excluded.lang)",
        (user_id, username, now(), lang),
    )
    conn.commit()


def get_lang(conn: sqlite3.Connection, user_id: int) -> str | None:
    """Язык пользователя или None, если он ещё не определялся."""
    row = conn.execute(
        "SELECT lang FROM seen_users WHERE user_id = ?", (user_id,)
    ).fetchone()
    return None if row is None else row["lang"]


def set_lang(conn: sqlite3.Connection, user_id: int, lang: str) -> None:
    """Явный выбор языка пользователем: строки в `seen_users` может ещё не
    быть - /language доступна и до попадания в allowlist."""
    conn.execute(
        "INSERT INTO seen_users (user_id, username, last_seen, lang)"
        " VALUES (?, NULL, ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang",
        (user_id, now(), lang),
    )
    conn.commit()


def find_seen_by_username(conn: sqlite3.Connection, username: str) -> int | None:
    """user_id по @username среди писавших боту (регистр не важен) или None."""
    row = conn.execute(
        "SELECT user_id FROM seen_users WHERE lower(username) = lower(?)"
        " ORDER BY last_seen DESC LIMIT 1",
        (username.lstrip("@"),),
    ).fetchone()
    return None if row is None else row["user_id"]


def seen_username(conn: sqlite3.Connection, user_id: int) -> str | None:
    row = conn.execute(
        "SELECT username FROM seen_users WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row["username"] if row else None


def is_allowed(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return row is not None


def add_user(conn: sqlite3.Connection, user_id: int, added_by: int) -> bool:
    """True - пользователь добавлен, False - уже был в allowlist."""
    if is_allowed(conn, user_id):
        return False
    conn.execute(
        "INSERT INTO users (user_id, added_by, added_at) VALUES (?, ?, ?)",
        (user_id, added_by, now()),
    )
    conn.commit()
    return True


def remove_user(conn: sqlite3.Connection, user_id: int) -> bool:
    """True - пользователь удалён, False - его не было в allowlist."""
    cur = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    conn.commit()
    return cur.rowcount > 0


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT user_id, added_by, added_at FROM users ORDER BY added_at"
    ).fetchall()


def add_feedback(
    conn: sqlite3.Connection, user_id: int, session_id: int | None, text: str
) -> int:
    """Сохраняет отзыв (спека user-feedback); возвращает id отзыва.

    session_id фиксируется в момент отправки: к моменту чтения у пользователя
    может идти уже другая сессия (design, решение 3)."""
    cur = conn.execute(
        "INSERT INTO feedback (ts, user_id, session_id, text) VALUES (?, ?, ?, ?)",
        (now(), user_id, session_id, text),
    )
    conn.commit()
    return cur.lastrowid or 0


def _feedback_rows(conn: sqlite3.Connection, where: str, params: tuple = ()) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, ts, user_id, session_id, text, read_by, read_at FROM feedback"
        f" {where}",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def unread_feedback(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Непрочитанные отзывы, старые первыми - читать в порядке поступления."""
    return _feedback_rows(conn, "WHERE read_at IS NULL ORDER BY id")


def recent_feedback(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    """Последние отзывы (свежие первыми) для /feedback all."""
    return _feedback_rows(conn, "ORDER BY id DESC LIMIT ?", (limit,))


def unread_feedback_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM feedback WHERE read_at IS NULL"
    ).fetchone()["n"]


def get_feedback(conn: sqlite3.Connection, feedback_id: int) -> dict[str, Any] | None:
    rows = _feedback_rows(conn, "WHERE id = ?", (feedback_id,))
    return rows[0] if rows else None


def mark_feedback_read(conn: sqlite3.Connection, feedback_id: int, admin_id: int) -> bool:
    """True - отзыв только что помечен прочитанным этим админом, False - уже
    был прочитан (или не существует). Условие `read_at IS NULL` делает пометку
    атомарной: гонка двух админов оставляет ровно одного автора."""
    cur = conn.execute(
        "UPDATE feedback SET read_by = ?, read_at = ? WHERE id = ? AND read_at IS NULL",
        (admin_id, now(), feedback_id),
    )
    conn.commit()
    return cur.rowcount > 0


# --- заявки на доступ (спека access-request) --------------------------------


def _access_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["admin_messages"] = json.loads(item["admin_messages"])
    return item


def open_access_request(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    """Незакрытая заявка пользователя (draft или pending) или None.

    Их не бывает больше одной: новая создаётся только когда открытых нет."""
    row = conn.execute(
        "SELECT * FROM access_requests WHERE user_id = ?"
        " AND status IN ('draft', 'pending') ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return _access_row(row)


def get_access_request(conn: sqlite3.Connection, request_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM access_requests WHERE id = ?", (request_id,)
    ).fetchone()
    return _access_row(row)


def create_access_draft(conn: sqlite3.Connection, user_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO access_requests (user_id, status, created_at) VALUES (?, 'draft', ?)",
        (user_id, now()),
    )
    conn.commit()
    return cur.lastrowid or 0


def submit_access_request(
    conn: sqlite3.Connection, request_id: int, name_reason: str | None
) -> None:
    """draft -> pending; текст заявителя фиксируется в момент подачи."""
    conn.execute(
        "UPDATE access_requests SET status = 'pending', name_reason = ?"
        " WHERE id = ? AND status = 'draft'",
        (name_reason, request_id),
    )
    conn.commit()


def set_access_messages(
    conn: sqlite3.Connection, request_id: int, card: str, messages: list[dict[str, int]]
) -> None:
    """Текст карточки и куда ушли пуши - чтобы отредактировать их после решения."""
    conn.execute(
        "UPDATE access_requests SET card = ?, admin_messages = ? WHERE id = ?",
        (card, json.dumps(messages), request_id),
    )
    conn.commit()


def decide_access_request(
    conn: sqlite3.Connection, request_id: int, admin_id: int, status: str
) -> bool:
    """True - решение зафиксировано этим админом, False - заявка уже решена
    (или не подана). Условие `status = 'pending'` делает гонку двух админов
    атомарной: побеждает ровно один."""
    cur = conn.execute(
        "UPDATE access_requests SET status = ?, decided_by = ?, decided_at = ?"
        " WHERE id = ? AND status = 'pending'",
        (status, admin_id, now(), request_id),
    )
    conn.commit()
    return cur.rowcount > 0


def access_history(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    """Контекст для карточки заявки: когда впервые написал, сколько раз
    получал отказ, чем закончились прошлые заявки."""
    first_seen = conn.execute(
        "SELECT min(ts) AS ts FROM log WHERE user_id = ?", (user_id,)
    ).fetchone()["ts"]
    denied_count = conn.execute(
        "SELECT count(*) AS n FROM log WHERE user_id = ? AND kind = 'access_denied'",
        (user_id,),
    ).fetchone()["n"]
    past = [
        r["status"]
        for r in conn.execute(
            "SELECT status FROM access_requests WHERE user_id = ?"
            " AND status IN ('approved', 'denied') ORDER BY id",
            (user_id,),
        )
    ]
    return {"first_seen": first_seen, "denied_count": denied_count, "past": past}


def session_log_filename(conn: sqlite3.Connection, session_id: int) -> str | None:
    """Имя файла журнала сессии или None (сессии нет). Для кнопки 'Лог сессии'
    у отзыва: имя не хранится в отзыве, а выводится из сессии - единственный
    источник истины о формате имени."""
    session = get_session(conn, session_id)
    return None if session is None else journal.session_filename(session)


def cache_get(
    conn: sqlite3.Connection, prompt_hash: str, ttl_s: float
) -> tuple[str, str | None, str | None] | None:
    """(ответ, провайдер, модель) или None (нет записи / протухла - удаляется
    лениво). Провайдер и модель нужны, чтобы попадание в кэш не было анонимным
    в журнале."""
    row = conn.execute(
        "SELECT answer, created_at, provider, model FROM agent_cache WHERE prompt_hash = ?",
        (prompt_hash,),
    ).fetchone()
    if row is None:
        return None
    age_s = (datetime.now(UTC) - datetime.fromisoformat(row["created_at"])).total_seconds()
    if age_s > ttl_s:
        conn.execute("DELETE FROM agent_cache WHERE prompt_hash = ?", (prompt_hash,))
        conn.commit()
        return None
    return row["answer"], row["provider"], row["model"]


def cache_put(
    conn: sqlite3.Connection,
    prompt_hash: str,
    answer: str,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO agent_cache (prompt_hash, answer, created_at, provider, model)"
        " VALUES (?, ?, ?, ?, ?)",
        (prompt_hash, answer, now(), provider, model),
    )
    conn.commit()


def clear_cache(conn: sqlite3.Connection) -> int:
    """Удаляет весь кэш ответов; возвращает число удалённых записей."""
    removed = conn.execute("SELECT count(*) AS n FROM agent_cache").fetchone()["n"]
    conn.execute("DELETE FROM agent_cache")
    conn.commit()
    return int(removed)
