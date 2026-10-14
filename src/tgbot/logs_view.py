"""Рендер файла-зеркала журнала (`.jsonl`, спека `file-logging`) в читаемый
`.md` для команды `/logs` (спека `admin-log-access`).

Чтение и сборка вызываются через `asyncio.to_thread` - обе функции здесь
синхронные и не трогают `conn`: соединение с БД однопоточное (design
`enable-concurrent-sessions`, решение 4), а этот модуль работает только с
файлами.
"""

import json
import re
from pathlib import Path

from . import i18n

# Лимит Bot API на документ (design.md, Context). Часть журнала больше этого
# режется решением 7 - без урезания содержимого.
TELEGRAM_DOCUMENT_LIMIT_BYTES = 50 * 1024 * 1024


def _fence_for(content: str) -> str:
    """Ограда для блока кода длиннее любой цепочки обратных кавычек внутри
    `content` - иначе payload с собственными ``` (промпт, где кто-то уже
    цитировал markdown) преждевременно закрыл бы блок и сломал вывод."""
    longest = max((len(m.group(0)) for m in re.finditer("`+", content)), default=0)
    return "`" * max(3, longest + 1)


def render_log_file(path: Path, lang: str) -> str:
    """Собирает `.md` из `.jsonl`: на событие - заголовок (время и вид), под
    ним - payload целиком, JSON-блоком кода. Блок кода - не косметика, а
    защита (design.md, Risks): тексты промптов и ответов LLM внутри payload
    могут содержать markdown-разметку, которая иначе сломала бы вывод.

    Устойчиво к двум вещам: битой последней строке (файл дописывается на
    ходу - `journal.mirror` может оборвать запись посередине) и неизвестным
    видам событий - оба просто не должны ронять сборку. Строка, которую не
    удалось разобрать как JSON, тихо пропускается: это тот же обрыв записи,
    а не повреждение файла, разбирать который стоило бы отдельно.

    `lang` держит сигнатуру симметричной с `split_for_telegram` (обе функции
    вызываются вместе одним потоком в `bot._render_parts`); сама сборка здесь
    языка не показывает - в JSON payload нет строк каталога.
    """
    text = path.read_text(encoding="utf-8")
    parts = [f"# {path.name}", ""]
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = record.get("ts", "?")
        kind = record.get("kind", "?")
        payload = record.get("payload", {})
        payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
        fence = _fence_for(payload_json)
        parts.append(f"## {ts} - {kind}")
        parts.append("")
        parts.append(f"{fence}json")
        parts.append(payload_json)
        parts.append(fence)
        parts.append("")
    return "\n".join(parts)


def _chunk_by_bytes(text: str, max_bytes: int) -> list[str]:
    """Режет `text` на куски не длиннее `max_bytes` в UTF-8, не разрывая
    многобайтовый символ пополам."""
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for ch in text:
        ch_bytes = len(ch.encode("utf-8"))
        if current and current_bytes + ch_bytes > max_bytes:
            chunks.append("".join(current))
            current = []
            current_bytes = 0
        current.append(ch)
        current_bytes += ch_bytes
    if current:
        chunks.append("".join(current))
    return chunks or [""]


def split_for_telegram(
    text: str, lang: str, limit_bytes: int = TELEGRAM_DOCUMENT_LIMIT_BYTES
) -> list[str]:
    """Режет `text` на части, каждая из которых укладывается в `limit_bytes`
    (Bot API принимает документ до 50 МБ). Если текст целиком помещается -
    возвращает его одной частью без пометки.

    Иначе части помечаются номером (design.md, решение 7: "режем на части и
    отправляем все, помечая номер части" - урезание не вариант, спека требует
    полного содержимого). Число частей заранее неизвестно - оно само влияет
    на длину пометки, поэтому подбирается: пробуем очередное число частей,
    пока пометка и соответствующий ей размер куска не сойдутся.
    """
    if len(text.encode("utf-8")) <= limit_bytes:
        return [text]

    label_template = i18n.texts(lang).LOGS_PART_LABEL
    n = 2
    while True:
        label_bytes = len(label_template.format(i=n, n=n).encode("utf-8"))
        chunk_bytes = limit_bytes - label_bytes
        if chunk_bytes <= 0:
            n += 1
            continue
        chunks = _chunk_by_bytes(text, chunk_bytes)
        if len(chunks) <= n:
            total = len(chunks)
            return [
                label_template.format(i=i, n=total) + chunk
                for i, chunk in enumerate(chunks, start=1)
            ]
        n += 1
