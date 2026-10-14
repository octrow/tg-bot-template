"""Сравнение ответов Справки "до/после" смены стиля (спека chat-text-formatting).

Берёт последние вопросы Справки из журнала, задаёт каждый модели дважды - со
старым промптом Справки (шаблон и блок стиля из git) и с текущим - и пишет
markdown-отчёт с парами ответов и простыми проверками: длина абзацев, пути и
расширения файлов, строки подряд (спека chat-text-formatting). Ответ
показывается так, как его видит пользователь: после `tidy_model_text`.

Тратит токены: 2 вызова модели на вопрос. Журнал и кэш не трогает - работает
на копии базы, кэш агента выключен. Клон репозитория (`TGBOT_REPO_URL`) обновляется как у бота
(`ensure_repo_async`). Базу копируйте вместе с `-wal`, если она в режиме WAL,
иначе последние строки журнала не попадут.

Запуск (из корня репозитория, с рабочим .env):
    .venv/bin/python extra_scripts/compare_reference_style.py \
        --limit 15 --old-ref HEAD --out logs/compare_reference_style.md

Живые вопросы - в базе прода: скопируйте её локально и передайте `--db`.
Без журнала вопросы можно задать руками: `--question "..." --question "..."`.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tgbot import db, reference_mode, utils  # noqa: E402
from tgbot.agent import ladder  # noqa: E402
from tgbot.config import load_config  # noqa: E402
from tgbot.locales import en, ru  # noqa: E402
from tgbot.repo import ensure_repo_async  # noqa: E402

OLD_STYLE_KEYS = ("PROMPT_CHAT_STYLE", "PROMPT_REFERENCE_STYLE")
LOCALES = {"ru": ru.STRINGS, "en": en.STRINGS}
# Имя файла с расширением или путь через `/` (только латиница: "и/или" не путь).
# ponytail: регулярка грубая, ссылку тоже поймает; для отчёта глазами хватает.
FILE_RE = re.compile(
    r"\b[\w-]+\.(?:py|md|json|ya?ml|toml|sql|txt|log|js|ts|sh)\b|\b[A-Za-z0-9_]+/[A-Za-z0-9_./-]+"
)


def old_strings(ref: str, lang: str) -> dict:
    """Каталог локали на ревизии `ref`: файл исполняется в пустом
    пространстве имён, импортов у локалей нет."""
    source = subprocess.run(
        ["git", "show", f"{ref}:src/tgbot/locales/{lang}.py"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout
    namespace: dict = {}
    exec(compile(source, f"{ref}:{lang}.py", "exec"), namespace)  # noqa: S102 - свой код из git
    return namespace["STRINGS"]


def old_prompt(strings: dict, question: str, lang: str) -> str:
    """Промпт Справки целиком со старой ревизии: и шаблон PROMPT_REFERENCE,
    и блок стиля. Подставляется то же, что в `reference_mode.prompt`."""
    style = next(strings[key] for key in OLD_STYLE_KEYS if key in strings)
    return strings["PROMPT_REFERENCE"].format(
        question=question,
        history=reference_mode._history_block([], lang),
        howto=reference_mode._howto_block(reference_mode.read_howto(), lang),
        style=style,
    )


def questions(conn, limit: int) -> list[str]:
    seen: list[str] = []
    rows = conn.execute(
        "SELECT payload FROM log WHERE kind = 'reference_question' ORDER BY id DESC"
    )
    for (payload,) in rows:
        text = (json.loads(payload).get("text") or "").strip()
        if text and text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return seen


def checks(shown: str, raw: str) -> dict:
    """Проверки текста, который видит пользователь (после `tidy_model_text`).
    Тире и эмодзи не считаем: их убирает код, а не промпт."""
    paragraphs = [p for p in shown.split("\n\n") if p.strip()]
    return {
        "paragraphs": len(paragraphs),
        "longest_paragraph": max((len(p) for p in paragraphs), default=0),
        "split_by_code": shown != utils.plain_typography(raw),
        "paths": FILE_RE.findall(shown),
        "longest_block": utils.longest_block(shown),
        "block_over_limit": utils.longest_block(shown) > utils.BLOCK_MAX_LINES,
    }


def _section(title: str, answer: str | None, lang: str) -> list[str]:
    if answer is None:
        return [f"**{title}:** модель не ответила", ""]
    raw, _ = reference_mode.split_offer(str(answer))
    shown = utils.tidy_model_text(raw, lang)
    return [f"**{title}:** `{json.dumps(checks(shown, raw), ensure_ascii=False)}`", "", shown, ""]


async def run(args) -> None:
    """Отчёт перезаписывается после каждого вопроса: прерванный прогон
    оставляет готовые пары."""
    config = load_config()
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "tgbot.db"
        shutil.copy(args.db, copy)
        config = dataclasses.replace(config, db_path=copy, agent_cache_ttl_s=0)
        conn = db.connect(copy)
        await ensure_repo_async(config, conn)
        before = old_strings(args.old_ref, args.lang)
        lines = [f"# Справка: стиль до ({args.old_ref}) и после", ""]
        asked = args.question or questions(conn, args.limit)
        if not asked:
            raise SystemExit("В журнале нет вопросов Справки: передайте --db или --question")
        for number, question in enumerate(asked, 1):
            prompt = reference_mode.prompt(question, [], lang=args.lang)
            answers = [
                await ladder.call_agent(
                    config, conn, p, 0, reference_mode.REFERENCE_CONTEXT,
                    lang=args.lang, notify=False,
                )
                for p in (old_prompt(before, question, args.lang), prompt)
            ]
            lines += [f"## {number}. {question}", ""]
            lines += _section("До", answers[0], args.lang)
            lines += _section("После", answers[1], args.lang)
            Path(args.out).write_text("\n".join(lines), encoding="utf-8")
            print(f"[{number}/{len(asked)}] {question}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=str(ROOT / "tgbot.db"))
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--question", action="append", help="вопрос вместо журнала, можно несколько")
    parser.add_argument("--old-ref", default="HEAD")
    parser.add_argument("--lang", choices=sorted(LOCALES), default="ru")
    parser.add_argument("--out", default=str(ROOT / "logs" / "compare_reference_style.md"))
    args = parser.parse_args()
    asyncio.run(run(args))
    print(f"Отчёт: {args.out}")


if __name__ == "__main__":
    main()
