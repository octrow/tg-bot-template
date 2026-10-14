#!/usr/bin/env bash
# Быстрое обновление tg-bot из GitHub: git pull + синхронизация юнита +
# зависимости (если менялись) + рестарт + проверка, что сервис поднялся.
# Запуск: sudo /opt/tg-bot/deploy/update.sh
# Плейсхолдеры: каталог APP_DIR (/opt/tg-bot) и пользователь/юнит SERVICE (tg-bot) -
# см. deploy/DEPLOY.md.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/tg-bot}
SERVICE=tg-bot
UNIT=/etc/systemd/system/${SERVICE}.service

cd "$APP_DIR"

# Хеш до пула - по нему решаем, нужен ли pip install (а не "на глаз").
deps_before=$(sha256sum pyproject.toml)

# GITHUB_TOKEN (чтение репозитория бота) лежит в .deploy.env (root, 600), а не
# в .env: .env systemd грузит в окружение бота, и токен увидел бы агент. В git он
# идёт заголовком через GIT_CONFIG_* в окружении: не в argv (ps), не в .git/config.
# Без токена - прежний git pull (спросит логин/пароль).
token=$(sed -n 's/^GITHUB_TOKEN=//p' .deploy.env 2>/dev/null | tail -1)
if [ -n "$token" ]; then
    basic=$(printf 'x-access-token:%s' "$token" | base64 -w0)
    export GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=1 \
        GIT_CONFIG_KEY_0=http.https://github.com/.extraHeader \
        GIT_CONFIG_VALUE_0="Authorization: Basic $basic"
    unset basic
    sudo --preserve-env=GIT_TERMINAL_PROMPT,GIT_CONFIG_COUNT,GIT_CONFIG_KEY_0,GIT_CONFIG_VALUE_0 \
        -u "$SERVICE" git pull --ff-only
    unset GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0
else
    sudo -u "$SERVICE" git pull --ff-only
fi
unset token

# Юнит тоже приезжает из репозитория: без этого правки tg-bot.service
# (окружение, Restart, PrivateTmp) молча остаются только в git. Локальные
# правки на сервере будут затёрты - держите их в drop-in:
# /etc/systemd/system/tg-bot.service.d/local.conf
if ! cmp -s "deploy/${SERVICE}.service" "$UNIT"; then
    cp "deploy/${SERVICE}.service" "$UNIT"
    systemctl daemon-reload
    echo "Юнит обновлён из репозитория"
fi

# Пакет должен импортироваться из репозитория (editable), иначе git pull меняет
# исходники, а бот продолжает исполнять копию из site-packages - именно так
# несколько релизов подряд "выкатились", ничего не изменив.
installed=$(sudo -u "$SERVICE" .venv/bin/python -c \
    'import tgbot, os; print(os.path.dirname(tgbot.__file__))' 2>/dev/null || true)
if [ "$installed" != "$APP_DIR/src/tgbot" ]; then
    echo "Пакет установлен не из репозитория ($installed) - ставлю editable"
    sudo -u "$SERVICE" .venv/bin/pip install -e .
elif [ "$deps_before" != "$(sha256sum pyproject.toml)" ]; then
    sudo -u "$SERVICE" .venv/bin/pip install -e .
    echo "Зависимости переустановлены"
fi

systemctl restart "$SERVICE"
sleep 3
if ! systemctl is-active --quiet "$SERVICE"; then
    echo "Сервис не поднялся после обновления, последние строки журнала:" >&2
    journalctl -u "$SERVICE" -n 30 --no-pager >&2
    exit 1
fi

echo "Обновлено: $(sudo -u "$SERVICE" git log -1 --format='%h %s')"
