import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    pass


TELEGRAM_BOT_API_BASE_URL_DEFAULT = "https://api.telegram.org/bot"
# Каталоги, которые агент Справки читает инструментами (TGBOT_KNOWLEDGE_DIRS);
# относительные - от рабочего каталога процесса.
KNOWLEDGE_DIRS_DEFAULT = (Path("docs"),)
# Клон TGBOT_REPO_URL - рядом с базой: каталог данных бота и так доступен на запись.
REPO_DIR_NAME = "repo"

# Ступень LLM одна - OpenRouter (ADR-0004, спека llm-provider-selection): Claude и
# прямой OpenAI удалены. От отказа модели страхуют запасные
# модели той же ступени (лестница, ADR-0003), запасного провайдера нет. Режим
# оставлен переменной, чтобы прежние `openai`/`auto`/`claude` роняли старт, а не
# молча игнорировались.
# `omniroute` - свой OmniRoute (OpenAI-совместимый шлюз): ключ и модели свои,
# адрес - TGBOT_LLM_BASE_URL, поля, которые понимает только OpenRouter, не шлются.
LLM_PROVIDERS = ("openrouter", "omniroute")
LLM_PROVIDER_DEFAULT = "openrouter"

# Модель ступени OpenRouter по умолчанию. Дешёвая, с контекстом 1.3M и
# инструментами вместе с размышлением (проверено, спека llm-provider-selection).
OPENROUTER_MODEL_DEFAULT = "z-ai/glm-5.3-flash"

# Потолок провайдера на число моделей в одном запросе (поле `models` у
# OpenRouter): четвёртая даёт 400 "'models' array must have 3 items or fewer."
# (проверено живым запросом).
OPENROUTER_MODELS_MAX = 3

# Уровни квантизации, которым OpenRouter может отдать запрос (мусор
# glm-5.3-flash списан на дешёвых fp4-хостеров). Разрешающий список - другого
# у OpenRouter нет; `unknown` обязателен: у хостеров `openai/gpt-6-luna`
# квантизация не указана, без него фильтр отсекает модель целиком.
OPENROUTER_QUANTIZATIONS = ("fp8", "fp16", "bf16", "fp32", "unknown")

# Модели распознавания скриншотов: основная и запасная при ошибке (ADR-0005,
# замер 07.10 на 14 настоящих скриншотах - обе без единой ошибки в id).
VISION_MODELS_DEFAULT = ("google/gemini-3.1-flash-lite", "google/gemini-3.8-flash")

# Пороги лестницы моделей (спека llm-model-ladder): подключение
# второй ступени, третьей и сбой, секунды от начала вызова. Полные вызовы:
# Справка p90 73 с, укладывается в 3 минуты; лёгкие - в секунды.
LLM_STEPS_FULL = (120.0, 240.0, 600.0)
LLM_STEPS_LIGHT = (20.0, 40.0, 60.0)
LLM_CALL_CLASSES = ("full", "reference", "light")

# Напоминание админам о непрочитанных отзывах: пн-пт в этот час по этому поясу
# (TGBOT_REMINDER_TZ, TGBOT_REMINDER_HOUR).
REMINDER_TZ_DEFAULT = ZoneInfo("Asia/Almaty")
REMINDER_HOUR_DEFAULT = 15
HOURS_IN_DAY = 24


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    admin_user_ids: frozenset[int]
    db_path: Path
    knowledge_dirs: tuple[Path, ...] = KNOWLEDGE_DIRS_DEFAULT
    # Git-репозиторий, который `repo.py` клонирует и обновляет; None - клона нет.
    repo_url: str | None = None
    # Имя продукта в промптах Справки; None - "этот продукт" на языке ответа.
    product_name: str | None = None
    openrouter_api_key: str | None = None
    # Первая модель - основная, остальные - запасные модели лестницы (ADR-0003).
    openrouter_models: tuple[str, ...] = (OPENROUTER_MODEL_DEFAULT,)
    omniroute_api_key: str | None = None
    omniroute_models: tuple[str, ...] = ()
    vision_models: tuple[str, ...] = VISION_MODELS_DEFAULT
    llm_provider: str = LLM_PROVIDER_DEFAULT
    # Ключ классификатора Jev (typesafe.ai) для пограничных выродившихся
    # ответов; без него работает только регулярная часть проверки.
    typesafe_api_key: str | None = None
    text_debounce_s: float = 2.0
    agent_cache_ttl_s: float = 86400.0
    # Класс вызова -> пороги лестницы; переопределяется
    # TGBOT_LLM_<CLASS>_STEPS_S="120,240,600".
    llm_steps: Mapping[str, tuple[float, ...]] = field(default_factory=lambda: _default_steps())
    # Адрес Bot API; `{token}` подставляет PTB. Тестовый DC Telegram -
    # `https://api.telegram.org/bot{token}/test`, фейк в тестах - localhost.
    telegram_bot_api_base_url: str = TELEGRAM_BOT_API_BASE_URL_DEFAULT
    # Хост OpenAI-совместимого API (формат, не провайдер) вместо зашитого у ступени (фейк LLM в
    # тестах). None - адреса ступени как есть.
    llm_base_url: str | None = None
    reminder_tz: ZoneInfo = REMINDER_TZ_DEFAULT
    reminder_hour: int = REMINDER_HOUR_DEFAULT
    # Бот только для ADMIN_USER_IDS: allowlist и заявки на доступ выключены.
    owner_only: bool = False

    @property
    def repo_path(self) -> Path | None:
        """Каталог клона `repo_url`; он тоже читается агентом."""
        return self.db_path.parent / REPO_DIR_NAME if self.repo_url else None


def _parse_admin_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            raise ConfigError(
                f"ADMIN_USER_IDS: ожидались числовые user_id через запятую, получено {part!r}"
            )
    return frozenset(ids)


def _parse_llm_provider(raw: str) -> str:
    """Режим провайдера; опечатка должна ронять старт, а не всплыть на первом
    вызове модели получасом позже."""
    value = raw.strip().lower() or LLM_PROVIDER_DEFAULT
    if value not in LLM_PROVIDERS:
        raise ConfigError(
            f"TGBOT_LLM_PROVIDER: ожидалось одно из {', '.join(LLM_PROVIDERS)}, "
            f"получено {raw.strip()!r}"
        )
    return value


def _parse_models(raw: str, name: str = "OPENROUTER_MODEL") -> tuple[str, ...]:
    """Модели ступени OpenRouter: первая основная, остальные - запасные.

    Список длиннее потолка провайдера роняет старт: молча отбросить лишние
    нельзя - администратор считал бы, что четвёртая запасная модель настроена, а провайдер
    о нём не знает. Пустое значение - модель по умолчанию.
    """
    models = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not models:
        return (OPENROUTER_MODEL_DEFAULT,)
    if len(models) > OPENROUTER_MODELS_MAX:
        raise ConfigError(
            f"{name}: провайдер принимает не больше {OPENROUTER_MODELS_MAX} "
            f"моделей в одном запросе (первая - основная, остальные - запасные), "
            f"задано {len(models)}"
        )
    return models


def _parse_omniroute_models(raw: str, provider: str) -> tuple[str, ...]:
    """Модели OmniRoute. Имена у шлюза свои (префикс провайдера или combo),
    угадывать по умолчанию нечего: при `TGBOT_LLM_PROVIDER=omniroute` пусто
    роняет старт."""
    if raw.strip():
        return _parse_models(raw, "OMNIROUTE_MODEL")
    if provider == "omniroute":
        raise ConfigError(
            "OMNIROUTE_MODEL не задан: при TGBOT_LLM_PROVIDER=omniroute укажите "
            "модели шлюза через запятую (первая - основная)"
        )
    return ()


def _parse_vision_models(raw: str) -> tuple[str, ...]:
    """Модели распознавания скриншотов, тем же списком, что `OPENROUTER_MODEL`.

    Бесплатный вариант (`:free`) роняет старт: его провайдеры хранят запросы и
    обучаются на них, а на скриншотах - данные клиентов (ADR-0005).
    """
    models = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not models:
        return VISION_MODELS_DEFAULT
    if len(models) > OPENROUTER_MODELS_MAX:
        raise ConfigError(
            f"TGBOT_VISION_MODELS: провайдер принимает не больше "
            f"{OPENROUTER_MODELS_MAX} моделей, задано {len(models)}"
        )
    free = [m for m in models if m.endswith(":free")]
    if free:
        raise ConfigError(
            f"TGBOT_VISION_MODELS: бесплатные модели обучаются на запросах, "
            f"а на скриншотах данные клиентов - уберите {', '.join(free)}"
        )
    return models


def _parse_paths(raw: str) -> tuple[Path, ...]:
    """Список каталогов через запятую; пусто - `KNOWLEDGE_DIRS_DEFAULT`."""
    return tuple(Path(part.strip()) for part in raw.split(",") if part.strip()) or KNOWLEDGE_DIRS_DEFAULT


def _default_steps() -> dict[str, tuple[float, ...]]:
    return {cls: LLM_STEPS_LIGHT if cls == "light" else LLM_STEPS_FULL for cls in LLM_CALL_CLASSES}


def _parse_steps(env: Mapping[str, str]) -> dict[str, tuple[float, ...]]:
    """Пороги лестницы по классам вызова. Опечатка роняет старт: иначе она
    всплыла бы зависшим вопросом через десять минут ожидания."""
    steps = _default_steps()
    for cls in LLM_CALL_CLASSES:
        name = f"TGBOT_LLM_{cls.upper()}_STEPS_S"
        raw = env.get(name, "").strip()
        if not raw:
            continue
        try:
            values = tuple(float(part) for part in raw.split(","))
        except ValueError:
            raise ConfigError(f"{name}: ожидались три числа через запятую, получено {raw!r}") from None
        if len(values) != 3 or values[0] <= 0 or not values[0] < values[1] < values[2]:
            raise ConfigError(
                f"{name}: ожидались три положительных возрастающих порога "
                f"(вторая ступень, третья, сбой), получено {raw!r}"
            )
        steps[cls] = values
    return steps


def _parse_reminder_tz(raw: str) -> ZoneInfo:
    """Пояс напоминания; опечатка роняет старт, а не сдвигает тик молча."""
    name = raw.strip()
    if not name:
        return REMINDER_TZ_DEFAULT
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(
            f"TGBOT_REMINDER_TZ: неизвестный часовой пояс {name!r}, пример: Europe/Berlin"
        ) from None


def _parse_reminder_hour(raw: str) -> int:
    raw = raw.strip()
    if not raw:
        return REMINDER_HOUR_DEFAULT
    if not (raw.isascii() and raw.isdigit()) or int(raw) >= HOURS_IN_DAY:
        raise ConfigError(f"TGBOT_REMINDER_HOUR: ожидался час 0-23, получено {raw!r}")
    return int(raw)


# Схемы TGBOT_REPO_URL: https/ssh для продукта, file:// - локальный клон
# (deploy/session-log-check.sh), `user@host:path` - короткая ssh-форма.
# http:// отклоняется: токен из URL ушёл бы открытым текстом; ведущий "-" -
# опция git, а не адрес; `ext::` и прочие транспорты git исполняют команды.
REPO_URL_PREFIXES = ("https://", "ssh://", "file://")
_SCP_LIKE_URL = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^-/][^\s]*$")


def _parse_repo_url(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith(REPO_URL_PREFIXES) or _SCP_LIKE_URL.match(raw):
        return raw
    raise ConfigError(
        "TGBOT_REPO_URL: ожидался https://, ssh://, file:// или git@host:path "
        "(без ведущего '-')"
    )


def _check_logs_max_bytes(raw: str) -> None:
    """Потолок каталога журнала. Значение читает `journal` сам, на ротации, а её
    ошибки он гасит - без проверки здесь опечатка молча выключила бы ротацию."""
    raw = raw.strip()
    if raw and not (raw.isascii() and raw.isdigit()):
        raise ConfigError(
            f"TGBOT_LOGS_MAX_BYTES: ожидалось целое число байт >= 0, получено {raw!r}"
        )


# Файлы окружения, из которых добираются переменные, не заданные в окружении
# процесса. На проде их читает systemd (`EnvironmentFile`), но локальный запуск
# уходил бы без ключей: `.env` рядом с репозиторием не грузит никто. Порядок
# значим - действует первое найденное значение; на каждой площадке существует не
# весь список, и это нормально.
DEPLOY_ENV_FILE = "/opt/tg-bot/.env"
ENV_FILES = ("./.env", DEPLOY_ENV_FILE, "~/.env")
# Свой путь деплой-файла вместо DEPLOY_ENV_FILE. Читается только из окружения
# процесса: чтобы найти файл, путь нужен до его чтения.
ENV_FILE_VAR = "TGBOT_ENV_FILE"


def default_env_files(environ: Mapping[str, str]) -> tuple[str, ...]:
    """`ENV_FILES`, где деплой-путь заменён на `TGBOT_ENV_FILE`, если он задан."""
    custom = environ.get(ENV_FILE_VAR, "").strip()
    if not custom:
        return tuple(ENV_FILES)
    return tuple(custom if path == DEPLOY_ENV_FILE else path for path in ENV_FILES)


def _parse_env_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE` из файла окружения; нечитаемый файл - пустой результат.

    Ровно то, что встречается в наших `.env`: комментарии, пустые строки,
    префикс `export`, кавычки по краям значения. Расширенного синтаксиса
    (интерполяция, multiline) у нас нет, поэтому и зависимости на него нет.
    Строка без `=` пропускается: ронять старт из-за мусора в файле незачем -
    обязательные переменные проверяет `load_config`.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:  # нет файла, каталог вместо файла, нет прав
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _env_with_files(env: Mapping[str, str], paths) -> Mapping[str, str]:
    """Окружение процесса, дополненное значениями из файлов.

    Окружение сильнее файла: забытый `.env` в рабочей копии не должен перебивать
    то, что подставил systemd. Значения не логируются - это секреты.
    """
    merged: dict[str, str] = {}
    for raw in paths:
        for key, value in _parse_env_file(Path(raw).expanduser()).items():
            merged.setdefault(key, value)  # первый файл списка сильнее
    merged.update(env)
    return merged


def load_config(env: Mapping[str, str] | None = None, env_files=None) -> Config:
    """`env=None` - окружение процесса плюс файлы `default_env_files()`.

    С явно переданным `env` файлы по умолчанию НЕ читаются: вызов с готовым
    словарём (тесты, проверки) обязан быть герметичным, иначе `.env` рядом с
    рабочим каталогом менял бы их результат."""
    if env is None:
        env = os.environ
        if env_files is None:
            env_files = default_env_files(env)
    env = _env_with_files(env, env_files or ())

    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN не задан. Укажите токен бота из BotFather "
            "в переменной окружения TELEGRAM_BOT_TOKEN (env systemd-юнита или .env)."
        )

    _check_logs_max_bytes(env.get("TGBOT_LOGS_MAX_BYTES", ""))
    provider = _parse_llm_provider(env.get("TGBOT_LLM_PROVIDER", ""))
    admins = _parse_admin_ids(env.get("ADMIN_USER_IDS", ""))
    owner_only = env.get("TGBOT_OWNER_ONLY", "").strip().lower() in ("1", "true", "yes")
    if owner_only and not admins:
        raise ConfigError("TGBOT_OWNER_ONLY включён, а ADMIN_USER_IDS пуст: бота не сможет использовать никто")
    return Config(
        telegram_bot_token=token,
        admin_user_ids=admins,
        db_path=Path(env.get("TGBOT_DB_PATH") or "./tgbot.db"),
        knowledge_dirs=_parse_paths(env.get("TGBOT_KNOWLEDGE_DIRS", "")),
        repo_url=_parse_repo_url(env.get("TGBOT_REPO_URL", "")),
        product_name=env.get("TGBOT_PRODUCT_NAME", "").strip() or None,
        openrouter_api_key=env.get("OPENROUTER_API_KEY") or None,
        openrouter_models=_parse_models(env.get("OPENROUTER_MODEL", "")),
        vision_models=_parse_vision_models(env.get("TGBOT_VISION_MODELS", "")),
        omniroute_api_key=env.get("OMNIROUTE_API_KEY") or None,
        omniroute_models=_parse_omniroute_models(env.get("OMNIROUTE_MODEL", ""), provider),
        llm_provider=provider,
        typesafe_api_key=env.get("TYPESAFE_API_KEY") or None,
        text_debounce_s=float(env.get("TGBOT_TEXT_DEBOUNCE_S") or 2.0),
        agent_cache_ttl_s=float(env.get("TGBOT_AGENT_CACHE_TTL_S") or 86400.0),
        llm_steps=_parse_steps(env),
        telegram_bot_api_base_url=(
            env.get("TELEGRAM_BOT_API_BASE_URL", "").strip() or TELEGRAM_BOT_API_BASE_URL_DEFAULT
        ),
        llm_base_url=env.get("TGBOT_LLM_BASE_URL", "").strip().rstrip("/") or None,
        reminder_tz=_parse_reminder_tz(env.get("TGBOT_REMINDER_TZ", "")),
        reminder_hour=_parse_reminder_hour(env.get("TGBOT_REMINDER_HOUR", "")),
        owner_only=owner_only,
    )
