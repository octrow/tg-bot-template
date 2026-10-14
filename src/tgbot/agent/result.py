"""Ответ агента (`AgentResult`) и признак непроверенного ответа."""

class AgentResult(str):
    """Ответ агента, остающийся обычной строкой для всех call-sites.

    `files_read` - файлы, реально прочитанные ЦЕЛИКОМ в ЭТОМ вызове; None -
    неизвестно (ответ из кэша, тестовый стаб) и за отсутствие чтения не
    считается. Найденное поиском в него НЕ попадает (см. `tools_used`,
    спека grounding-verification).
    `tools_used` - имена ВСЕХ вызванных инструментов чтения (Read/Grep/Glob и
    их OpenAI-аналоги), включая поиск без единого чтения. None - неизвестно
    (стаб/кэш), как и у `files_read`. На нём строится `is_unverified`: поиск -
    тоже обращение к коду (спека grounding-verification).
    `tools_stripped` - инструменты были сняты из запроса (отказ API): ответ
    получен без доступа к коду, и выдавать его за проверенный нельзя
    (см. spec grounding-verification).
    """

    files_read: list[str] | None
    tools_used: list[str] | None
    tools_stripped: bool

    def __new__(
        cls,
        text: str,
        files_read: list[str] | None = None,
        tools_used: list[str] | None = None,
        tools_stripped: bool = False,
    ):
        obj = super().__new__(cls, text)
        obj.files_read = files_read
        obj.tools_used = tools_used
        obj.tools_stripped = tools_stripped
        return obj


def files_read_of(answer) -> list[str] | None:
    """`files_read` ответа; None - неизвестно (кэш, стаб, голая строка)."""
    return getattr(answer, "files_read", None)


def tools_used_of(answer) -> list[str] | None:
    """`tools_used` ответа; None - неизвестно (кэш, стаб, голая строка)."""
    return getattr(answer, "tools_used", None)


def is_unverified(answer) -> bool:
    """Ответ претендует на факты, но к коду не обращался - или инструменты
    были сняты. К коду обращались, если вызван хоть один инструмент чтения,
    включая поиск (Grep/Glob и их OpenAI-аналоги grep/glob) - найти файл и
    строку поиском тоже значит подтвердить ответ кодом (спека
    grounding-verification). `tools_stripped` первичен: инструменты сняты -
    непроверено, даже если что-то успели найти до снятия.

    Когда `tools_used` неизвестен (None - старые стабы/кэш, отвечающие только
    `files_read`), решение принимается по числу прочитанных ЦЕЛИКОМ файлов -
    прежнее поведение. Неизвестность (None) отказом не считается ни там, ни
    там."""
    if bool(getattr(answer, "tools_stripped", False)):
        return True
    tools_used = tools_used_of(answer)
    if tools_used is not None:
        return len(tools_used) == 0
    return files_read_of(answer) == []
