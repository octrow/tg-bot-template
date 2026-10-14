import errno
import fcntl
import logging
import os
import sys
from pathlib import Path

from . import __version__, db, logging_setup, repo
from .agent import ladder, stages
from .bot import build_application
from .config import ConfigError, load_config

log = logging.getLogger(__name__)


def _lock_path(db_path: Path) -> Path:
    """Путь файла блокировки: из env, иначе рядом с БД (`<db>.lock`)."""
    raw = os.environ.get("TGBOT_LOCK_PATH", "").strip()
    return Path(raw) if raw else db_path.with_suffix(".lock")


def _acquire_lock(path: Path):
    """Эксклюзивная файловая блокировка процесса (`fcntl.flock`, stdlib).

    Не даёт двум экземплярам бота одновременно опрашивать Telegram
    `getUpdates` - второй `getUpdates` с тем же токеном обрывает первый
    `telegram.error.Conflict`. Возвращает открытый файл-дескриптор (держать
    открытым, пока бот работает - иначе лок освободится) или None, если лок
    уже занят другим процессом.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")  # noqa: SIM115 - дескриптор живёт, пока держим лок
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            fh.close()
            return None
        raise
    return fh


def _check_repo(config, conn) -> None:
    """Клон на старте, а не только на первом вопросе: без него агент отвечает без
    кода, и причина должна быть видна сразу. Любой исход - бот стартует."""
    if config.repo_path is None:
        return
    if repo.ensure_repo(config, conn):
        log.info("Клон репозитория готов: %s", config.repo_path)
    else:
        log.warning(
            "Клона репозитория нет (%s) - ответов по коду не будет; "
            "проверь доступ к репозиторию (deploy key или токен в URL).",
            config.repo_path,
        )


def _log_startup(config, conn) -> None:
    """Запись `startup` и строка в journal: версия, ступень и модели LLM."""
    effective = stages.effective_provider(config)
    # Вся лестница по порядку: основная модель меняется редко, и
    # без списка смену запасных по журналу не увидеть.
    models = [model for _, model in ladder.ladder_rungs(config)]
    db.log_event(
        conn, None, "startup",
        {
            "version": __version__,
            "db_path": str(config.db_path),
            "llm_provider": effective,
            "model": models[0],
            "models": models,
        },
    )
    log.info(
        "tg-bot %s, ступень LLM: %s, модели: %s",
        __version__, effective, ", ".join(models),
    )
    if not getattr(config, stages.STAGES[effective]["key_attr"], None):
        log.warning(
            "У ступени LLM нет ключа: бот работает, агентские шаги "
            "отключены. Задайте %s.", stages.STAGES[effective]["key_attr"].upper()
        )


def main() -> int:
    logging_setup.setup()
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"tg-bot: {exc}", file=sys.stderr)
        return 1
    logging_setup.mask_secrets(
        config.telegram_bot_token, config.openrouter_api_key, config.omniroute_api_key,
        config.typesafe_api_key,
        *repo.url_secrets(config.repo_url),
    )

    lock_path = _lock_path(config.db_path)
    lock_fh = _acquire_lock(lock_path)
    if lock_fh is None:
        print(
            f"tg-bot: бот уже запущен (лок {lock_path} занят). "
            "Останови предыдущий процесс или используй deploy/restart.sh.",
            file=sys.stderr,
        )
        return 1

    try:
        conn = db.connect(config.db_path)
        _log_startup(config, conn)
        _check_repo(config, conn)
        app = build_application(config, conn)
        app.run_polling()
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
    if app.bot_data.get("instance_conflict"):
        # Конфликт getUpdates завершает polling штатно, но для systemd это
        # сбой: ненулевой код заставит Restart= поднять бот заново.
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
