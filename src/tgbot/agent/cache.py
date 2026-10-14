"""Кэш ответов агента в SQLite (`agent_cache`): ключ, критерий
кэшируемости, попадание и запись."""

import hashlib

from .. import db
from .result import is_unverified

# Версия кэша: поднимается вручную, когда меняются формулировки промптов,
# системный промпт или критерий кэшируемости ответа - иначе старые ответы
# переживут релиз, который их чинит. Версия 9 - новый критерий
# "полный вызов без прочитанных файлов не кэшируется" (см. `cacheable`, спека
# agent-cache-hygiene): уже накопленные деградированные
# записи иначе доживут до конца TTL. Версия 10 - добавляет язык промпта в ключ:
# иначе русский ответ мог бы обслужить английскую сессию (и наоборот). Версия
# 11 - бракованный ответ (обрезанный, выродившийся, длиннее сообщения
# Telegram) не кэшируется: мусорный ответ s126 иначе дожил бы до конца TTL.
# Версия 12 - у Справки свой системный промпт (reference-language-and-grounding).
CACHE_VERSION = 12


def cache_key(user_id: int | None, stage: str, light: bool, prompt: str, lang: str) -> str:
    """Ключ кэша: версия + пользователь + ступень + режим + язык + промпт.

    Ступень в ключе - чтобы ответ дешёвого фолбэка не выдавался за ответ
    основного агента; режим - потому что в лёгком агент не читает репозиторий.
    Пользователь - потому что на разборе коротких ответов промпт это метка поля
    плюс "да" или "не знаю", и совпадения у разных пользователей ожидаемы: попасть
    в чужой ответ нельзя, а переиспользование между людьми того не стоит. Язык -
    иначе ответ, закэшированный для одного языка сессии, обслужил бы другой.
    """
    raw = f"{CACHE_VERSION}|{user_id}|{stage}|{int(light)}|{lang}|{prompt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def cacheable(answer: str | None, *, light: bool = False) -> bool:
    """Отказ и пустой ответ не кэшируем: показать их нечего, а живут они до
    конца TTL и переживают исправление причины отказа.

    Полный вызов (`light=False`), чей ответ не подтверждён кодом, - тоже не
    кэшируем: `is_unverified` здесь ровно то, что нужно - файлов ноль либо
    инструменты были сняты отказом провайдера (`tools_stripped`), и в обоих
    случаях кэшировать деградацию на весь TTL нельзя (спека agent-cache-hygiene).
    У лёгкого вызова инструментов чтения нет по замыслу - пустой `tools_used`
    ему не в минус (раньше решал пустой `files_read`, спека grounding-verification;
    поле сменилось, довод - нет)."""
    if not (answer and answer.strip()):
        return False
    return light or not is_unverified(answer)


def cache_hit(conn, user_id: int, key: str, ttl_s: float, context_label: str) -> str | None:
    """Ответ из кэша (и запись в журнал, чей он) либо None."""
    hit = db.cache_get(conn, key, ttl_s)
    if hit is None:
        return None
    answer, provider, model = hit
    db.log_event(conn, user_id, "agent_cache_hit", {"context": context_label})
    db.log_event(
        conn,
        user_id,
        "llm_response",
        {"cached": True, "context": context_label, "provider": provider, "model": model},
    )
    return answer


def cache_store(
    conn, key: str | None, answer: str | None, provider: str, model=None, *, light: bool = False
) -> None:
    """Кладёт ответ в кэш, если кэш включён (`key`) и ответ есть что показать.

    `light` уходит в `cacheable` - решает, применять ли критерий "файлов
    ноль" (только к полным вызовам): `files_read`/`tools_stripped` уже несёт
    сам `answer` (`AgentResult`), передавать их отдельно незачем."""
    if key is not None and cacheable(answer, light=light):
        db.cache_put(conn, key, answer, provider=provider, model=model)
