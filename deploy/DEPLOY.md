# Деплой tg-bot

Плейсхолдеры ниже - заменить своими:

| Плейсхолдер | Что это |
|---|---|
| `<server>` | хост, где живёт бот (ssh `user@<server>`) |
| `tg-bot` | системный пользователь и имя systemd-юнита; сменили - поправить `tg-bot.service`, `update.sh` (`SERVICE`) |
| `/opt/tg-bot` | каталог бота на сервере; сменили - поправить `tg-bot.service` и `APP_DIR` в `update.sh`. Юниту путь `.env` не нужен (его грузит `EnvironmentFile`); для ручного запуска вне systemd задать путь в окружении процесса: `TGBOT_ENV_FILE=/srv/my-bot/.env` |
| `https://github.com/<org>/<bot-repo>` | репозиторий этого бота |

Бот - один процесс Python (long polling, наружу ничего не слушает), один
systemd-юнит, один файл SQLite. Пользователей процесс обслуживает параллельно
(конкурентная обработка апдейтов внутри одного цикла событий), но экземпляр
по-прежнему ровно один - `flock` при старте это гарантирует. Файлы деплоя лежат рядом:
[`tg-bot.service`](tg-bot.service), [`env.example`](env.example).

## Предварительно

- Токен бота из @BotFather. **Правило: прод-токен живёт только на сервере.**
  Один токен = один поллер: второй процесс с тем же токеном (например, на
  ноутбуке разработчика) будет случайным образом воровать апдейты, и бот
  "иногда не отвечает". Для разработки - отдельный тестовый бот.
- Ключ LLM: `OPENROUTER_API_KEY`. Ступень одна - OpenRouter (ADR-0004), запасного
  провайдера нет. Без ключа бот работает, но ответов модели нет: Справка
  отвечает понятным отказом, в journal при старте - предупреждение. `TGBOT_LLM_PROVIDER` по умолчанию
  `openrouter`; `omniroute` переключает на свой шлюз OmniRoute (`OMNIROUTE_API_KEY`,
  `OMNIROUTE_MODEL`, адрес - `TGBOT_LLM_BASE_URL`). Значения `auto`, `claude` и
  `openai` роняют старт.
- Модели - лестница в `OPENROUTER_MODEL` (до трёх, первая основная). Пример:
  `z-ai/glm-5.3-flash,openai/gpt-6-luna,deepseek/deepseek-v4.1-flash`. Запасную
  подключает сам бот по порогу времени или после брака/ошибки предыдущей
  (ADR-0003). После деплоя проверить запись `startup` в журнале: поле `models` -
  вся лестница по порядку.
- Скриншоты распознаются тем же `OPENROUTER_API_KEY` моделями из
  `TGBOT_VISION_MODELS` (по умолчанию `google/gemini-3.1-flash-lite`,
  запасная `google/gemini-3.8-flash`; ADR-0005). Без ключа OpenRouter на картинку
  приходит понятный отказ, текст работает как раньше.
- Ключ Jev `TYPESAFE_API_KEY` (необязательно): пограничные случаи вырождения и
  классификатор намерения. Без него работают только регулярки, а кнопки перехода в режим по
  просьбе словами нет.
- На сервере: Python 3.12+, git, доступ по https к api.telegram.org, openrouter.ai и
  github.com (или где лежат репозитории).

## Первый деплой

```bash
# 1. Пользователь и каталог
sudo useradd --system --home /opt/tg-bot --shell /usr/sbin/nologin tg-bot
sudo git clone https://github.com/<org>/<bot-repo> /opt/tg-bot
sudo mkdir -p /opt/tg-bot/data

# 2. Виртуальное окружение
cd /opt/tg-bot
sudo python3 -m venv .venv
# Только editable: иначе пакет копируется в site-packages, и git pull
# перестаёт влиять на то, что реально исполняет бот.
sudo .venv/bin/pip install -e .

# 3. Переменные окружения
sudo cp deploy/env.example /opt/tg-bot/.env
sudo nano /opt/tg-bot/.env        # TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, ADMIN_USER_IDS, ...
sudo chmod 600 /opt/tg-bot/.env
sudo chown -R tg-bot:tg-bot /opt/tg-bot

# 4. systemd
sudo cp deploy/tg-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-bot
```

## Проверка

```bash
systemctl status tg-bot                  # active (running)
journalctl -u tg-bot -n 20               # "Application started", без traceback
```

В Telegram: `/whoami` отвечает любому (свои id для ADMIN_USER_IDS берутся
отсюда); чужому пользователю - отказ с подсказкой `/request_access` (один раз за
время работы процесса); админу
вопрос текстом даёт ответ Справки, `/echo` - эхо с шапкой режима. В журнале не
должно быть URL с токеном (httpx приглушён до WARNING).

## Автозагрузка

Бот должен подниматься сам при перезагрузке сервера. Первый деплой это уже
включает (`enable --now` в шаге 4), но если юнит ставили руками - проверить:

```bash
systemctl is-enabled tg-bot        # должно быть: enabled
```

Если `disabled` (или юнит вовсе не запущен):

```bash
sudo systemctl enable --now tg-bot # включает автозагрузку и сразу стартует
```

Падения и "тихие" завершения процесса systemd перезапускает сам
(`Restart=always` в юните) - ручной `systemctl start` нужен только после
явного `systemctl stop`.

## Обновление

```bash
sudo /opt/tg-bot/deploy/update.sh
```

Скрипт делает всё сам: `git pull --ff-only` от имени `tg-bot`,
обновляет юнит из репозитория (с `daemon-reload`, только если он изменился),
переустанавливает зависимости при изменении `pyproject.toml`, перезапускает
сервис и проверяет, что тот поднялся. Если сервис не стал `active` - скрипт
печатает последние строки журнала и завершается с ненулевым кодом.

**Правки юнита прямо на сервере затираются обновлением.** Серверные значения
держите в drop-in `/etc/systemd/system/tg-bot.service.d/local.conf` -
его скрипт не трогает.

Рестарт безопасен для данных: allowlist, сессии и журнал - в SQLite
(`data/tgbot.db`); открытая сессия режима переживает рестарт, теряется только
пачка сообщений, которая копилась в момент рестарта. Бэкап = копия этого файла.

## Если что-то не так

| Симптом | Причина / что делать |
|---|---|
| Юнит падает сразу, в journal "TELEGRAM_BOT_TOKEN не задан" | Заполнить `.env`, `systemctl restart tg-bot` |
| Бот молчит пользователю | Он не в allowlist: `/whoami` -> `/allow <id>` от админа, или заявка `/request_access`. Отказ чужому показывается только один раз за время работы процесса |
| Бот "иногда не отвечает" | Второй поллер на том же токене (чей-то локальный запуск). Найти и погасить; перевыпустить токен через /token у BotFather, если источник неясен |
| Справка отвечает отказом с кодом | Код - id записи `llm_failure` в журнале SQLite: в ней ступень, род отказа, класс исключения и модели. Частая причина - нет или невалиден `OPENROUTER_API_KEY` |
| Старт падает с `TGBOT_LLM_PROVIDER: ожидалось одно из openrouter` | В env осталось значение удалённой ступени (`auto`/`claude`/`openai`): убрать переменную (дефолт `openrouter`). Ступень и модели видны в записи `startup` (`llm_provider`, `models`) и в journal строкой при старте |
| Старт падает с `OPENROUTER_MODEL: провайдер принимает не больше 3 моделей` | В списке моделей больше трёх - оставить основную и не больше двух запасных |
| Старт падает с `TGBOT_VISION_MODELS: бесплатные модели обучаются на запросах` | В списке модель `:free` - заменить платной: на скриншотах бывают чужие данные |
| На скриншот бот отвечает "не смог прочитать" | Журнал SQLite: `image_failed.reason` (`timeout`, `error`, `empty`, `degenerate`, `format`, `size`, `no_key`) |
| Старт падает с `TGBOT_LOGS_MAX_BYTES: ожидалось целое число` | Значение - целое число байт без суффиксов (`1073741824`, не `1G`) |
| Выкат прошёл, а поведение прежнее | Пакет установлен не editable: `sudo -u tg-bot /opt/tg-bot/.venv/bin/python -c "import tgbot; print(tgbot.__file__)"` - путь должен вести в `/opt/tg-bot/src/tgbot`. Если ведёт в `site-packages`: `sudo -u tg-bot /opt/tg-bot/.venv/bin/pip install -e /opt/tg-bot` + рестарт. `update.sh` теперь чинит это сам |
| `repo_pull_failed` в журнале SQLite | Нет доступа к github.com - бот продолжает работать на текущей версии клона, не критично |
| В journal `Клона репозитория нет` | Клон `TGBOT_REPO_URL` не создан (обычно нет доступа к приватному репозиторию). Справка отвечает, но только по `TGBOT_KNOWLEDGE_DIRS`. Проверить: `ls /opt/tg-bot/data/repo` и `journalctl -u tg-bot \| grep -i git`; дать доступ по `deploy/env.example` (deploy key или read-only токен в `TGBOT_REPO_URL`), затем `systemctl restart tg-bot` - клон делается на старте |
| `detected dubious ownership` в journal | Клон сделан не пользователем сервиса (обычно руками из-под root). Бот всё равно работает (`safe.directory` подставляется в вызов), но записать в каталог не сможет: `sudo chown -R tg-bot:tg-bot /opt/tg-bot/data/repo` |
| Отказ модели `400 Bad Request` в `llm_failure` | В записи - тело ответа провайдера, в нём и причина (нет такой модели, неподдерживаемый параметр). Проверить `OPENROUTER_MODEL` |
| `git ... could not read Username` в journal | Учётных данных на машине нет; git специально запускается неинтерактивно и падает сразу, а не висит до таймаута. Настроить доступ (см. строку выше) |
