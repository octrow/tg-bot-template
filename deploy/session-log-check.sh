#!/usr/bin/env bash
# Ручная проверка session-log-completeness на своём тестовом боте (не прод-боте).
# Токен тестового бота - TEST_TELEGRAM_BOT_TOKEN в .env, его @username - в окружении,
# TEST_BOT_USERNAME (необязательно, только для подсказки).
#
#   deploy/session-log-check.sh run     # бот локально под TEST_TELEGRAM_BOT_TOKEN, Ctrl+C - стоп
#   deploy/session-log-check.sh check   # разбор последнего файла сессии Справки
#   deploy/session-log-check.sh route   # путь (chat/full/chat_fallback) и язык каждого ответа
#   CLONE_FROM=../product  deploy/session-log-check.sh run   # + клон репозитория продукта
#                                             # (TGBOT_REPO_URL): ответы по коду, а не только по docs
#
# Чистые БД и логи в $CHECK_DIR (по умолчанию /tmp/tg-bot-session-check):
# подсказка о языке не была показана, язык по умолчанию - русский. Прод-токен
# и рабочая tgbot.db не трогаются. Запускать из корня репозитория.
set -euo pipefail

CHECK_DIR="${CHECK_DIR:-/tmp/tg-bot-session-check}"
cmd="${1:-}"

case "$cmd" in
run)
    token="$(grep -E '^TEST_TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2- | tr -d '"'"'"' ')"
    [ -n "$token" ] || { echo "в .env нет TEST_TELEGRAM_BOT_TOKEN" >&2; exit 1; }
    rm -rf "$CHECK_DIR" && mkdir -p "$CHECK_DIR"
    if [ -n "${CLONE_FROM:-}" ]; then
        # Бот сам склонирует его в $CHECK_DIR/repo - рядом с базой.
        export TGBOT_REPO_URL="file://$(realpath "$CLONE_FROM")"
    fi
    echo "БД и логи: $CHECK_DIR. Пишите тестовому боту ${TEST_BOT_USERNAME:-(TEST_BOT_USERNAME не задан)}"
    TELEGRAM_BOT_TOKEN="$token" \
    TGBOT_DB_PATH="$CHECK_DIR/tgbot.db" \
    TGBOT_LOGS_DIR="$CHECK_DIR/logs" \
        exec .venv/bin/python -m tgbot
    ;;
check)
    exec python3 - "$CHECK_DIR/logs" <<'PY'
import collections, json, pathlib, sys

logs = pathlib.Path(sys.argv[1])
files = sorted(p for p in logs.glob("*.jsonl") if p.name != "bot.jsonl")
if not files:
    sys.exit(f"в {logs} нет файлов сессий: сначала run и вопрос боту")
path = files[-1]
records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
kinds = [r["kind"] for r in records]
print(f"файл: {path.name}, событий: {len(records)}")

problems = []
want = ["tg_update", "language_offer", "reference_question", "reference_answer"]
missing = [k for k in want if k not in kinds]
if missing:
    problems.append(f"нет событий: {missing}")
else:
    idx = [kinds.index(k) for k in want]
    if idx != sorted(idx):
        problems.append(f"порядок нарушен: {list(zip(want, idx))}")

reactions = collections.Counter(
    r["payload"]["params"].get("message_id")
    for r in records
    if r["kind"] == "tg_call" and r["payload"].get("method") == "setMessageReaction"
)
print(f"реакций по сообщениям: {dict(reactions)}")
twice = [m for m, n in reactions.items() if n > 1]
if twice:
    problems.append(f"реакция дважды на сообщения {twice}")

common = logs / "bot.jsonl"
if common.exists() and '"language_offer"' in common.read_text(encoding="utf-8"):
    problems.append("language_offer ушёл в общий bot.jsonl")
sessions = len(files)
print(f"файлов сессий: {sessions}")
if problems:
    sys.exit("НЕ ОК:\n  " + "\n  ".join(problems))
print("ОК")
PY
    ;;
route)
    exec python3 - "$CHECK_DIR/logs" <<'PY'
import json, pathlib, sys

logs = pathlib.Path(sys.argv[1])
for path in sorted(p for p in logs.glob("*.jsonl") if p.name != "bot.jsonl"):
    print(f"== {path.name}")
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        kind, payload = r["kind"], r.get("payload") or {}
        if kind == "reference_question":
            print(f"вопрос:  {payload.get('text')!r}")
        elif kind == "reference_intent":
            print(f"  jev:   {payload.get('outcome')} {payload.get('probabilities')}")
        elif kind == "reference_route":
            print(f"  путь:  {payload.get('route')}")
        elif kind == "llm_request":
            prompt = payload.get("prompt") or ""
            lang = "en" if "(en)" in prompt else "ru" if "(ru)" in prompt else "?"
            print(f"  промпт: язык {lang}")
        elif kind == "reference_answer":
            first = (payload.get("text") or "").strip().splitlines()[:1]
            print(f"  ответ: {first[0][:100] if first else ''!r}")
PY
    ;;
*)
    echo "использование: $0 run|check|route" >&2
    exit 2
    ;;
esac
