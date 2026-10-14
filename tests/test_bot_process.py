"""Смоук `python -m tgbot` целиком: настоящий процесс против фейкового
Telegram. Проверяет то, чего не видит слой Bot API в процессе pytest: разбор
окружения, лок, `run_polling` и штатную остановку по SIGTERM."""

import time

from tgbot import i18n

from .fakes.bot_process import bot_env, start_bot, stop_bot
from .fakes.llm_api import FakeLLM
from .fakes.telegram_api import FakeTelegram

ADMIN = 1001


def test_bot_process_answers_start_and_stops_on_sigterm(tmp_path):
    tg, llm = FakeTelegram().start(), FakeLLM().start()
    try:
        env = bot_env(tmp_path, token="123:abc", api_base_url=tg.base_url,
                      llm_url=llm.base_url, admin_id=ADMIN)
        proc, log_path = start_bot(tmp_path, env)
        tg.push_update({"message": {
            "message_id": 1, "date": int(time.time()),
            "chat": {"id": ADMIN, "type": "private"},
            "from": {"id": ADMIN, "is_bot": False, "first_name": "A", "language_code": "ru"},
            "text": "/start", "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        }})
        reply = tg.wait_for("sendMessage", lambda p: p.get("chat_id") == ADMIN, timeout=15)[0]
        assert i18n.texts("ru").START in reply.params["text"]
        tg.release()
        assert stop_bot(proc) == 0, log_path.read_text()
    finally:
        for fake in (tg, llm):
            fake.stop()
