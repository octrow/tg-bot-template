# Участие в разработке

[English](CONTRIBUTING.md) | **Русский**

Баг-репорты, вопросы и небольшие pull request'ы приветствуются. Перед крупным изменением откройте issue,
чтобы договориться о подходе до того, как вы потратите на него время.

## Подготовка

```bash
uv sync --extra dev
git config core.hooksPath .githooks   # чистит типографский юникод, при gitleaks не пускает секреты
uv run pytest -q
```

Для разработки заведите отдельного тестового бота; прод-токен локально не запускайте.

## Pull request'ы

- Одно изменение на PR и тест, который без него падает.
- Проходят `uv run pytest -q` и `uvx ruff check --select F src tests tests_e2e extra_scripts`.
- Новая строка для пользователя - в обе локали, `locales/ru.py` и `locales/en.py`.
- Изменение поведения обновляет свою спеку в `openspec/specs/` и, если нужно, README (оба,
  `README.md` и `README.ru.md`).
- Сообщения коммитов: `<type>: <description>`, типы `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `ci`.

Участвуя, вы соглашаетесь соблюдать [Кодекс поведения](CODE_OF_CONDUCT.md).
