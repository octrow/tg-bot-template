import asyncio
import base64
import os
import subprocess
import threading
import time

import pytest

from tgbot import db, logging_setup, repo
from tgbot.config import Config

from .conftest import log_rows


@pytest.fixture(autouse=True)
def _reset_git_backoff(monkeypatch):
    """Пауза после неудачи git живёт в модуле - между тестами её надо снимать,
    иначе одна упавшая операция глушит git у следующих тестов."""
    monkeypatch.setattr(repo, "_last_failure", {})


def make_config(tmp_path, repo_path=None) -> Config:
    """Клон живёт рядом с базой: `repo_path` - всегда `tmp_path / "repo"`."""
    config = Config(
        telegram_bot_token="t",
        admin_user_ids=frozenset(),
        db_path=tmp_path / "db.sqlite",
        repo_url="https://example.com/knowledge.git" if repo_path is not None else None,
    )
    assert config.repo_path == repo_path
    return config


class FakeCompleted:
    """Дубль `subprocess.CompletedProcess`.

    `stdout` есть всегда, как у настоящего результата: без него дубль
    заставлял бы код читать вывод через `getattr`, то есть подстраиваться под
    тест, а не под реальность.
    """

    def __init__(self, returncode: int, stderr: str = "", stdout: str = ""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


async def _neighbour(order: list, started: threading.Event, release: threading.Event) -> str:
    """Второй пользователь: должен получить своё, пока git ещё работает."""
    while not started.is_set():
        await asyncio.sleep(0.01)
    order.append("сосед")
    release.set()
    return "ответ соседу"


async def test_ensure_repo_async_does_not_hold_the_event_loop(tmp_path, monkeypatch):
    """Долгий git у одного пользователя не должен останавливать остальных:
    сосед обязан ответить раньше, чем вернётся `subprocess.run`."""
    conn = db.connect(tmp_path / "db.sqlite")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = make_config(tmp_path, repo_path)
    order: list[str] = []
    started, release = threading.Event(), threading.Event()

    def slow_git(args, **kwargs):
        if "rev-parse" in args:  # чтение ревизии после pull - не сам pull
            return FakeCompleted(0)
        started.set()
        release.wait(1.0)  # держим git, пока соседу не дали ответить
        order.append("git")
        return FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", slow_git)

    ok, neighbour = await asyncio.wait_for(
        asyncio.gather(
            repo.ensure_repo_async(config, conn), _neighbour(order, started, release)
        ),
        timeout=2,
    )

    assert ok is True
    assert neighbour == "ответ соседу"
    assert order == ["сосед", "git"]


async def test_failed_pull_does_not_reach_the_neighbour(tmp_path, monkeypatch):
    """Недоступный источник: Справка отвечает по текущему клону, а второй
    пользователь ни ошибки не видит, ни таймаута не ждёт."""
    conn = db.connect(tmp_path / "db.sqlite")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = make_config(tmp_path, repo_path)
    order: list[str] = []
    started, release = threading.Event(), threading.Event()

    def hanging_git(args, timeout=None, **kwargs):
        if "rev-parse" in args:  # чтение ревизии после неудачи - не сам pull
            return FakeCompleted(0)
        started.set()
        release.wait(1.0)
        order.append("git")
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", hanging_git)

    ok, neighbour = await asyncio.wait_for(
        asyncio.gather(
            repo.ensure_repo_async(config, conn), _neighbour(order, started, release)
        ),
        timeout=2,
    )

    assert ok is True  # клон на месте - Справка отвечает
    assert neighbour == "ответ соседу"
    assert order == ["сосед", "git"]
    assert log_rows(conn, "repo_pull_failed")[0]["op"] == "pull"


def test_ensure_repo_clones_when_missing(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    repo_path = tmp_path / "repo"
    config = make_config(tmp_path, repo_path)

    calls = []

    def fake_run(args, cwd=None, timeout=None, **kwargs):
        if "rev-parse" in args:  # чтение ревизии после clone - отдельный вызов
            return FakeCompleted(0)
        calls.append((args, cwd, timeout))
        return FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    repo.ensure_repo(config, conn)

    assert len(calls) == 1
    args, cwd, timeout = calls[0]
    assert args[:3] == ["git", "clone", "--"]
    assert args[3] == config.repo_url
    assert args[4] == str(repo_path)
    assert timeout == repo.CLONE_TIMEOUT_S
    assert log_rows(conn, "repo_pulled")[0]["op"] == "clone"


def test_ensure_repo_pulls_when_present(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = make_config(tmp_path, repo_path)

    calls = []

    def fake_run(args, cwd=None, timeout=None, **kwargs):
        if "rev-parse" in args:  # чтение ревизии после pull - отдельный вызов
            return FakeCompleted(0)
        calls.append((args, cwd, timeout))
        return FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    repo.ensure_repo(config, conn)

    assert len(calls) == 1
    args, cwd, timeout = calls[0]
    assert args[-2:] == ["pull", "--ff-only"]
    # Клон мог сделать другой пользователь - без safe.directory git откажется
    # ("dubious ownership") и /new останется без свежего кода.
    assert args[:3] == ["git", "-c", f"safe.directory={repo_path}"]
    assert cwd == str(repo_path)
    assert timeout == repo.PULL_TIMEOUT_S
    entries = log_rows(conn, "repo_pulled")
    assert entries[0]["op"] == "pull"


def test_ensure_repo_pull_failure_logs_warning_and_does_not_raise(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = make_config(tmp_path, repo_path)

    def fake_run(args, cwd=None, timeout=None, **kwargs):
        return FakeCompleted(1, stderr="fatal: unable to access: network unreachable")

    monkeypatch.setattr(subprocess, "run", fake_run)

    repo.ensure_repo(config, conn)  # не должно поднимать исключение

    entries = log_rows(conn, "repo_pull_failed")
    assert len(entries) == 1
    assert entries[0]["op"] == "pull"
    assert "network unreachable" in entries[0]["stderr"]
    assert log_rows(conn, "repo_pulled") == []


def test_ensure_repo_git_exception_logs_warning_and_does_not_raise(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = make_config(tmp_path, repo_path)

    def fake_run(args, cwd=None, timeout=None, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)

    repo.ensure_repo(config, conn)

    entries = log_rows(conn, "repo_pull_failed")
    assert len(entries) == 1
    assert entries[0]["op"] == "pull"
    assert "error" in entries[0]


def test_ensure_repo_returns_true_when_clone_present(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = make_config(tmp_path, repo_path)

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeCompleted(0))

    assert repo.ensure_repo(config, conn) is True


def test_ensure_repo_returns_false_and_warns_when_clone_failed(tmp_path, monkeypatch, caplog):
    conn = db.connect(tmp_path / "db.sqlite")
    repo_path = tmp_path / "repo"
    config = make_config(tmp_path, repo_path)

    def fake_run(args, cwd=None, timeout=None, **kwargs):
        return FakeCompleted(128, stderr="fatal: could not read Username")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with caplog.at_level("WARNING", logger="tgbot.repo"):
        assert repo.ensure_repo(config, conn) is False

    assert "could not read Username" in caplog.text
    assert log_rows(conn, "repo_pull_failed")[0]["op"] == "clone"


def test_git_runs_noninteractively(tmp_path, monkeypatch):
    """Под systemd отвечать на запрос учётных данных некому - git должен
    падать сразу, а не висеть до таймаута."""
    conn = db.connect(tmp_path / "db.sqlite")
    repo_path = tmp_path / "repo"
    config = make_config(tmp_path, repo_path)
    envs: list[dict] = []

    def fake_run(args, cwd=None, timeout=None, env=None, **kwargs):
        envs.append(env)
        return FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    repo.ensure_repo(config, conn)

    assert envs[0]["GIT_TERMINAL_PROMPT"] == "0"
    assert envs[0]["GIT_ASKPASS"] == "/bin/true"
    assert envs[0]["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    assert "PATH" in envs[0]  # окружение процесса не затёрто


def test_ensure_repo_noop_without_configured_path(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    config = make_config(tmp_path, repo_path=None)

    def fake_run(*args, **kwargs):
        raise AssertionError("git не должен вызываться без configured path")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert repo.ensure_repo(config, conn) is False

    assert log_rows(conn, "repo_pulled") == []
    assert log_rows(conn, "repo_pull_failed") == []


def test_failed_clone_is_not_retried_immediately(tmp_path, monkeypatch):
    """Без доступа к репозиторию каждый /new не должен ждать обречённый git."""
    conn = db.connect(tmp_path / "db.sqlite")
    config = make_config(tmp_path, repo_path=tmp_path / "repo")
    calls = []

    def failing_git(args, **kwargs):
        if "rev-parse" in args:  # чтение ревизии после неудачи - не сам clone
            return subprocess.CompletedProcess(args, 128, "", "")
        calls.append(args)
        return subprocess.CompletedProcess(args, 128, "", "403 Forbidden")

    monkeypatch.setattr(subprocess, "run", failing_git)

    assert repo.ensure_repo(config, conn) is False
    assert repo.ensure_repo(config, conn) is False

    assert len(calls) == 1  # второй раз git не звали - ждём паузы


def test_clone_retried_after_backoff_expires(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    config = make_config(tmp_path, repo_path=tmp_path / "repo")
    calls = []

    def failing_git(args, **kwargs):
        if "rev-parse" in args:  # чтение ревизии после неудачи - не сам clone
            return subprocess.CompletedProcess(args, 128, "", "")
        calls.append(args)
        return subprocess.CompletedProcess(args, 128, "", "403 Forbidden")

    monkeypatch.setattr(subprocess, "run", failing_git)
    repo.ensure_repo(config, conn)

    # Пауза истекла - пробуем снова.
    monkeypatch.setattr(repo, "_last_failure", {"clone": time.monotonic() - repo.GIT_RETRY_S - 1})
    repo.ensure_repo(config, conn)

    assert len(calls) == 2


# --- refresh-clone-for-diagnosis: ревизия клона попадает в журнал ----------
#
# Без неё "ответ был по свежему коду?" не имеет ответа постфактум
# (proposal.md). Ревизия читается ПОСЛЕ git-операции - настоящим `git
# rev-parse` на настоящем клоне, а не подменяется в моках, иначе тест не
# отличит "прочитали после" от "прочитали до" (мутация 2.3).


def _real_git(*args: str, cwd) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    )


def _real_clone_pair(tmp_path):
    """origin с одним коммитом + его настоящий клон (то, чем `_plan` считает
    репозиторий "уже на месте")."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _real_git("init", "-q", "-b", "main", cwd=origin)
    (origin / "f.txt").write_text("1")
    _real_git("add", "f.txt", cwd=origin)
    _real_git("commit", "-q", "-m", "init", cwd=origin)
    clone = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)],
        check=True, capture_output=True, text=True,
    )
    return origin, clone


def _current_revision(path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def test_successful_pull_logs_the_clone_revision(tmp_path):
    """RED (2.1): ревизия в записи должна быть ТА, что на диске ПОСЛЕ pull -
    не старая (до него) и не отсутствующая."""
    conn = db.connect(tmp_path / "db.sqlite")
    origin, clone = _real_clone_pair(tmp_path)
    config = make_config(tmp_path, clone)

    (origin / "f.txt").write_text("2")
    _real_git("add", "f.txt", cwd=origin)
    _real_git("commit", "-q", "-m", "second", cwd=origin)

    assert repo.ensure_repo(config, conn) is True
    expected = _current_revision(clone)

    entries = log_rows(conn, "repo_pulled")
    assert entries[-1]["revision"] == expected


def test_failed_pull_logs_the_current_revision(tmp_path, monkeypatch):
    """RED (2.1): pull провалился, но клон на месте - ревизия (прежняя,
    неизменная) обязана попасть в запись о неудаче."""
    conn = db.connect(tmp_path / "db.sqlite")
    _origin, clone = _real_clone_pair(tmp_path)
    config = make_config(tmp_path, clone)
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if "pull" in args:
            return FakeCompleted(1, stderr="fatal: unable to access")
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert repo.ensure_repo(config, conn) is True
    expected = _current_revision(clone)

    entries = log_rows(conn, "repo_pull_failed")
    assert entries[-1]["revision"] == expected


def test_when_the_revision_cannot_be_read_the_field_is_absent(tmp_path, monkeypatch):
    """RED (2.1): каталог есть, но это не git-репозиторий - поле не
    выдумывается, его просто нет."""
    conn = db.connect(tmp_path / "db.sqlite")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = make_config(tmp_path, repo_path)
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if "pull" in args:
            return FakeCompleted(0)
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert repo.ensure_repo(config, conn) is True

    entries = log_rows(conn, "repo_pulled")
    assert "revision" not in entries[-1]


def test_failed_pull_is_not_retried_immediately(tmp_path, monkeypatch, caplog):
    """Клон на месте - неудачный pull не должен ни повторяться на каждом вопросе,
    ни кричать WARNING: Справка отвечает по текущей версии кода."""
    conn = db.connect(tmp_path / "db.sqlite")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = make_config(tmp_path, repo_path)
    calls = []

    def failing_git(args, **kwargs):
        if "rev-parse" in args:  # чтение ревизии после неудачи - не сам pull
            return FakeCompleted(0)
        calls.append(args)
        return FakeCompleted(1, stderr="remote: Repository not found.")

    monkeypatch.setattr(subprocess, "run", failing_git)

    with caplog.at_level("WARNING", logger="tgbot.repo"):
        assert repo.ensure_repo(config, conn) is True
        assert repo.ensure_repo(config, conn) is True

    assert len(calls) == 1
    assert caplog.text == ""  # неудача пишется INFO, а не WARNING


TOKEN_URL = "https://ghp_secrettoken123@github.com/org/kb.git"


def _capture_runs(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_run(args, cwd=None, timeout=None, env=None, **kwargs):
        if "rev-parse" not in args:
            calls.append({"args": args, "env": env})
        return FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_clone_keeps_token_out_of_argv_and_git_config(tmp_path, monkeypatch):
    """Токен из URL не попадает ни в argv (ps), ни в `.git/config` клона: git
    получает URL без учётных данных и заголовок через GIT_CONFIG_* в env."""
    conn = db.connect(tmp_path / "db.sqlite")
    config = Config(
        telegram_bot_token="t", admin_user_ids=frozenset(),
        db_path=tmp_path / "db.sqlite", repo_url=TOKEN_URL,
    )
    calls = _capture_runs(monkeypatch)

    repo.ensure_repo(config, conn)

    args, env = calls[0]["args"], calls[0]["env"]
    assert args[:3] == ["git", "clone", "--"]
    assert args[3] == "https://github.com/org/kb.git"
    assert not any("ghp_secrettoken123" in arg for arg in args)
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
    expected = base64.b64encode(b"ghp_secrettoken123:").decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {expected}"
    assert env["GIT_CONFIG_COUNT"] == "1"


def test_pull_passes_credentials_through_env_too(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    (tmp_path / "repo").mkdir()
    config = Config(
        telegram_bot_token="t", admin_user_ids=frozenset(),
        db_path=tmp_path / "db.sqlite", repo_url="https://bot:pa%40ss@git.example.com/kb.git",
    )
    calls = _capture_runs(monkeypatch)

    repo.ensure_repo(config, conn)

    env = calls[0]["env"]
    assert env["GIT_CONFIG_KEY_0"] == "http.https://git.example.com/.extraHeader"
    expected = base64.b64encode(b"bot:pa@ss").decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {expected}"


def test_url_without_credentials_adds_no_header(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    config = make_config(tmp_path, tmp_path / "repo")
    calls = _capture_runs(monkeypatch)

    repo.ensure_repo(config, conn)

    assert "GIT_CONFIG_COUNT" not in calls[0]["env"]
    assert calls[0]["args"][3] == "https://example.com/knowledge.git"


def test_url_secrets_lists_credentials_only():
    assert set(repo.url_secrets(TOKEN_URL)) == {"ghp_secrettoken123"}
    assert set(repo.url_secrets("https://bot:pa%40ss@h/kb")) >= {"bot:pa%40ss", "pa@ss"}
    assert repo.url_secrets("https://example.com/kb.git") == ()
    assert repo.url_secrets("git@github.com:o/r.git") == ()
    assert repo.url_secrets(None) == ()


def test_real_clone_works_with_end_of_options(tmp_path):
    """Настоящий git принимает `clone -- URL путь`; URL в argv и есть тот, что
    git пишет в `.git/config`, поэтому токена там нет (тест выше)."""
    (tmp_path / "src").mkdir()
    origin, _ = _real_clone_pair(tmp_path / "src")
    conn = db.connect(tmp_path / "db.sqlite")
    config = Config(
        telegram_bot_token="t", admin_user_ids=frozenset(),
        db_path=tmp_path / "db.sqlite", repo_url=f"file://{origin}",
    )

    assert repo.ensure_repo(config, conn) is True
    assert (tmp_path / "repo" / "f.txt").exists()


def test_pull_is_fast_forward_of_current_branch(tmp_path, monkeypatch):
    """Не `origin main`: ветка по умолчанию бывает `master`/`trunk`."""
    conn = db.connect(tmp_path / "db.sqlite")
    (tmp_path / "repo").mkdir()
    calls = _capture_runs(monkeypatch)

    repo.ensure_repo(make_config(tmp_path, tmp_path / "repo"), conn)

    assert calls[0]["args"][-2:] == ["pull", "--ff-only"]


def test_real_pull_on_non_main_branch(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _real_git("init", "-q", "-b", "trunk", cwd=origin)
    (origin / "f.txt").write_text("1")
    _real_git("add", "f.txt", cwd=origin)
    _real_git("commit", "-q", "-m", "init", cwd=origin)
    subprocess.run(["git", "clone", "-q", str(origin), str(tmp_path / "repo")], check=True)
    (origin / "f.txt").write_text("2")
    _real_git("commit", "-q", "-am", "second", cwd=origin)
    conn = db.connect(tmp_path / "db.sqlite")

    assert repo.ensure_repo(make_config(tmp_path, tmp_path / "repo"), conn) is True

    assert log_rows(conn, "repo_pull_failed") == []
    assert (tmp_path / "repo" / "f.txt").read_text() == "2"


def test_git_stderr_is_masked_in_journal(tmp_path, monkeypatch):
    """stderr git может повторить URL с токеном - в журнал он идёт с маской."""
    monkeypatch.setattr(logging_setup, "_secrets", set())
    logging_setup.mask_secrets(*repo.url_secrets(TOKEN_URL))
    conn = db.connect(tmp_path / "db.sqlite")
    config = make_config(tmp_path, tmp_path / "repo")

    def fake_run(args, **kwargs):
        return FakeCompleted(128, stderr=f"fatal: could not read {TOKEN_URL}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    repo.ensure_repo(config, conn)

    stderr = log_rows(conn, "repo_pull_failed")[0]["stderr"]
    assert "ghp_secrettoken123" not in stderr
    assert logging_setup.MASK in stderr
