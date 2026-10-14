#!/usr/bin/env bash
# Копирует локальный .env.e2e на сервер в ~/.config/tg-bot-e2e.env
# (не в /opt/tg-bot/.env: тот - настройки прод-бота и читается только sudo).
# Запуск: deploy/e2e-env-copy.sh [user@host]
# Потом на сервере: E2E_ENV_FILE=~/.config/tg-bot-e2e.env /opt/tg-bot/deploy/e2e.sh
set -euo pipefail
cd "$(dirname "$0")/.."
target="${1:?укажите user@host сервера}"
[ -f .env.e2e ] || { echo "нет .env.e2e - см. tests_e2e/README.md" >&2; exit 1; }
ssh "$target" 'mkdir -p ~/.config && umask 077 && cat > ~/.config/tg-bot-e2e.env' < .env.e2e
echo "скопировано: $target:~/.config/tg-bot-e2e.env (права 600)"
