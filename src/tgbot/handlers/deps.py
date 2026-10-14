"""Общие зависимости обработчиков и запись реестра режимов.

Обработчики - функции модулей `handlers/<зона>.py` вида `async def name(update,
context)`. Всё общее (соединение с БД, доступ, конфиг) и состояние процесса
(диалоги Справки, предложения перехода, поставленные меню, пачки, альбомы)
живут в одном `Deps`: его создаёт `tgbot.bot.build_deps`, а `build_application`
кладёт в `application.bot_data` - место PTB для данных всего приложения.
Обработчик берёт его через `get_deps(context)`."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .. import i18n, modes
from ..access import Access
from ..config import Config
from ..mode_switch import Switches
from ..packs import Packs
from ..reference_mode import Dialogs
from ..screenshot_input import Images

DEPS_KEY = "deps"


@dataclass(frozen=True)
class Deps:
    access: Access
    config: Config | None = None
    # Предложения перейти в другой режим по просьбе словами.
    switches: Switches = field(default_factory=Switches)
    # Короткий контекст Справки по пользователям (спека reference-mode).
    dialogs: Dialogs = field(default_factory=Dialogs)
    # Какое меню (уровень, язык) уже встало у пользователя за жизнь процесса.
    # /allow тому, кто ещё не открывал чат с ботом, падает "chat not found" -
    # тогда меню доставит его первый /start.
    applied_menus: dict[int, tuple[str, str]] = field(default_factory=dict)
    # Сессия Справки уже открыта входящим апдейтом, а диалог ещё не сохранён:
    # второе сообщение пачки и сам ответ её продолжают (session-log-completeness).
    reference_opened: set[int] = field(default_factory=set)
    # Пачка сообщений (спека message-packs); None - окно 0, без пачек.
    packs: Packs | None = None
    # Вход картинок: альбомы, которые ещё собираются.
    images: Images | None = None

    @property
    def conn(self):
        return self.access.conn


def get_deps(context) -> Deps:
    return context.bot_data[DEPS_KEY]


def user_lang(conn, update) -> str:
    """Язык обработчика, реагирующего на своего же отправителя: сохранённый
    выбор -> автоопределение по профилю ЭТОГО апдейта -> русский."""
    user = update.effective_user
    lang = i18n.lang_for(conn, user.id, getattr(user, "language_code", None))
    modes.ui_lang.set(lang)
    return lang


@dataclass(frozen=True)
class ModeSpec:
    """Режим кроме Справки - запись реестра `handlers.registry.MODES`.

    `mode` - значение `sessions.mode`, метка классификатора Справки и ключ
    текстов `MODE_HEADERS` / `MODE_GO` в локалях; `intent` - критерий
    классификатора (`tgbot.intent`): какая просьба ведёт в режим. `command` -
    команда входа (рабочее меню, после /help), `callback_prefix` - начало
    callback_data кнопок режима. `enter(deps, message, lang, user_id, text)` -
    вход (команда или кнопка перехода из Справки), `text(deps, message, lang,
    session, text)` - свободный текст при открытой сессии режима."""

    mode: str
    intent: str
    command: str
    callback_prefix: str
    command_handler: Callable[..., Awaitable[None]]
    callback_handler: Callable[..., Awaitable[None]]
    enter: Callable[..., Awaitable[None]]
    text: Callable[..., Awaitable[None]]
