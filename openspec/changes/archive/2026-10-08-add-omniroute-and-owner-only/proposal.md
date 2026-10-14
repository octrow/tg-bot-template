## Why

Бот должен уметь ходить в LLM через свой шлюз OmniRoute, а не только через OpenRouter: у владельца копии может не быть ключа OpenRouter, зато есть OmniRoute с подписками. И у копии шаблона, которой пользуется один человек, allowlist и заявки на доступ - лишняя поверхность: чужие могут слать заявки админу.

## What Changes

- `TGBOT_LLM_PROVIDER` допускает второе значение - `omniroute`. Это замена ступени, а не запасной провайдер: ступень по-прежнему одна (ADR-0004, дополнение 2026-10-08).
- Новые переменные `OMNIROUTE_API_KEY` и `OMNIROUTE_MODEL`; адрес шлюза - `TGBOT_LLM_BASE_URL` (по умолчанию `http://localhost:20128/v1`). Без `OMNIROUTE_MODEL` при `omniroute` старт падает.
- Запрос к OmniRoute не несёт полей, которые понимает только OpenRouter: фильтр квантизации, `models`, запрос `/generation`. Скриншоты идут через шлюз тем же ключом, одной основной моделью.
- Новый режим `TGBOT_OWNER_ONLY=1`: бота используют только `ADMIN_USER_IDS`, allowlist не действует, `/request_access` молчит, чужим нет даже отказа. С пустым `ADMIN_USER_IDS` старт падает.

## Capabilities

### New Capabilities

### Modified Capabilities

- `llm-stage-selection`: ступень OpenRouter или OmniRoute; запрос OmniRoute без полей OpenRouter.
- `screenshot-input`: распознавание через действующую ступень.
- `access-control`: режим "только для админов".
- `access-request`: `/request_access` выключен в режиме "только для админов".

## Impact

- Код: `config.py`, `agent/stages.py`, `vision.py`, `screenshot_input.py`, `access.py`, `handlers/access.py`, `bot.py`, `__main__.py`.
- Настройки: `deploy/env.example`, README, `deploy/DEPLOY.md`, ADR-0004.
- Тесты: `tests/test_omniroute.py`.
