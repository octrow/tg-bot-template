## 1. OmniRoute

- [x] 1.1 `TGBOT_LLM_PROVIDER=omniroute`, `OMNIROUTE_API_KEY`, `OMNIROUTE_MODEL` (обязательна при omniroute)
- [x] 1.2 Ступень `omniroute` в `STAGES`: адрес по умолчанию `http://localhost:20128/v1`, без `routing`, `reasoning_effort` в лёгком запросе
- [x] 1.3 Распознавание скриншотов через действующую ступень, без `models` и фильтра квантизации у OmniRoute
- [x] 1.4 Предупреждение о ключе и маскирование ключа OmniRoute в журнале процесса

## 2. Только для админов

- [x] 2.1 `TGBOT_OWNER_ONLY`: allowlist не действует, чужим молчание, `/request_access` молчит
- [x] 2.2 Пустой `ADMIN_USER_IDS` при `TGBOT_OWNER_ONLY` роняет старт

## 3. Документация и тесты

- [x] 3.1 `deploy/env.example`, README, `deploy/DEPLOY.md`, дополнение ADR-0004
- [x] 3.2 `tests/test_omniroute.py`
