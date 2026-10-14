#!/usr/bin/env bash
# True E2E: живой бот на тестовом DC Telegram против пользователя Telethon.
# Секреты - в .env.e2e рядом с репозиторием или в файле из E2E_ENV_FILE
# (см. tests_e2e/README.md); без них набор пропускается.
#
# Ничего не пишет в рабочую копию и не трогает её .venv: на сервере это
# каталог прод-бота (/opt/tg-bot), и он чужой. Свой venv - вне репозитория,
# проект не ставится (код берётся из src через PYTHONPATH).
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_PROJECT_ENVIRONMENT="${E2E_VENV:-$HOME/.cache/tg-bot-e2e/venv}"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1  # без __pycache__ в чужом каталоге
uv sync --frozen --no-install-project --extra dev --extra e2e --quiet
exec uv run --no-sync pytest tests_e2e --timeout=300 -p no:cacheprovider -v "$@"
