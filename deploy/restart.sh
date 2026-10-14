#!/usr/bin/env bash
# Локальный перезапуск бота одной командой (только для разработки - прод
# перезапускается через systemd/deploy/update.sh). Останавливает предыдущий
# процесс, запущенный этим же скриптом (по pid-файлу), запускает
# `python -m tgbot` в фоне и показывает хвост его консольного лога.
# Без этого второй getUpdates с тем же токеном сталкивается с первым
# (telegram.error.Conflict).
#
# Использование: bash deploy/restart.sh
set -euo pipefail

APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$APP_DIR"

PID_FILE=${TGBOT_PID_FILE:-"$APP_DIR/logs/tg-bot.pid"}
RUN_LOG=${TGBOT_RUN_LOG:-"$APP_DIR/logs/restart.log"}
PYTHON=${TGBOT_PYTHON:-"$APP_DIR/.venv/bin/python"}

mkdir -p "$(dirname "$PID_FILE")"

# Остановить предыдущий процесс, если он ещё жив.
if [ -f "$PID_FILE" ]; then
    old_pid=$(cat "$PID_FILE")
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
        echo "Останавливаю предыдущий процесс (pid $old_pid)..."
        kill "$old_pid" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$old_pid" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "$old_pid" 2>/dev/null; then
            echo "Процесс не остановился за 10с, kill -9" >&2
            kill -9 "$old_pid" 2>/dev/null || true
        fi
    fi
    rm -f "$PID_FILE"
fi

echo "Запускаю: $PYTHON -m tgbot"
nohup "$PYTHON" -m tgbot >>"$RUN_LOG" 2>&1 &
new_pid=$!
echo "$new_pid" >"$PID_FILE"
echo "Запущен (pid $new_pid), лог: $RUN_LOG"

# Дать процессу время либо подняться, либо упасть на файловой блокировке/
# конфиге (__main__.main() завершается быстро при ошибке).
sleep 1
if ! kill -0 "$new_pid" 2>/dev/null; then
    echo "Процесс не поднялся, хвост лога:" >&2
    tail -n 40 "$RUN_LOG" >&2
    rm -f "$PID_FILE"
    exit 1
fi

echo "--- хвост лога ($RUN_LOG) ---"
tail -n 20 "$RUN_LOG"
