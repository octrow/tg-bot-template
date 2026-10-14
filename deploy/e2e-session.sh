#!/usr/bin/env bash
# Однократный вход пользователя Telethon для True E2E: дописывает E2E_SESSION
# в .env.e2e (режим и api_id/api_hash берутся оттуда же). Запускать в своём
# терминале - спросит номер, код из приложения Telegram и, если есть, пароль 2FA.
set -euo pipefail
cd "$(dirname "$0")/.."
# Секреты: E2E_ENV_FILE, иначе .env.e2e в репозитории, иначе (сервер, где
# каталог чужой) ~/.config/tg-bot-e2e.env.
env_file="${E2E_ENV_FILE:-}"
if [ -z "$env_file" ]; then
    if [ -f .env.e2e ]; then env_file=.env.e2e; else env_file="$HOME/.config/tg-bot-e2e.env"; fi
fi
if [ ! -f "$env_file" ]; then
    echo "нет файла секретов $env_file: скопируйте его с рабочей машины (deploy/e2e-env-copy.sh)" >&2
    echo "или создайте с E2E_API_ID, E2E_API_HASH, E2E_BOT_TOKEN, E2E_BOT_USERNAME, E2E_TELEGRAM_ENV" >&2
    exit 1
fi
get() { grep -E "^$1=" "$env_file" | tail -1 | cut -d= -f2-; }
api_id="$(get E2E_API_ID || true)"; api_hash="$(get E2E_API_HASH || true)"
if [ -z "$api_id" ] || [ -z "$api_hash" ]; then
    echo "в $env_file нет E2E_API_ID / E2E_API_HASH (my.telegram.org)" >&2
    exit 1
fi
# Обычный Telegram - вход по QR: код в чат «Telegram» сторонним приложениям
# бывает «отправлен», но не доходит. Тестовый DC - кодом (X пять раз).
where="--dc 2"; [ "$(get E2E_TELEGRAM_ENV || true)" = "prod" ] && where="--prod --qr"
export UV_PROJECT_ENVIRONMENT="${E2E_VENV:-$HOME/.cache/tg-bot-e2e/venv}"
uv sync --frozen --no-install-project --extra e2e --quiet
# Аргументы скрипта уходят дальше: `deploy/e2e-session.sh --qr` - вход по QR.
uv run --no-sync python tests_e2e/make_session.py --api-id "$api_id" --api-hash "$api_hash" $where "$@" >> "$env_file"
echo "E2E_SESSION дописан в $env_file" >&2
