"""Клон `REPO_URL` (`config.repo_path`): bootstrap-клонирование и обновление
перед ответом Справки. Без `REPO_URL` клона нет и делать нечего.

Любая ошибка git не поднимается наружу - ответ идёт по текущей версии
клона, неудача пишется в журнал (kind="repo_pull_failed") и в лог
процесса. Уровень зависит от последствий: нет клона - WARNING (ответов
по коду не будет), клон есть - INFO (работаем на текущей версии).
После неудачи операция уходит в паузу на GIT_RETRY_S.
Успешное обновление/клонирование журналируется как "repo_pulled".
"""

import asyncio
import base64
import logging
import os
import subprocess
import time
from typing import NamedTuple
from urllib.parse import unquote, urlsplit, urlunsplit

from . import db, logging_setup
from .agent import tools as agent_tools

log = logging.getLogger(__name__)

CLONE_TIMEOUT_S = 60
PULL_TIMEOUT_S = 30
# rev-parse - локальная операция без сети, в отличие от clone/pull: не нужен
# отдельный ретрай/бэкофф, а таймаут - только защита от зависшего git.
REV_PARSE_TIMEOUT_S = 5

# Под systemd на запрос логина/пароля отвечать некому: без этих переменных git
# не падает, а висит до таймаута и держит ответ. Пусть лучше сразу ошибка.
NONINTERACTIVE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/true",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
}


# Учётные данные из URL (`https://<токен>@host/...`) git записал бы в
# `.git/config` клона, а клон читает агент. Поэтому git получает URL без них, а
# заголовок Authorization - через GIT_CONFIG_* в окружении: не в argv (ps) и не
# в `.git/config` (так же делает deploy/update.sh). Basic из тех же user:password,
# что git отправил бы сам: токен без пароля - "<токен>:".
HTTP_SCHEMES = ("http", "https")


def split_credentials(url: str) -> tuple[str, str | None]:
    """(URL без учётных данных, "user:password" или None)."""
    parts = urlsplit(url)
    if parts.scheme not in HTTP_SCHEMES or "@" not in parts.netloc:
        return url, None
    userinfo, _, host = parts.netloc.rpartition("@")
    user, _, password = userinfo.partition(":")
    return urlunsplit(parts._replace(netloc=host)), f"{unquote(user)}:{unquote(password)}"


def url_secrets(url: str | None) -> tuple[str, ...]:
    """Что маскировать в логах: учётная часть URL как есть и раскодированная."""
    if not url:
        return ()
    parts = urlsplit(url)
    if parts.scheme not in HTTP_SCHEMES or "@" not in parts.netloc:
        return ()
    userinfo = parts.netloc.rpartition("@")[0]
    user, _, password = userinfo.partition(":")
    found = {userinfo, unquote(user), unquote(password)}
    return tuple(value for value in found if value)


def _auth_env(url: str) -> dict[str, str]:
    """Заголовок Authorization для хоста URL через GIT_CONFIG_*; без учётных
    данных в URL - пусто."""
    clean, credentials = split_credentials(url)
    if credentials is None:
        return {}
    origin = urlsplit(clean)
    basic = base64.b64encode(credentials.encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{origin.scheme}://{origin.netloc}/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


# Пауза после неудачи: без доступа к репозиторию каждый вопрос иначе ждёт
# заведомо обречённый git (403/таймаут), а результата это не приближает.
GIT_RETRY_S = 600.0
_last_failure: dict[str, float] = {}


def _backoff(op: str) -> bool:
    """True - недавно уже пробовали и не смогли, пропускаем эту операцию."""
    failed_at = _last_failure.get(op)
    return failed_at is not None and (time.monotonic() - failed_at) < GIT_RETRY_S


def ensure_repo(config, conn) -> bool:
    """Клонирует репозиторий, если каталога нет, иначе обновляет его (git pull).

    Синхронный вход - для старта процесса, где цикла событий ещё нет. Из
    хендлеров звать `ensure_repo_async`: там git держал бы всех пользователей.
    Возвращает признак "клон на месте": по нему бот пишет исход в лог при
    старте, что ответов по коду не будет.
    """
    plan = _plan(config)
    if plan is not None:
        _finish(conn, plan, _run_git(plan))
    return _clone_present(config)


async def ensure_repo_async(config, conn) -> bool:
    """То же, но `subprocess.run` уходит в рабочий поток.

    В потоке только сам git: журнал (`_finish`) пишется здесь, в потоке цикла
    событий, поэтому общее соединение SQLite остаётся однопоточным и WAL с
    busy_timeout не нужны (design, решения 3 и 4).
    """
    plan = _plan(config)
    if plan is not None:
        _finish(conn, plan, await asyncio.to_thread(_run_git, plan))
    return _clone_present(config)


def _clone_present(config) -> bool:
    repo_path = config.repo_path
    return repo_path is not None and repo_path.exists()


class GitPlan(NamedTuple):
    """Что нужно сделать с клоном. Именованные поля, а не кортеж: план
    распаковывается в двух местах, и порядок в них молча разъехался бы.

    `repo_path` - явно, отдельным полем, а не выведено из `cwd`/`args` при
    журналировании: у pull путь лежит в `cwd`, у clone - последним элементом
    `args`, и вывод одного из другого в месте использования был бы хрупким
    (молча разъедется при следующей правке `_plan`)."""

    op: str
    args: list[str]
    cwd: str | None
    timeout: int
    level: int
    repo_path: str
    # Сверх NONINTERACTIVE_ENV: заголовок авторизации (`_auth_env`).
    env: dict[str, str]


def _plan(config) -> GitPlan | None:
    """План работы с клоном либо None - делать нечего (путь не задан или
    операция в паузе после неудачи)."""
    repo_path = config.repo_path
    if repo_path is None:
        return None
    if repo_path.exists():
        if _backoff("pull"):
            return None
        # safe.directory: клон мог быть создан другим пользователем (руками из-под
        # root), и тогда git отказывается работать - "detected dubious ownership".
        # Каталог наш, read-only, чужого кода мы не исполняем - помечаем доверенным
        # прямо в вызове, не трогая глобальный конфиг пользователя сервиса.
        # Неудача тут не важна: клон на месте, агент работает на текущей версии,
        # поэтому и в лог она идёт спокойным INFO (задача: не шуметь зря).
        # `--ff-only` текущей ветки, а не `origin main`: ветка по умолчанию
        # бывает и `master`/`trunk`, а слияние в клоне только для чтения не нужно.
        return GitPlan(
            "pull",
            ["git", "-c", f"safe.directory={repo_path}", "pull", "--ff-only"],
            str(repo_path),
            PULL_TIMEOUT_S,
            logging.INFO,
            str(repo_path),
            _auth_env(config.repo_url),
        )
    if _backoff("clone"):
        return None
    clean_url, _ = split_credentials(config.repo_url)
    return GitPlan(
        "clone",
        # `--`: URL не примется за опцию git, даже если начнётся с "-".
        ["git", "clone", "--", clean_url, str(repo_path)],
        None,
        CLONE_TIMEOUT_S,
        logging.WARNING,
        str(repo_path),
        _auth_env(config.repo_url),
    )


def _run_git(plan: GitPlan) -> subprocess.CompletedProcess | Exception:
    """Только запуск git - ни журнала, ни `conn`: эта часть уходит в поток."""
    env = {**os.environ, **NONINTERACTIVE_ENV, **plan.env}
    try:
        return subprocess.run(
            plan.args,
            cwd=plan.cwd,
            capture_output=True,
            text=True,
            timeout=plan.timeout,
            env=env,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return exc


def _read_revision(repo_path: str) -> str | None:
    """Короткий sha HEAD клона - или None, если прочитать не удалось.

    Читается ПОСЛЕ git-операции (clone/pull), а не до: иначе в журнал ушла бы
    ревизия, по которой агент как раз НЕ работал (см. tasks.md, 2.3).
    Никогда не поднимает исключение и не подставляет выдуманное значение:
    отсутствие поля в журнале - честный сигнал "не знаем", а не "не менялась".
    """
    env = {**os.environ, **NONINTERACTIVE_ENV}
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=REV_PARSE_TIMEOUT_S,
            env=env,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    if not result.stdout:
        return None
    return result.stdout.strip()


def _with_revision(payload: dict, revision: str | None) -> dict:
    """Новый словарь с полем ревизии - или тот же payload, если её не прочли."""
    if revision is None:
        return payload
    return {**payload, "revision": revision}


def _finish(conn, plan: GitPlan, result) -> None:
    """Журналирует исход git; никакая ошибка не поднимается наружу.

    Неудача пишется и в лог процесса (`level`): в SQLite её никто не читает, а
    на проде отсутствие клона искали именно в journalctl. После неудачи
    операция уходит в паузу - повторять её на каждый вопрос бессмысленно.

    Ревизия клона попадает в запись при любом исходе (успех или неудача, если
    клон на месте): без неё "ответ был по свежему коду?" не имеет ответа
    постфактум (proposal.md).
    """
    op, level = plan.op, plan.level
    revision = _read_revision(plan.repo_path)
    # Текст ошибки git может повторить URL с токеном: в журнал (SQLite и
    # файловое зеркало) он идёт через ту же маску, что и лог процесса.
    if isinstance(result, Exception):
        error = logging_setup.redact(str(result))
        _failed(conn, op, _with_revision({"error": error}, revision), level)
        return
    if result.returncode != 0:
        stderr = logging_setup.redact(result.stderr or "")
        _failed(
            conn,
            op,
            _with_revision({"stderr": stderr, "returncode": result.returncode}, revision),
            level,
        )
        return
    _last_failure.pop(op, None)
    # Клон изменился - обзор дерева, который агент шлёт в каждый запрос, надо
    # пересчитать (иначе он застынет на состоянии первого запроса).
    agent_tools.reset_overview()
    db.log_event(conn, None, "repo_pulled", _with_revision({"op": op}, revision))


def _failed(conn, op: str, payload: dict, level: int) -> None:
    """Журналирует неудачу git и ставит операцию на паузу."""
    _last_failure[op] = time.monotonic()
    detail = payload.get("error") or payload.get("stderr", "").strip()
    log.log(level, "git %s не удался: %s", op, detail)
    db.log_event(conn, None, "repo_pull_failed", {"op": op, **payload})
