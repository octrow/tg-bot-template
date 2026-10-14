"""Инструменты чтения агента (read_file / grep / glob) по каталогам знаний и
клону репозитория, потолки расхода и стартовый обзор дерева."""

import asyncio
import itertools
import logging
import re
from pathlib import Path

from .. import i18n

log = logging.getLogger(__name__)

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Прочитать файл целиком.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "путь к файлу"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Найти регулярное выражение в файлах.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string", "description": "например **/*.py"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Список файлов по шаблону.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
]

# Стартовый обзор дерева: без него модель не знает, что рядом лежит клон с
# docs/, patches/ и кодом, и отвечает "подтверждения нет", не сделав ни одного
# поиска. Список неполный - остальное она дочитывает инструментами.
OVERVIEW_LIMIT = 40

# Потолки расхода. Каждый круг инструментов переотправляет всю историю, поэтому
# щедрый лимит на чтение растёт квадратично по деньгам. Числа подобраны так,
# чтобы типовой документ архива (медиана рабочего клона - около 18 000
# символов) дочитывался целиком, а общий бюджет допускал три таких чтения
# подряд (замер в исходном боте: 6 000/24 000 обрезали 176 из 178 документов архива).
MAX_TOOL_ROUNDS = 3
READ_MAX_CHARS = 20_000
MATCH_LIMIT = 40
# Общий бюджет на всё, что инструменты отдали модели за один запрос: как только
# он исчерпан, инструменты снимаются и модель отвечает по прочитанному.
TOOL_BUDGET_CHARS = 60_000
BUDGET_SPENT = "Бюджет чтения исчерпан - отвечай по уже прочитанному."

# Потолки grep. Регулярку пишет модель, а у `re` нет таймаута: катастрофический
# возврат на длинной строке держит рабочий поток сколько угодно.
# ponytail: потолок длины шаблона и объёма текста, а не таймаут - таймаут
# потребовал бы процесс или сторонний движок регулярок.
GREP_PATTERN_MAX = 200
# С файла читается только начало: дамп или лог на гигабайт иначе целиком ушёл бы
# в память ради одной строки совпадения.
GREP_FILE_MAX_BYTES = 1_000_000
# За один вызов grep: не больше файлов и байт, чем нужно поиску по документации.
GREP_MAX_FILES = 2_000
GREP_MAX_TOTAL_BYTES = 20_000_000
GREP_CUT = "[Поиск остановлен на потолке объёма - сузь glob]"


def _has_dot_segment(parts) -> bool:
    """Сегмент с точки: `..` (выход из корня) или служебное (`.git` с токеном
    клона в `.git/config`, `.env` с ключами, `.claude/`)."""
    return any(part.startswith(".") for part in parts)


def _confined(roots: list[Path], path: Path) -> Path | None:
    """Реальный путь файла, если он лежит под одним из корней и не проходит
    через точечный сегмент; иначе None.

    Единая проверка для read_file, grep и glob. Смотрим на путь ПОСЛЕ
    разрешения симлинков: симлинк внутри корня, ведущий наружу или в `.git`,
    отсекается так же, как прямой путь туда."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):  # петля симлинков, нет прав
        return None
    for root in roots:
        real_root = root.resolve()
        if resolved == real_root or real_root in resolved.parents:
            if _has_dot_segment(resolved.relative_to(real_root).parts):
                return None
            return resolved
    return None


def _resolve(roots: list[Path], raw: str) -> Path | None:
    """Путь внутри разрешённых каталогов либо None.

    Модель называет пути как хочет (относительно клона, относительно docs/,
    иногда с ../) - принимаем только то, что реально лежит под roots и не
    в служебном каталоге (`_confined`)."""
    candidates = [Path(raw)] + [root / raw for root in roots]
    for candidate in candidates:
        resolved = _confined(roots, candidate)
        if resolved is not None:
            return resolved
    return None


def _tool_read(roots: list[Path], args: dict) -> tuple[str, list[str]]:
    """(содержимое файла, прочитанные пути) - пути нужны журналу.

    Молчаливая обрезка выглядит для модели как весь файл - она не может
    отличить "дочитано" от "оборвано". Если файл длиннее потолка, к тексту
    дописывается пометка с числом отданных символов и полным размером файла
   ."""
    path = _resolve(roots, args.get("path", ""))
    if path is None or not path.is_file():
        return f"Файл не найден: {args.get('path')!r}", []
    text = path.read_text(encoding="utf-8", errors="replace")
    total = len(text)
    if total <= READ_MAX_CHARS:
        return text, [str(path)]
    shown = text[:READ_MAX_CHARS]
    notice = (
        f"\n\n[Показано {READ_MAX_CHARS} из {total} символов - файл обрезан, "
        "остальное можно дочитать через grep]"
    )
    return shown + notice, [str(path)]


def _iter_files(roots: list[Path], pattern: str):
    """Файлы под roots по шаблону модели.

    Шаблон приходит от LLM в любом виде - в том числе абсолютным путём, на
    котором `Path.glob` падает ("Non-relative patterns are unsupported").
    Приводим к относительному, а неудачный шаблон просто ничего не находит.
    Каждый найденный файл проходит `_confined`: `../`, симлинк наружу и
    точечные каталоги из выдачи выпадают.
    """
    for root in roots:
        relative = _as_relative(pattern, root)
        if relative is None:
            continue
        try:
            found = sorted(p for p in root.glob(relative) if p.is_file())
        except (ValueError, OSError) as exc:
            log.warning("Шаблон %r не подошёл к %s: %s", pattern, root, exc)
            continue
        yield from (p for p in found if _confined(roots, p) is not None)


def _as_relative(pattern: str, root: Path) -> str | None:
    """Шаблон относительно `root` либо None - по этому корню не ищем.

    Абсолютный путь режем по корню; абсолютный путь вне корня и шаблон с
    точечным сегментом (`..`, `.git`, `.*`) отклоняются сразу."""
    pattern = pattern.strip() or "**/*"
    if pattern.startswith("/"):
        try:
            pattern = str(Path(pattern).relative_to(root))
        except ValueError:  # путь не из этого корня
            return None
    if _has_dot_segment(Path(pattern).parts):
        return None
    return pattern


def _tool_glob(roots: list[Path], args: dict) -> str:
    found = [str(p) for p in _iter_files(roots, args.get("pattern") or "**/*")]
    return "\n".join(found[:MATCH_LIMIT]) or "Ничего не найдено"


def _tool_grep(roots: list[Path], args: dict) -> str:
    pattern = args.get("pattern", "")
    if len(pattern) > GREP_PATTERN_MAX:
        return f"Слишком длинное регулярное выражение: больше {GREP_PATTERN_MAX} символов"
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Плохое регулярное выражение: {exc}"
    hits: list[str] = []
    scanned = 0
    for count, path in enumerate(_iter_files(roots, args.get("glob") or "**/*")):
        if count >= GREP_MAX_FILES or scanned >= GREP_MAX_TOTAL_BYTES:
            hits.append(GREP_CUT)
            break
        text = _read_head(path)
        if text is None:
            continue
        scanned += len(text)
        for number, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hits.append(f"{path}:{number}: {line.strip()[:200]}")
                if len(hits) >= MATCH_LIMIT:
                    return "\n".join(hits)
    return "\n".join(hits) or "Ничего не найдено"


def _read_head(path: Path) -> str | None:
    """Начало файла до GREP_FILE_MAX_BYTES; None - прочитать не удалось."""
    try:
        with path.open("rb") as fh:
            return fh.read(GREP_FILE_MAX_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None


async def run_tool(name: str, roots: list[Path], args: dict) -> tuple[str, list[str]]:
    """(ответ инструмента, прочитанные файлы).

    Инструменты выполняем мы, поэтому их падение - наша ошибка, а не отказ
    провайдера: модель должна получить текст ошибки и продолжить, а не
    ронять весь запрос.
    Сама работа - обход каталогов и чтение файлов - уходит в рабочий поток:
    пока агент одного пользователя читает клон, остальные не ждут. К БД
    инструменты не обращаются, поэтому соединение остаётся однопоточным.
    """
    try:
        return await asyncio.to_thread(_dispatch_tool, name, roots, args)
    except Exception as exc:  # noqa: BLE001 - инструмент не должен ронять ответ
        log.warning("Инструмент %s не отработал (%r): %s", name, args, exc)
        return f"Инструмент не отработал: {exc}", []


def _dispatch_tool(name: str, roots: list[Path], args: dict) -> tuple[str, list[str]]:
    if name == "read_file":
        return _tool_read(roots, args)
    if name == "grep":
        return _tool_grep(roots, args), []
    if name == "glob":
        return _tool_glob(roots, args), []
    return f"Неизвестный инструмент: {name}", []


# Обзор дерева уходит в каждый запрос, но меняется он только вместе с клоном -
# строить его заново на каждое обращение к провайдеру незачем.
_overviews: dict[tuple[str, ...], str] = {}


def reset_overview() -> None:
    """Клон обновился - обзор пересчитается на следующем запросе (repo.`_git`)."""
    _overviews.clear()


async def overview(roots: list[Path], lang: str = i18n.DEFAULT_LANG) -> str:
    """Обзор доступных каталогов; считается один раз на состояние клона.

    Первый обход - в рабочем потоке: он читает каталоги, а не БД, и держать на
    нём цикл событий нечего. Дальше ответ берётся из памяти.
    """
    key = tuple(str(root) for root in roots)
    if key not in _overviews:
        _overviews[key] = await asyncio.to_thread(_build_overview, roots)
    listing = _overviews[key]
    return f"{i18n.t(lang, 'PROMPT_OVERVIEW_HEAD')}\n{listing}" if listing else ""


def _build_overview(roots: list[Path]) -> str:
    """Что лежит в доступных каталогах: документация первой, затем остальное.

    Служебные каталоги (скрытые - имя сегмента начинается с точки, вроде
    `.claude/` или `.git/`, включая рабочие копии веток внутри них) в обзор
    не попадают: на рабочем клоне все 40 строк лимита уходили именно на них,
    ни одного пути из `docs/`. Фильтр применяется ДО потолка
    в 2000 файлов - иначе на клоне, где `.git`/`.claude` идут первыми по
    алфавиту и сами превышают 2000 файлов, отсев после среза не оставил бы
    ничего. Точечные пути закрыты и для grep/glob/read_file (`_confined`),
    здесь фильтр остаётся страховкой для обзора. Пути показываем относительными -
    абсолютные вчетверо длиннее, а уходят они в каждый запрос.
    """
    visible = (p for p in _iter_files(roots, "**/*") if _is_visible(p, roots))
    paths = list(itertools.islice(visible, 2000))
    docs_first = sorted(paths, key=lambda p: (p.suffix not in (".md", ".txt"), str(p)))
    shown = [_short(p, roots) for p in docs_first[:OVERVIEW_LIMIT]]
    return "\n".join(shown)  # заголовок - по языку вызова, в `overview`


def _is_visible(path: Path, roots: list[Path]) -> bool:
    """`False`, если хоть один сегмент относительного пути начинается с
    точки - служебный каталог, а не документация или код."""
    return not any(part.startswith(".") for part in Path(_short(path, roots)).parts)


def _short(path: Path, roots: list[Path]) -> str:
    for root in roots:
        if root == path or root in path.parents:
            return str(path.relative_to(root))
    return str(path)


# Те же инструменты в плоском формате /v1/responses.
RESPONSES_TOOLS = [{"type": "function", **tool["function"]} for tool in OPENAI_TOOLS]
