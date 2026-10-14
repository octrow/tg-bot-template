import fcntl

import tgbot
from tgbot import __main__ as main_mod
from tgbot import db, logging_setup, repo

from .conftest import log_rows


def test_main_returns_nonzero_and_skips_build_when_lock_busy(tmp_path, monkeypatch):
    """Занятый лок: main() не строит приложение и завершается с кодом != 0."""
    lock_path = tmp_path / "bot.lock"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TGBOT_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("TGBOT_LOCK_PATH", str(lock_path))

    with open(lock_path, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def fail_if_called(*args, **kwargs):
            raise AssertionError("build_application не должен вызываться при занятом локе")

        monkeypatch.setattr(main_mod, "build_application", fail_if_called)

        code = main_mod.main()

        assert code != 0
        fcntl.flock(held, fcntl.LOCK_UN)


def test_main_acquires_and_releases_lock_on_normal_run(tmp_path, monkeypatch):
    """Обычный запуск: лок берётся, а после run_polling() снова свободен."""
    lock_path = tmp_path / "bot.lock"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TGBOT_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("TGBOT_LOCK_PATH", str(lock_path))

    class FakeApp:
        bot_data: dict = {}

        def run_polling(self):
            # Внутри run_polling лок должен быть занят этим же процессом.
            with open(lock_path, "w") as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    raise AssertionError("лок не должен быть свободен во время run_polling")
                except OSError:
                    pass

    monkeypatch.setattr(main_mod, "build_application", lambda config, conn: FakeApp())

    code = main_mod.main()

    assert code == 0

    # После завершения main() лок снова свободен - можно перезапустить бота.
    with open(lock_path, "w") as probe:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(probe, fcntl.LOCK_UN)


def test_startup_records_version_and_llm_provider(tmp_path, monkeypatch, caplog):
    """Старт пишет версию и режим провайдера - и в лог, и в payload `startup`.

    Именно это при разборе на сервере отвечает на два вопроса сразу: какая
    сборка крутится и в каком режиме она поднялась. Событие `startup` одно,
    поэтому и проверка одна.
    """
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TGBOT_DB_PATH", str(db_path))
    monkeypatch.setenv("TGBOT_LOCK_PATH", str(tmp_path / "bot.lock"))
    monkeypatch.setenv("TGBOT_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv(
        "OPENROUTER_MODEL", "z-ai/glm-5.3-flash,openai/gpt-6-luna,deepseek/deepseek-v4.1-flash",
    )

    class IdleApp:
        bot_data: dict = {}

        def run_polling(self):
            pass

    monkeypatch.setattr(main_mod, "build_application", lambda config, conn: IdleApp())

    with caplog.at_level("INFO", logger="tgbot.__main__"):
        assert main_mod.main() == 0

    assert tgbot.__version__ in caplog.text
    assert "openrouter" in caplog.text

    conn = db.connect(db_path)
    try:
        startup = log_rows(conn, "startup")[-1]
        assert startup["version"] == tgbot.__version__
        assert startup["llm_provider"] == "openrouter"
        # Модель действующей ступени, а не константа одной из них: смену модели
        # видно по журналу без чтения кода.
        assert startup["model"] == "z-ai/glm-5.3-flash"
        # Вся лестница по порядку: основная модель не меняется, и без
        # списка смену запасных по журналу не увидеть.
        assert startup["models"] == [
            "z-ai/glm-5.3-flash", "openai/gpt-6-luna", "deepseek/deepseek-v4.1-flash",
        ]
        # Ступень одна (ADR-0004): расходиться заданному режиму не с чем.
        assert "llm_provider_requested" not in startup
    finally:
        conn.close()


def test_startup_tries_repo_and_starts_anyway(tmp_path, monkeypatch, caplog):
    """Клон проверяется на старте; неудача пишется в лог, но не мешает боту."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TGBOT_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("TGBOT_LOCK_PATH", str(tmp_path / "bot.lock"))
    monkeypatch.setenv("TGBOT_REPO_URL", "https://example.com/knowledge.git")

    calls: list = []

    def fake_ensure(config, conn):
        calls.append(config.repo_path)
        return False

    class IdleApp:
        bot_data: dict = {}

        def run_polling(self):
            pass

    monkeypatch.setattr(repo, "ensure_repo", fake_ensure)
    monkeypatch.setattr(main_mod, "build_application", lambda config, conn: IdleApp())

    with caplog.at_level("WARNING", logger="tgbot.__main__"):
        assert main_mod.main() == 0

    assert calls == [tmp_path / "repo"]
    assert "ответов по коду не будет" in caplog.text


def test_main_returns_nonzero_after_instance_conflict(tmp_path, monkeypatch):
    """Polling остановлен из-за Conflict (второй поллер) - main() выходит с
    кодом != 0, чтобы systemd счёл это сбоем и перезапустил бот."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TGBOT_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("TGBOT_LOCK_PATH", str(tmp_path / "bot.lock"))

    class ConflictedApp:
        bot_data: dict

        def __init__(self):
            self.bot_data = {}

        def run_polling(self):
            # Так error handler помечает конфликт перед stop_running().
            self.bot_data["instance_conflict"] = True

    monkeypatch.setattr(main_mod, "build_application", lambda config, conn: ConflictedApp())

    assert main_mod.main() != 0


async def test_logs_command_is_in_admin_menu_only(monkeypatch):
    """Меню админа пополняется /logs; обычный пользователь его не видит.

    Проверяется не константа, а то, что уходит в set_my_commands: меню и /help
    берут один и тот же ADMIN_COMMANDS, но зарегистрировано в Telegram именно
    то, что здесь.
    """
    calls: list[tuple[list[str], object | None]] = []

    class MenuBot:
        async def set_my_commands(self, commands, scope=None, **kwargs):
            calls.append(([c.command for c in commands], scope))

    from types import SimpleNamespace

    from tgbot.bot import make_post_init

    await make_post_init([777])(SimpleNamespace(bot=MenuBot()))

    default_commands, default_scope = calls[0]
    admin_commands, admin_scope = calls[2]
    assert default_scope is None and "logs" not in default_commands
    assert admin_scope.chat_id == 777 and "logs" in admin_commands


def test_repo_url_credentials_are_masked_in_logs(tmp_path, monkeypatch):
    """Токен из TGBOT_REPO_URL регистрируется маской, как ключи LLM."""
    monkeypatch.setattr(logging_setup, "_secrets", set())
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TGBOT_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("TGBOT_LOCK_PATH", str(tmp_path / "bot.lock"))
    monkeypatch.setenv("TGBOT_REPO_URL", "https://ghp_secrettoken123@github.com/o/r.git")

    class IdleApp:
        bot_data: dict = {}

        def run_polling(self):
            pass

    monkeypatch.setattr(repo, "ensure_repo", lambda config, conn: True)
    monkeypatch.setattr(main_mod, "build_application", lambda config, conn: IdleApp())

    assert main_mod.main() == 0

    assert "ghp_secrettoken123" not in logging_setup.redact(
        "fatal: https://ghp_secrettoken123@github.com/o/r.git"
    )
