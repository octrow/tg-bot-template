# Contributing

**English** | [Русский](CONTRIBUTING.ru.md)

Bug reports, questions and small pull requests are welcome. For a bigger change, open an issue first so
we can agree on the approach before you spend time on it.

## Setup

```bash
uv sync --extra dev
git config core.hooksPath .githooks   # cleans typographic Unicode, blocks secrets if gitleaks is installed
uv run pytest -q
```

Use a separate test bot for development; never run a production token locally.

## Pull requests

- One change per PR, with a test that fails without it.
- `uv run pytest -q` and `uvx ruff check --select F src tests tests_e2e extra_scripts` pass.
- A new user-facing string goes into both `locales/ru.py` and `locales/en.py`.
- Behaviour changes update the matching spec in `openspec/specs/` and, if needed, the README (both
  `README.md` and `README.ru.md`).
- Commit messages: `<type>: <description>`, types `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `ci`.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
