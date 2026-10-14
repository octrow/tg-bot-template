"""Действующая модель в `/help` (спека llm-provider-selection)."""

from types import SimpleNamespace

from tgbot.agent.stages import active_model


def test_active_model_follows_provider():
    config = SimpleNamespace(llm_provider="openrouter", openrouter_model="z-ai/glm-5.3-flash")
    assert active_model(config) == "z-ai/glm-5.3-flash"


def test_active_model_falls_back_to_stage_default():
    """Поля в конфиге нет (SimpleNamespace в тестах) - показываем дефолт
    действующей ступени, а не роняем `/help`."""
    assert active_model(SimpleNamespace(llm_provider="openrouter")) == "z-ai/glm-5.3-flash"
    # Режима нет вовсе - действует дефолтная ступень, то есть OpenRouter.
    assert active_model(SimpleNamespace()) == "z-ai/glm-5.3-flash"


def test_active_model_is_the_primary_of_the_list():
    """Дублёры не показываем: пользователю важно, какая модель основная."""
    config = SimpleNamespace(
        llm_provider="openrouter",
        openrouter_models=("z-ai/glm-5.3-flash", "deepseek/deepseek-v4-flash"),
    )
    assert active_model(config) == "z-ai/glm-5.3-flash"


# --- Ступень одна (ADR 0004) --------------------------------------------------


def test_effective_stage_is_openrouter_whatever_keys_remain():
    """Выбирать по ключам больше не из чего: оставшийся ключ прямого OpenAI не
    делает его ступенью, а без ключа OpenRouter сообщение назовёт его ключ."""
    from tgbot.agent.stages import effective_provider

    assert effective_provider(SimpleNamespace(openai_api_key="sk-openai")) == "openrouter"
    assert effective_provider(SimpleNamespace(openrouter_api_key=None)) == "openrouter"
