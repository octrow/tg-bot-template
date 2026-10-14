from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tgbot.config import Config, ConfigError, load_config


def test_missing_token_raises_with_readable_message():
    with pytest.raises(ConfigError) as exc:
        load_config({})
    assert "TELEGRAM_BOT_TOKEN" in str(exc.value)


def test_defaults():
    config = load_config({"TELEGRAM_BOT_TOKEN": "t"})
    assert config.db_path == Path("./tgbot.db")
    assert config.admin_user_ids == frozenset()
    assert config.knowledge_dirs == (Path("docs"),)
    assert config.repo_url is None and config.repo_path is None
    assert config.product_name is None


def test_knowledge_dirs_repo_url_and_product_name():
    config = load_config({
        "TELEGRAM_BOT_TOKEN": "t", "TGBOT_KNOWLEDGE_DIRS": "docs, /srv/kb ,",
        "TGBOT_REPO_URL": "https://example.com/kb.git", "TGBOT_DB_PATH": "/var/bot/bot.db",
        "TGBOT_PRODUCT_NAME": "Акме",
    })
    assert config.knowledge_dirs == (Path("docs"), Path("/srv/kb"))
    assert config.repo_url == "https://example.com/kb.git"
    assert config.repo_path == Path("/var/bot/repo")  # клон - рядом с базой
    assert config.product_name == "Акме"


def test_admin_ids_parsed():
    config = load_config({"TELEGRAM_BOT_TOKEN": "t", "ADMIN_USER_IDS": "1, 2 ,3,"})
    assert config.admin_user_ids == frozenset({1, 2, 3})


def test_admin_ids_invalid():
    with pytest.raises(ConfigError):
        load_config({"TELEGRAM_BOT_TOKEN": "t", "ADMIN_USER_IDS": "1,abc"})


def test_llm_provider_defaults_to_openrouter():
    """Ступень одна - OpenRouter (ADR-0004): один вход к моделям, цена вызова
    приходит в ответе."""
    assert load_config({"TELEGRAM_BOT_TOKEN": "t"}).llm_provider == "openrouter"


def test_retired_provider_modes_stop_startup():
    """Ступеней Claude и прямого OpenAI больше нет (ADR-0004): оставшееся в
    окружении `auto`/`claude`/`openai` обязано ронять старт, а не молча
    откатываться на дефолт - иначе администратор думает, что бот ходит туда."""
    for retired in ("auto", "claude", "openai"):
        with pytest.raises(ConfigError) as exc:
            load_config({"TELEGRAM_BOT_TOKEN": "t", "TGBOT_LLM_PROVIDER": retired})
        assert "ожидалось одно из openrouter," in str(exc.value)


def test_leftover_openai_variables_change_nothing():
    """Ключ и модель прямого OpenAI больше не читаются: оставшиеся на площадке
    значения безвредны - старт не падает, конфигурация та же."""
    base = load_config({"TELEGRAM_BOT_TOKEN": "t"})
    leftover = load_config({
        "TELEGRAM_BOT_TOKEN": "t", "OPENAI_API_KEY": "sk-old", "OPENAI_MODEL": "gpt-5.6-luna",
    })
    assert leftover == base
    assert not hasattr(leftover, "openai_api_key")


def test_openrouter_defaults_and_env():
    """Ключа нет - None (ступень просто не сработает); модель по умолчанию
    задана в коде, чтобы её смена не требовала правки окружения."""
    config = load_config({"TELEGRAM_BOT_TOKEN": "t"})
    assert config.openrouter_api_key is None
    assert config.openrouter_models == ("z-ai/glm-5.3-flash",)

    config = load_config({
        "TELEGRAM_BOT_TOKEN": "t",
        "OPENROUTER_API_KEY": "sk-or-1",
        "OPENROUTER_MODEL": "z-ai/glm-5.3",
    })
    assert config.openrouter_api_key == "sk-or-1"
    assert config.openrouter_models == ("z-ai/glm-5.3",)


def test_llm_provider_default_matches_dataclass():
    assert load_config({"TELEGRAM_BOT_TOKEN": "t"}).llm_provider == Config.llm_provider


def test_llm_provider_case_and_spaces_ignored():
    config = load_config({"TELEGRAM_BOT_TOKEN": "t", "TGBOT_LLM_PROVIDER": " OpenRouter "})
    assert config.llm_provider == "openrouter"


def test_llm_provider_invalid_stops_startup():
    with pytest.raises(ConfigError) as exc:
        load_config({"TELEGRAM_BOT_TOKEN": "t", "TGBOT_LLM_PROVIDER": "openal"})
    assert "ожидалось одно из openrouter," in str(exc.value)


def test_claude_config_is_gone():
    """Ступень Claude удалена вместе со своими переменными: остаточные значения в
    окружении не должны ни читаться, ни влиять на старт."""
    config = load_config({
        "TELEGRAM_BOT_TOKEN": "t",
        "ANTHROPIC_API_KEY": "sk-ant",
        "TGBOT_CLAUDE_TIMEOUT_S": "не число",
    })
    assert not hasattr(config, "anthropic_api_key")
    assert not hasattr(config, "claude_timeout_s")


# --- Ключ из файлов окружения (спека llm-provider-selection) ------


def _write_env(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_value_taken_from_env_file_when_absent_in_process_env(tmp_path):
    """`.env` рядом с репозиторием не грузит никто (на проде это systemd), и
    локальный запуск уходил бы без ключа."""
    env_file = _write_env(tmp_path / ".env", "OPENROUTER_API_KEY=sk-or-from-file\n")
    config = load_config({"TELEGRAM_BOT_TOKEN": "t"}, env_files=[env_file])
    assert config.openrouter_api_key == "sk-or-from-file"


def test_process_env_wins_over_file(tmp_path):
    """Забытый `.env` в рабочей копии не должен перебивать то, что подставил
    systemd."""
    env_file = _write_env(tmp_path / ".env", "OPENROUTER_API_KEY=sk-from-file\n")
    config = load_config(
        {"TELEGRAM_BOT_TOKEN": "t", "OPENROUTER_API_KEY": "sk-from-env"},
        env_files=[env_file],
    )
    assert config.openrouter_api_key == "sk-from-env"


def test_first_file_in_list_wins(tmp_path):
    first = _write_env(tmp_path / "first.env", "OPENROUTER_MODEL=z-ai/glm-5.3\n")
    second = _write_env(tmp_path / "second.env", "OPENROUTER_MODEL=z-ai/glm-5.2\n")
    config = load_config({"TELEGRAM_BOT_TOKEN": "t"}, env_files=[first, second])
    assert config.openrouter_models == ("z-ai/glm-5.3",)


def test_missing_and_unreadable_files_are_skipped(tmp_path):
    """Список путей общий для всех площадок, и на каждой существует не весь."""
    present = _write_env(tmp_path / "present.env", "OPENROUTER_API_KEY=sk-ok\n")
    config = load_config(
        {"TELEGRAM_BOT_TOKEN": "t"},
        env_files=[tmp_path / "нет-такого.env", tmp_path, present],
    )
    assert config.openrouter_api_key == "sk-ok"


def test_env_file_syntax(tmp_path):
    """Комментарии, пустые строки, `export` и кавычки по краям - всё, что реально
    встречается в наших .env; строка без `=` не должна ронять старт."""
    env_file = _write_env(
        tmp_path / ".env",
        "\n".join([
            "# комментарий",
            "",
            "   ",
            "мусор без равно",
            "export OPENROUTER_API_KEY='sk-quoted'",
            'OPENROUTER_MODEL="z-ai/glm-5.3-flash"',
            "OPENAI_API_KEY=  sk-spaces  ",
        ]) + "\n",
    )
    config = load_config({"TELEGRAM_BOT_TOKEN": "t"}, env_files=[env_file])
    assert config.openrouter_api_key == "sk-quoted"
    assert config.openrouter_models == ("z-ai/glm-5.3-flash",)
    assert not hasattr(config, "openai_api_key")


def test_explicit_env_reads_no_files_by_default(tmp_path, monkeypatch):
    """Тесты и вызовы с явным окружением остаются герметичными: файл рядом с
    рабочим каталогом на них не влияет."""
    monkeypatch.chdir(tmp_path)
    _write_env(tmp_path / ".env", "OPENROUTER_API_KEY=sk-from-cwd\n")
    assert load_config({"TELEGRAM_BOT_TOKEN": "t"}).openrouter_api_key is None




# --- Напоминание об отзывах: пояс и час из настроек ---


def test_reminder_defaults_keep_almaty_at_15():
    config = load_config({"TELEGRAM_BOT_TOKEN": "t"})
    assert config.reminder_tz == ZoneInfo("Asia/Almaty")
    assert config.reminder_hour == 15


def test_reminder_zone_and_hour_from_env():
    config = load_config({
        "TELEGRAM_BOT_TOKEN": "t", "TGBOT_REMINDER_TZ": " Europe/Berlin ", "TGBOT_REMINDER_HOUR": "9",
    })
    assert config.reminder_tz == ZoneInfo("Europe/Berlin")
    assert config.reminder_hour == 9


def test_unknown_reminder_zone_stops_startup():
    with pytest.raises(ConfigError) as exc:
        load_config({"TELEGRAM_BOT_TOKEN": "t", "TGBOT_REMINDER_TZ": "Mars/Olympus"})
    assert "TGBOT_REMINDER_TZ" in str(exc.value)


@pytest.mark.parametrize("raw", ["24", "-1", "15:00", "x"])
def test_bad_reminder_hour_stops_startup(raw):
    with pytest.raises(ConfigError) as exc:
        load_config({"TELEGRAM_BOT_TOKEN": "t", "TGBOT_REMINDER_HOUR": raw})
    assert "TGBOT_REMINDER_HOUR" in str(exc.value)

# --- Путь деплой-файла окружения: TGBOT_ENV_FILE из окружения процесса ---


def test_default_env_files_keep_the_deploy_path(monkeypatch):
    from tgbot import config as config_mod

    monkeypatch.undo()  # вернуть настоящий ENV_FILES (conftest его гасит); файлы не читаются

    assert config_mod.default_env_files({}) == ("./.env", "/opt/tg-bot/.env", "~/.env")


def test_tgbot_env_file_replaces_the_deploy_path(monkeypatch):
    from tgbot import config as config_mod

    monkeypatch.undo()

    files = config_mod.default_env_files({"TGBOT_ENV_FILE": "/srv/my-bot/.env"})
    assert files == ("./.env", "/srv/my-bot/.env", "~/.env")


def test_process_env_points_load_config_at_its_env_file(tmp_path, monkeypatch):
    """Путь задан в окружении процесса - значения добираются из этого файла."""
    monkeypatch.setattr("tgbot.config.ENV_FILES", ("/opt/tg-bot/.env",))
    env_file = tmp_path / "bot.env"
    _write_env(env_file, "OPENROUTER_API_KEY=sk-from-custom\n")
    monkeypatch.setattr("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TGBOT_ENV_FILE": str(env_file)})
    assert load_config().openrouter_api_key == "sk-from-custom"


def test_tgbot_env_file_inside_a_file_is_not_followed(tmp_path):
    """Ключ в самом файле ничего не меняет: путь к файлу нужен до его чтения."""
    other = tmp_path / "other.env"
    _write_env(other, "OPENROUTER_API_KEY=sk-other\n")
    env_file = tmp_path / ".env"
    _write_env(env_file, f"TGBOT_ENV_FILE={other}\n")
    config = load_config({"TELEGRAM_BOT_TOKEN": "t"}, env_files=[env_file])
    assert config.openrouter_api_key is None

# --- Список моделей ступени OpenRouter (спека llm-provider-selection)


def test_model_list_parsed_in_order():
    """Первая модель - основная, остальные дублёры; порядок значим."""
    config = load_config({
        "TELEGRAM_BOT_TOKEN": "t",
        "OPENROUTER_MODEL": "z-ai/glm-5.3-flash, deepseek/deepseek-v4-flash ,openai/gpt-5.6-luna,",
    })
    assert config.openrouter_models == (
        "z-ai/glm-5.3-flash", "deepseek/deepseek-v4-flash", "openai/gpt-5.6-luna",
    )


def test_model_list_longer_than_provider_cap_stops_startup():
    """Потолок провайдера - 3 модели в запросе (400 "'models' array must have 3
    items or fewer."). Молча отбросить лишние нельзя: администратор считал бы,
    что дублёр настроен, а провайдер о нём не знает."""
    with pytest.raises(ConfigError) as exc:
        load_config({
            "TELEGRAM_BOT_TOKEN": "t",
            "OPENROUTER_MODEL": "a/one,b/two,c/three,d/four,e/five",
        })
    assert "3" in str(exc.value) and "5" in str(exc.value)


def test_empty_model_value_falls_back_to_default():
    config = load_config({"TELEGRAM_BOT_TOKEN": "t", "OPENROUTER_MODEL": " , "})
    assert config.openrouter_models == ("z-ai/glm-5.3-flash",)


def test_api_base_urls_default():
    config = load_config({"TELEGRAM_BOT_TOKEN": "t"})
    assert config.telegram_bot_api_base_url == "https://api.telegram.org/bot"
    assert config.llm_base_url is None


def test_api_base_urls_from_env():
    config = load_config({
        "TELEGRAM_BOT_TOKEN": "t",
        "TELEGRAM_BOT_API_BASE_URL": " https://api.telegram.org/bot{token}/test ",
        "TGBOT_LLM_BASE_URL": "http://127.0.0.1:9/api/v1/",
    })
    assert config.telegram_bot_api_base_url == "https://api.telegram.org/bot{token}/test"
    assert config.llm_base_url == "http://127.0.0.1:9/api/v1"


def test_bot_uses_api_base_url():
    from tgbot import db
    from tgbot.bot import build_application

    config = load_config({
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "TELEGRAM_BOT_API_BASE_URL": "https://api.telegram.org/bot{token}/test",
    })
    app = build_application(config, db.connect(":memory:"))
    assert app.bot.base_url == "https://api.telegram.org/bot123:abc/test"
    assert app.bot.base_file_url == "https://api.telegram.org/file/bot123:abc/test"


# --- модели распознавания скриншотов (спека screenshot-input, 1.3) ---------

def test_vision_models_default_to_the_measured_pair():
    cfg = load_config({"TELEGRAM_BOT_TOKEN": "x"}, env_files=[])
    assert cfg.vision_models == ("google/gemini-3.1-flash-lite", "google/gemini-3.8-flash")
    assert Config.vision_models == cfg.vision_models


def test_vision_models_are_read_as_a_list():
    cfg = load_config(
        {"TELEGRAM_BOT_TOKEN": "x", "TGBOT_VISION_MODELS": " a/one , b/two "}, env_files=[]
    )
    assert cfg.vision_models == ("a/one", "b/two")


def test_a_free_vision_model_stops_startup():
    with pytest.raises(ConfigError) as exc:
        load_config(
            {"TELEGRAM_BOT_TOKEN": "x", "TGBOT_VISION_MODELS": "a/one,google/gemma-4-26b-a4b-it:free"},
            env_files=[],
        )
    assert ":free" in str(exc.value)


def test_more_than_three_vision_models_stop_startup():
    with pytest.raises(ConfigError):
        load_config({"TELEGRAM_BOT_TOKEN": "x", "TGBOT_VISION_MODELS": "a,b,c,d"}, env_files=[])


# --- потолок файлового журнала: его читает journal, но опечатка роняет старт ---

@pytest.mark.parametrize("raw", ["1G", "-1", "1.5"])
def test_invalid_logs_max_bytes_stops_startup(raw):
    """Журнал читает `TGBOT_LOGS_MAX_BYTES` сам, на ротации, и гасит её ошибки.
    Без проверки здесь опечатка молча выключила бы ротацию и каталог рос бы без предела."""
    with pytest.raises(ConfigError) as exc:
        load_config({"TELEGRAM_BOT_TOKEN": "x", "TGBOT_LOGS_MAX_BYTES": raw}, env_files=[])
    assert "TGBOT_LOGS_MAX_BYTES" in str(exc.value)


@pytest.mark.parametrize("raw", ["", "0", " 1048576 "])
def test_valid_logs_max_bytes_is_accepted(raw):
    load_config({"TELEGRAM_BOT_TOKEN": "x", "TGBOT_LOGS_MAX_BYTES": raw}, env_files=[])


@pytest.mark.parametrize("url", [
    "https://github.com/o/r.git",
    "https://tok@github.com/o/r.git",
    "ssh://git@github.com/o/r.git",
    "git@github.com:o/r.git",
    "file:///srv/product",
])
def test_repo_url_accepts_supported_schemes(url):
    assert load_config({"TELEGRAM_BOT_TOKEN": "t", "TGBOT_REPO_URL": url}).repo_url == url


@pytest.mark.parametrize("url", [
    "--upload-pack=touch /tmp/x",
    "-u",
    "http://github.com/o/r.git",
    "ext::sh -c touch% /tmp/x",
    "/srv/product",
    "github.com/o/r",
])
def test_repo_url_rejects_options_and_unknown_schemes(url):
    with pytest.raises(ConfigError) as exc:
        load_config({"TELEGRAM_BOT_TOKEN": "t", "TGBOT_REPO_URL": url})
    assert "TGBOT_REPO_URL" in str(exc.value)
