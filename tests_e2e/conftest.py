"""True E2E: настоящий `python -m tgbot` на тестовом DC Telegram,
сценарии ведёт пользователь Telethon (design, решения 5-6).

Без секретов набор пропускается целиком; с прод-токеном - отказывается
запускаться. LLM - тот же фейк, что в слое Bot API: бот в
подпроцессе ходит к нему по loopback."""

import asyncio
import os
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from tgbot.config import ENV_FILES, _parse_env_file

from tests.fakes.bot_process import bot_env, start_bot, stop_bot, tail
from tests.fakes.llm_api import FakeLLM

REPO = Path(__file__).resolve().parents[1]
REQUIRED = ("E2E_API_ID", "E2E_API_HASH", "E2E_SESSION", "E2E_BOT_TOKEN", "E2E_BOT_USERNAME")
# Где живёт тестовый бот: `test` - тестовый DC (отдельный аккаунт и бот),
# `prod` - обычный Telegram, отдельный бот у обычного @BotFather и запасной
# аккаунт (core.telegram.org/bots/features#testing-your-bot).
API_BASE_URLS = {
    "test": "https://api.telegram.org/bot{token}/test",
    "prod": "https://api.telegram.org/bot{token}",
}
DEFAULT_TELEGRAM_ENV = "test"


def api_base_url(env: dict[str, str]) -> str:
    name = (env.get("E2E_TELEGRAM_ENV") or DEFAULT_TELEGRAM_ENV).strip().lower()
    if name not in API_BASE_URLS:
        pytest.exit(f"E2E_TELEGRAM_ENV={name!r}: ожидалось test или prod", 2)
    return API_BASE_URLS[name]
REPLY_TIMEOUT_S = 20.0


def e2e_env() -> dict[str, str]:
    """Файл секретов (`E2E_ENV_FILE`, по умолчанию `.env.e2e` в корне
    репозитория), окружение процесса сильнее. Свой путь нужен на сервере:
    каталог прод-бота чужой, класть туда файл нельзя."""
    return {**_parse_env_file(secrets_path()), **os.environ}


def secrets_path() -> Path:
    """E2E_ENV_FILE, иначе `.env.e2e` в репозитории, иначе (сервер) `~/.config`."""
    if os.environ.get("E2E_ENV_FILE"):
        return Path(os.environ["E2E_ENV_FILE"]).expanduser()
    local = REPO / ".env.e2e"
    return local if local.exists() else Path("~/.config/tg-bot-e2e.env").expanduser()


def prod_tokens() -> set[str]:
    """Прод-токены, которые набор не имеет права использовать."""
    tokens = {os.environ.get("TELEGRAM_BOT_TOKEN", "")}
    for raw in ENV_FILES:
        tokens.add(_parse_env_file(Path(raw).expanduser()).get("TELEGRAM_BOT_TOKEN", ""))
    return {t.strip() for t in tokens if t.strip()}


def _bot_id(token: str) -> str:
    return token.strip().split(":", 1)[0]


def pytest_collection_modifyitems(config, items):
    # Хук конфтеста видит весь сбор - трогаем только сценарии этого каталога.
    here = Path(__file__).parent
    items = [item for item in items if here in item.path.parents]
    if not items:
        return
    env = e2e_env()
    missing = [name for name in REQUIRED if not env.get(name, "").strip()]
    if missing:
        skip = pytest.mark.skip(reason=f"нет переменных True E2E: {', '.join(missing)}")
        for item in items:
            item.add_marker(skip)
        return
    # По id бота, а не только по строке: перевыпущенный токен - тот же прод-бот.
    if _bot_id(env["E2E_BOT_TOKEN"]) in {_bot_id(t) for t in prod_tokens()}:
        pytest.exit("E2E_BOT_TOKEN совпадает с прод-токеном TELEGRAM_BOT_TOKEN - отказ", 2)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    # FloodWait бьёт по аккаунту целиком: следующие сценарии упадут так же,
    # а повторы только продлят паузу - останавливаем прогон с понятной причиной.
    if call.excinfo is not None and type(call.excinfo.value).__name__ == "FloodWaitError":
        item.session.shouldstop = (
            f"Telegram FloodWait {call.excinfo.value.seconds} с на аккаунте E2E - "
            "повторите прогон после паузы"
        )
    return report


@pytest.fixture(scope="session")
def env() -> dict[str, str]:
    return e2e_env()


@pytest.fixture(scope="session")
def fakes():
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    llm = FakeLLM().start()
    yield llm
    llm.stop()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def user(env):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    # Без `set_dc`: DC, адрес и ключ уже в строке сессии (make_session.py).
    client = TelegramClient(
        StringSession(env["E2E_SESSION"]), int(env["E2E_API_ID"]), env["E2E_API_HASH"],
        lang_code="ru", system_lang_code="ru",
    )
    await client.connect()
    if not await client.is_user_authorized():
        pytest.exit("E2E_SESSION не авторизована: заново tests_e2e/make_session.py", 2)
    yield client
    await client.disconnect()


def _drop_pending_updates(base_url: str, token: str) -> None:
    """Апдейты прошлого прогона не должны доехать до этого."""
    url = base_url.format(token=token) + "/deleteWebhook"
    resp = httpx.post(url, data={"drop_pending_updates": "true"}, timeout=30)
    if resp.status_code != 200:
        # Без URL: в нём токен, а текст ошибки pytest печатает целиком.
        pytest.fail(f"deleteWebhook на тестовом DC: HTTP {resp.status_code} {resp.text[:200]}")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def bot_process(env, fakes, user, tmp_path_factory):
    workdir = tmp_path_factory.mktemp("e2e-bot")
    llm = fakes
    me = await user.get_me()
    base_url = api_base_url(env)
    _drop_pending_updates(base_url, env["E2E_BOT_TOKEN"])
    bot_env_vars = bot_env(
        workdir, token=env["E2E_BOT_TOKEN"], api_base_url=base_url,
        llm_url=llm.base_url, admin_id=me.id,
    )
    try:
        proc, log_path = await asyncio.to_thread(start_bot, workdir, bot_env_vars)
    except RuntimeError as exc:
        pytest.fail(str(exc))
    yield proc
    stop_bot(proc)
    print(f"\n--- хвост журнала бота ({log_path}) ---\n{tail(log_path)}")


@pytest.fixture(scope="session")
def bot_username(env) -> str:
    return env["E2E_BOT_USERNAME"].strip()
