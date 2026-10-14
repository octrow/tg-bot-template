"""Запуск и простые команды через настоящий `build_application`."""

from .conftest import ADMIN, ALLOWED


async def test_start_and_stop_cleanly(bot):
    """Фикстура поднимает бота и гасит его - базовая проверка слоя."""
    assert bot.tg.calls_of("getMe")
    assert bot.tg.calls_of("deleteWebhook")


async def test_post_init_registers_command_menus(bot):
    # Три уровня (clearer-bot-output): ограниченное по умолчанию (ru, en),
    # рабочее допущенному, рабочее плюс админское админу.
    calls = await bot.wait("setMyCommands", count=4)
    scopes = [c.params.get("scope") for c in calls]
    langs = [c.params.get("language_code") for c in calls]
    assert scopes[:2] == [None, None] and langs[:2] == [None, "en"]
    assert scopes[2] == {"type": "chat", "chat_id": ALLOWED}
    assert scopes[3] == {"type": "chat", "chat_id": ADMIN}
