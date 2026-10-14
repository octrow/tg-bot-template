"""Бот настоящим процессом `python -m tgbot`: общий для смоук-теста
против фейкового Telegram и для True E2E против тестового DC."""

import os
import subprocess
import sys
import time
from pathlib import Path

from tgbot.config import ENV_FILES, _parse_env_file

REPO = Path(__file__).resolve().parents[2]
READY_LINE = "Application started"  # PTB пишет его, когда polling запущен
START_TIMEOUT_S = 60.0
LOG_TAIL_LINES = 60


def bot_env(workdir: Path, *, token: str, api_base_url: str, llm_url: str,
            admin_id: int) -> dict[str, str]:
    """Окружение бота-подпроцесса: всё своё, ничего от прода.

    Пустые значения ключей перебивают `/opt/tg-bot/.env` и `~/.env`
    (окружение сильнее файлов), `HOME` - временный каталог. Остальные ключи
    файлов окружения (модель, ступень...) тоже гасятся пустым
    значением - конфиг берёт для них дефолты, а не прод."""
    from_files = {
        key: "" for raw in ENV_FILES for key in _parse_env_file(Path(raw).expanduser())
    }
    return {
        **from_files,
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(workdir),
        "PYTHONPATH": str(REPO / "src"),
        "NO_PROXY": "127.0.0.1,localhost",
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_BOT_API_BASE_URL": api_base_url,
        "TGBOT_LLM_BASE_URL": llm_url,
        "OPENROUTER_API_KEY": "sk-or-fake",
        "OPENAI_API_KEY": "",
        "TYPESAFE_API_KEY": "",
        "TGBOT_REPO_URL": "",
        "ADMIN_USER_IDS": str(admin_id),
        "TGBOT_DB_PATH": str(workdir / "bot.db"),
        "TGBOT_LOCK_PATH": str(workdir / "bot.lock"),
        "TGBOT_LOGS_DIR": str(workdir / "logs"),
        "TGBOT_TEXT_DEBOUNCE_S": "0",
        "TGBOT_AGENT_CACHE_TTL_S": "0",
        "TGBOT_LOG_LEVEL": "INFO",
    }


def tail(path: Path) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-LOG_TAIL_LINES:])


def start_bot(workdir: Path, env: dict[str, str]) -> tuple[subprocess.Popen, Path]:
    """Запускает бота и ждёт строки готовности; при сбое - хвост журнала."""
    log_path = workdir / "bot.log"
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "tgbot"], cwd=workdir, env=env,
            stdout=log, stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + START_TIMEOUT_S
    while READY_LINE not in log_path.read_text(encoding="utf-8", errors="replace"):
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            proc.wait()
            raise RuntimeError(f"бот не стартовал (код {proc.returncode}):\n{tail(log_path)}")
        time.sleep(0.2)
    return proc, log_path


def stop_bot(proc: subprocess.Popen) -> int:
    proc.terminate()
    try:
        return proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait()
