"""Ступень LLM и её описание: вход, ключ, модели, размышление лёгкого запроса,
выбор хостера (ADR 0003, ADR 0004) - и каталоги, которые агент читает."""

from pathlib import Path

from ..config import KNOWLEDGE_DIRS_DEFAULT, OPENROUTER_MODEL_DEFAULT, OPENROUTER_QUANTIZATIONS

# Ступень, исполнившая вызов (см. spec llm-stage-selection). Значение одно, но
# поле остаётся в журнале и ключе кэша: старые записи с `openai` сопоставимы.
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_OMNIROUTE = "omniroute"

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
RESPONSES_URL = "https://openrouter.ai/api/v1/responses"

# Размышление лёгкого запроса - минимум, доступный у провайдера:
# `z-ai/glm-5.3-flash` отвечает на отключение 400 "Reasoning is mandatory for
# this endpoint and cannot be disabled" и принимает только минимальное усилие.
LIGHT_REASONING = {"reasoning": {"effort": "minimal"}}

# Адрес OmniRoute по умолчанию (локальный шлюз); другой - TGBOT_LLM_BASE_URL.
OMNIROUTE_URL = "http://localhost:20128/v1"

# Описание ступени: вход, ключ, модели, размышление лёгкого запроса, выбор
# хостера. Серверного состояния у OpenRouter нет (`store` обязан быть false),
# историю полного вызова шлём мы (спека llm-provider-selection).
STAGES = {
    PROVIDER_OPENROUTER: {
        "key_attr": "openrouter_api_key",
        "model_attr": "openrouter_models",
        "model_default": OPENROUTER_MODEL_DEFAULT,
        "chat_url": CHAT_URL,
        "responses_url": RESPONSES_URL,
        "light_reasoning": LIGHT_REASONING,
        "routing": {"provider": {"quantizations": list(OPENROUTER_QUANTIZATIONS)}},
    },
    # Без `routing`: фильтр хостеров - поле OpenRouter. Размышление - полем
    # chat completions OpenAI; не принятое моделью снимается повтором без него.
    PROVIDER_OMNIROUTE: {
        "key_attr": "omniroute_api_key",
        "model_attr": "omniroute_models",
        "model_default": (),
        "chat_url": f"{OMNIROUTE_URL}/chat/completions",
        "responses_url": f"{OMNIROUTE_URL}/responses",
        "light_reasoning": {"reasoning_effort": "low"},
        "routing": {},
    },
}

# Потолок ответа в лёгком режиме: там ждём короткий ответ без рассуждений.
LIGHT_MAX_TOKENS = 200

# Потолок полного запроса, рассуждения входят в него (сломавшаяся
# модель выдала 44k токенов за 40 минут). Нормальные ответы по логам не больше
# 1 580 токенов; потолок с запасом в 2,5 раза.
FULL_MAX_TOKENS = 4000


def stage_url(config, url: str) -> str:
    """Адрес ступени с учётом `TGBOT_LLM_BASE_URL`: хост меняется, путь
    после `/v1` остаётся (фейк LLM в тестах повторяет пути OpenRouter)."""
    base = getattr(config, "llm_base_url", None)
    return base + url.split("/v1", 1)[1] if base else url


def effective_provider(config) -> str:
    """Ступень, которая будет вызвана: `TGBOT_LLM_PROVIDER` (OpenRouter или
    OmniRoute), запасной нет (ADR 0004). Она задаёт ключ кэша и первую ступень
    лестницы (`ladder_rungs`); без ключа агент недоступен, и сообщение называет
    переменную ключа ступени."""
    return getattr(config, "llm_provider", None) or PROVIDER_OPENROUTER


def stage_models(config, provider: str) -> tuple[str, ...]:
    """Модели ступени: первая основная, остальные - запасные модели лестницы.
    Строку (одна модель из тестового конфига) нормализуем здесь, а не у каждого
    вызывающего."""
    stage = STAGES[provider]
    raw = getattr(config, stage["model_attr"], None) or stage["model_default"]
    return (raw,) if isinstance(raw, str) else tuple(raw)


def active_model(config) -> str:
    """Основная модель действующей ступени - её называют `/help` и startup."""
    return stage_models(config, effective_provider(config))[0]


def knowledge_roots(config) -> list[Path]:
    """Каталоги, которые агент читает инструментами: `KNOWLEDGE_DIRS` и клон
    `REPO_URL`. Только существующие: клона может не быть (клонирование не
    удалось), и это не повод отдавать агенту каталог процесса."""
    dirs = getattr(config, "knowledge_dirs", KNOWLEDGE_DIRS_DEFAULT)
    candidates = (*dirs, getattr(config, "repo_path", None))
    return [path.resolve() for path in candidates if path is not None and path.is_dir()]
