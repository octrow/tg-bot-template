"""Журнал процесса: маска секретов, контекст апдейта, уровень из окружения.

Запуск: uv run pytest tests/test_logging_setup.py -q
"""

import asyncio
import logging
from types import SimpleNamespace

from tgbot import logging_setup

TOKEN = "123456789:AAH" + "x" * 32


def _format(msg, *args, exc_info=None) -> str:
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, msg, args, exc_info)
    return logging_setup.Formatter(logging_setup.FORMAT).format(record)


def test_a_bot_token_is_masked_in_message_and_traceback():
    try:
        raise RuntimeError(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage")
    except RuntimeError:
        import sys
        line = _format("url %s", f"bot{TOKEN}", exc_info=sys.exc_info())
    assert TOKEN not in line
    assert logging_setup.MASK in line


def test_an_openrouter_key_is_masked_by_its_shape():
    key = "sk-or-v1-" + "a1" * 20
    assert key not in _format("401 for key %s", key)


def test_a_registered_secret_is_masked_whatever_its_shape(monkeypatch):
    monkeypatch.setattr(logging_setup, "_secrets", set())
    logging_setup.mask_secrets("typesafe-key-42abc", None, "short")
    line = _format("ключ %s и short", "typesafe-key-42abc")
    assert "typesafe-key-42abc" not in line
    assert "short" in line  # короткое значение не маскируется


def test_each_update_carries_its_own_user_in_the_log_line():
    async def handle(user_id, update_id):
        update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id), update_id=update_id)
        await logging_setup.bind_update(update, None)
        await asyncio.sleep(0)
        return _format("шаг")

    async def both():
        return await asyncio.gather(
            asyncio.create_task(handle(1, 10)), asyncio.create_task(handle(2, 20))
        )

    first, second = asyncio.run(both())
    assert "[u=1 upd=10]" in first
    assert "[u=2 upd=20]" in second
    assert f"[{logging_setup.NO_CONTEXT}]" in _format("вне апдейта")


def test_level_comes_from_the_environment_and_a_typo_falls_back(caplog):
    root = logging.getLogger()
    saved = root.level, list(root.handlers)
    try:
        root.handlers.clear()  # basicConfig настраивает только пустой корень
        logging_setup.setup({logging_setup.LEVEL_ENV: "debug"})
        assert root.level == logging.DEBUG
        root.handlers.clear()
        logging_setup.setup({logging_setup.LEVEL_ENV: "verbose"})
        assert root.level == logging.INFO
    finally:
        root.setLevel(saved[0])
        root.handlers[:] = saved[1]
