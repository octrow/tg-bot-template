# tg-bot-template

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/hero.svg">
    <img src="docs/assets/hero-light.svg" width="100%" alt="tg-bot-template: a Telegram bot that answers questions about your product from its docs and code">
  </picture>
</p>

<p><b>English</b> | <a href="README.ru.md">Русский</a></p>

A Python template for a Telegram bot that answers questions about your product from its docs and source
code. It runs on python-telegram-bot 22 and SQLite and talks to LLMs through OpenRouter (or a local
OmniRoute gateway). Out of the box you get a model fallback ladder, message batching, screenshot-to-text,
user feedback, access control and admin commands, all covered by 878 tests. New behaviour goes into a
separate mode module.

It's for developers who need a support or internal Q&A bot for their product and don't want to build
the plumbing around the LLM call themselves. The template was extracted from a bot that runs in
production. A new bot is a copy of the template made with the checklist below
([ADR-0006](docs/adr/0006-template-is-a-copy.md)); copies don't receive updates from the template.

## How it differs

- **From a bare python-telegram-bot or aiogram skeleton:** a skeleton gives you handlers and polling.
  This template also covers what comes after the first LLM answer: slow or broken model responses,
  several messages sent as one question, screenshots, answers that never looked at the docs, cost per
  session, incident codes and an admin log viewer.
- **From hosted no-code bot builders:** the bot reads your real repository with `read_file` / `grep` /
  `glob` tools and runs on your own server, so the code and the keys stay with you. The price is that you
  deploy and maintain it yourself (systemd unit and update script included).
- **What it is not:** a library or a framework. It is a starting point you copy and change.

## What's inside

**Modes.** A user always has exactly one active mode (terms are in [`CONTEXT.md`](CONTEXT.md), in Russian):

- **Help** is the default mode. It answers in plain language using the folders in `TGBOT_KNOWLEDGE_DIRS`
  and a clone of `TGBOT_REPO_URL`; the model reads them with the `read_file` / `grep` / `glob` tools.
  Each answer has "Clear / Not clear" buttons. "Not clear" makes the bot explain again more simply, and
  after the second try it offers to pass the question to people as Feedback.
- **Echo** (`/echo`) is a demo mode: it repeats the text and counts steps. It's the sample for your own
  mode; a real bot removes it.

**Shared machinery** that every mode gets:

- **Message batches.** Consecutive messages are glued into one question (window `TGBOT_TEXT_DEBOUNCE_S`).
  A message that arrives while the answer is being prepared is late: the answer goes out anyway, marked,
  with an "Answer with the new ones" button. Any command drops the open batch.
- **Screenshot to text.** A photo or a PNG/JPG/WEBP file is recognised into text once by a model from
  `TGBOT_VISION_MODELS` ([ADR-0005](docs/adr/0005-screenshot-to-text-over-multimodal-ladder.md)) and then
  follows the same path as text. Up to 10 images per batch.
- **Reactions.** The bot reacts to each message it accepts into a batch.
- **Model ladder** via OpenRouter ([ADR-0003](docs/adr/0003-model-ladder-over-single-stage.md),
  [ADR-0004](docs/adr/0004-openrouter-only.md)): a primary model; after a time threshold or a bad answer,
  backup models start in parallel and the first good answer wins. Thresholds per call class are
  `TGBOT_LLM_<CLASS>_STEPS_S`.
- **Answer checks.** Regexes catch degenerate answers (word fragments, mixed alphabets); borderline cases
  go to the Jev classifier (typesafe.ai, `TYPESAFE_API_KEY`). Jev also classifies intent: small talk goes
  to a cheap call without tools, and a request that fits another mode adds a switch button under the Help
  answer. Without a Jev key there is no intent classifier and the degeneration check is regexes only.
- **Unverified answers.** An answer where the model never touched the files (no read, no search), or where
  tools were removed, comes with a warning and isn't cached.
- **Answer cache** for the agent in SQLite (`TGBOT_AGENT_CACHE_TTL_S`), cleared with `/cache_clear`.
- **Incident codes and log.** On a failure the user sees a short code and a "Report to developers" button.
  The event log lives in SQLite and is mirrored to per-session JSONL files (`TGBOT_LOGS_DIR`), rotated by
  folder size. Admins get a push on timeouts, bad answers and handler errors. The process log (stderr,
  journald) carries `[u=<user> upd=<update>]` on every line and masks tokens and keys.
- **Access.** Admins come from `ADMIN_USER_IDS`, everyone else from an allowlist. A stranger can apply
  with `/request_access` and admins decide with buttons. For a bot only you use, set `TGBOT_OWNER_ONLY=1`:
  only `ADMIN_USER_IDS` get in, strangers get silence, no applications.
- **Feedback.** `/support <text>` saves feedback, `/feedback` shows admins the unread ones, and on
  weekdays at 15:00 (Asia/Almaty; `TGBOT_REMINDER_HOUR`, `TGBOT_REMINDER_TZ`) admins get a reminder about
  unread feedback.
- **Languages** ru and en: `/language`; until the user picks, the Telegram profile decides. If someone
  writes in the other language, the bot offers to switch.
- **Plain language** ([`docs/simple-language.md`](docs/simple-language.md), in Russian): rules in the
  prompts, code that cleans model output, a linter test for static texts, and a pre-commit hook that
  removes typographic Unicode.

**Commands.** For every admitted user: `/start`, `/help`, `/echo`, `/support`, `/language`, `/whoami`.
Before admission: `/whoami`, `/request_access`, `/language`. Admins also get:

| Command | What it does |
|---|---|
| `/allow <user_id or @username>` | add to the allowlist |
| `/deny <user_id or @username>` | remove from the allowlist |
| `/users` | admins and the allowlist |
| `/cost [session_id]` | tokens and $ spent per session (default: your last one) |
| `/cache_clear` | clear the agent answer cache |
| `/logs` | log files in the chat, with paging |
| `/feedback [all]` | unread feedback with "Read" and "Session log" buttons; `all` shows the last 10 |

## Architecture

Package `src/tgbot`:

| Module | Contents |
|---|---|
| `__main__` | startup: config, file lock (one poller), `startup` log entry, repo clone, `run_polling` |
| `bot` | builds the `Application`: `Deps` in `bot_data`, registers commands and buttons |
| `config` | `Config` and `load_config`: env plus `./.env`, `/opt/tg-bot/.env` (or `TGBOT_ENV_FILE`), `~/.env` |
| `handlers/deps` | `Deps` (shared dependencies and process state), `ModeSpec`, `get_deps`, `user_lang` |
| `handlers/registry` | registry of modes other than Help (`MODES`) |
| `handlers/reference` | Help, free text input, batches, update log |
| `handlers/echo` | demo mode `/echo`, the sample mode |
| `handlers/modes` | the mode switch button under a Help answer |
| `handlers/{admin,access,support,language,common}` | admin commands, access and applications, feedback and reminders, language, `/start` `/help` and the menu |
| `modes`, `mode_switch` | the "mode · stage" header and switch suggestions |
| `reference_mode` | Help prompts and the short dialogue context |
| `agent/` | `ladder`, `openrouter` (transport), `tools` (file reading), `cache`, `failures`, `result`, `stages` |
| `intent`, `degeneration`, `jev` | intent classifier, degeneration check, Jev client |
| `packs`, `screenshot_input`, `vision` | message batches, image input, screenshot recognition |
| `repo` | clone and `git pull` of `TGBOT_REPO_URL` |
| `db`, `journal`, `logs_view` | SQLite (sessions, users, feedback, applications, cost, cache), file log, `/logs` output |
| `access`, `alerts`, `i18n` + `locales/`, `utils`, `logging_setup` | allowlist, admin pushes, ru/en texts, sending long messages and HTML, process log |

Handlers are `async def name(update, context)` functions; they get dependencies through `get_deps(context)`.
If the root has `docs/howto.md`, its text goes into the Help prompt as instructions for the bot.

## New bot checklist

1. Copy the template without `.git`, `.venv`, `*.db`, `logs/`, `.env*`:
   `rsync -a --exclude .git --exclude .venv --exclude '*.db' --exclude logs --exclude '.env*' tg-bot-template/ my-bot/`
2. `cd my-bot && git init`.
3. Name the bot: name and description in BotFather, texts in `src/tgbot/locales/{ru,en}.py` (`START`,
   `CMD_DESCRIPTIONS`, `REFUSAL`), `name` and `description` in `pyproject.toml`, and the product name for
   the prompts in `TGBOT_PRODUCT_NAME`.
4. `.env` from `deploy/env.example`: `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY`, `TYPESAFE_API_KEY`,
   `ADMIN_USER_IDS`, `TGBOT_KNOWLEDGE_DIRS`, `TGBOT_REPO_URL`.
5. Create a separate test bot for development and `.env.e2e` ([`tests_e2e/README.md`](tests_e2e/README.md)).
   The production token lives only on the server.
6. `git config core.hooksPath .githooks`: the pre-commit hook cleans typographic Unicode and, when
   `gitleaks` is installed, blocks commits with secrets.
7. `uv sync --extra dev && uv run pytest`.
8. Deploy: the systemd unit from [`deploy/DEPLOY.md`](deploy/DEPLOY.md).
9. Once you have your first own mode, remove `/echo`: `echo.SPEC` from `handlers/registry.py`, the
   `handlers/echo.py` module, its texts in locales (`ECHO_*` and the `echo` keys) and the tests that
   rely on it (`rg -l echo tests tests_e2e`).

## Adding a mode

1. Copy `src/tgbot/handlers/echo.py` to `handlers/<mode>.py`. Set `MODE`, the entry function
   `enter(deps, message, lang, user_id, text)` and the step function `text_step(deps, message, lang, session, text)`,
   the command and button handlers, and `SPEC = ModeSpec(mode, intent, command, callback_prefix,
   command_handler, callback_handler, enter, text)`. `intent` is the classifier criterion: which request
   leads to this mode.
2. Add `<mode>.SPEC` to `handlers.registry.MODES`.
3. Texts in `locales/ru.py` and `locales/en.py`: `MODE_HEADERS` (header with icon), `MODE_GO` (switch
   button), `MODE_STAGES` (stages), `CMD_DESCRIPTIONS` (command description) and the mode's own strings.
   Both locales must have the same keys (`tests/test_i18n.py`).

The rest comes from the registry: command and buttons (`bot`), menu position (`handlers.common`), the
classifier label and criterion (`intent`), text while a session is open (`handlers.reference`) and entry
via the switch button (`handlers.modes`). Entering a mode opens a session (`db.open_mode_session`), leaving
closes it (`db.close_mode_session`); the step number is stored in `sessions.step`. Write the step through
`db.advance_session` (as `echo.text_step` does): it won't revive a session that was closed while the step
was running.

Traps:

- the mode name (`MODE`) is not `chat` or `question`: those are Help classifier labels (`intent`);
- the button prefix (`callback_prefix`) doesn't overlap the taken ones: `lang:`, `logs:`, `fb_`, `fail:`,
  `pack:redo:`, `access:`, `ref:`, `mode:`, `echo:`; the handler is picked by the start of `callback_data`;
- the command doesn't clash with an existing one (`bot.COMMAND_HANDLER_NAMES`, other modes' commands);
- every new text key goes into both locales (`ru.py` and `en.py`), or `tests/test_i18n.py` fails.

## Settings

Environment variables. On the server systemd supplies them (`EnvironmentFile`); locally `load_config` fills
in unset ones from `./.env`, `/opt/tg-bot/.env`, `~/.env` (first value found wins; the process environment
beats the files). `TGBOT_ENV_FILE` replaces the deploy path `/opt/tg-bot/.env`. Example:
[`deploy/env.example`](deploy/env.example).

| Variable | Default | What it sets |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | - (required) | bot token from BotFather |
| `ADMIN_USER_IDS` | empty | admin user_ids, comma-separated |
| `TGBOT_DB_PATH` | `./tgbot.db` | SQLite file; the `repo` clone folder sits next to it |
| `TGBOT_KNOWLEDGE_DIRS` | `docs` | folders Help reads, comma-separated; relative ones are from the working directory |
| `TGBOT_REPO_URL` | none | the product's git repo (`https://`, `ssh://`, `file://`, `git@host:path`): cloned at startup, `git pull --ff-only` before each Help answer. For access prefer a deploy key (ssh) or a read-only token; a token from `https://<token>@...` is passed to git as a header through the environment and never lands in the clone's `.git/config` |
| `TGBOT_PRODUCT_NAME` | none ("this product") | product name in the Help prompts |
| `OPENROUTER_API_KEY` | none | OpenRouter key; without it the bot runs but has no model answers and no screenshot recognition |
| `OPENROUTER_MODEL` | `z-ai/glm-5.3-flash` | the ladder: up to three comma-separated models, the first is primary; more than three fails startup |
| `TGBOT_VISION_MODELS` | `google/gemini-3.1-flash-lite,google/gemini-3.8-flash` | screenshot recognition models, up to three; `:free` fails startup |
| `TGBOT_LLM_PROVIDER` | `openrouter` | `openrouter` or `omniroute`; anything else fails startup |
| `OMNIROUTE_API_KEY` | none | OmniRoute key (the gateway's Endpoint page); required with `omniroute`, also used for screenshots |
| `OMNIROUTE_MODEL` | none | the gateway's model ladder (up to three, OmniRoute names: `if/...`, combo); required with `omniroute`. Set `TGBOT_VISION_MODELS` with gateway names too |
| `TGBOT_OWNER_ONLY` | none | `1`: the bot serves only `ADMIN_USER_IDS`, allowlist and `/request_access` are off, strangers get silence; an empty `ADMIN_USER_IDS` fails startup |
| `TYPESAFE_API_KEY` | none | Jev key: borderline degeneration cases and the intent classifier |
| `TGBOT_TEXT_DEBOUNCE_S` | `2.0` | batch window, seconds; `0` turns batches off |
| `TGBOT_REMINDER_TZ` | `Asia/Almaty` | feedback reminder time zone (IANA name); an unknown one fails startup |
| `TGBOT_REMINDER_HOUR` | `15` | feedback reminder hour (Mon-Fri), 0-23; anything else fails startup |
| `TGBOT_AGENT_CACHE_TTL_S` | `86400` | agent answer cache lifetime, seconds; `0` turns the cache off |
| `TGBOT_LLM_FULL_STEPS_S` | `120,240,600` | ladder thresholds for a full call: second model, third, failure |
| `TGBOT_LLM_REFERENCE_STEPS_S` | `120,240,600` | the same for a Help question |
| `TGBOT_LLM_LIGHT_STEPS_S` | `20,40,60` | the same for a light call (small talk) |
| `TGBOT_LOGS_DIR` | `./logs` | file log folder (JSONL) |
| `TGBOT_LOGS_MAX_BYTES` | `1073741824` (1 GiB) | log folder size cap; a non-integer fails startup |
| `TGBOT_LOCK_PATH` | `TGBOT_DB_PATH` with a `.lock` extension | lock file: a second instance with the same database won't start |
| `TGBOT_LOG_LEVEL` | `INFO` | process log level |
| `TGBOT_ENV_FILE` | `/opt/tg-bot/.env` | path of the deploy env file in `load_config`'s file list (another bot folder) |
| `TELEGRAM_BOT_API_BASE_URL` | `https://api.telegram.org/bot` | Bot API address (test DC, fake in tests) |
| `TGBOT_LLM_BASE_URL` | none | OpenAI-compatible API prefix including `/v1`: the OmniRoute address (default `http://localhost:20128/v1`) or a fake in tests |

Ladder thresholds are three increasing positive numbers, comma-separated; a typo fails startup.
`TGBOT_LOGS_DIR`, `TGBOT_LOGS_MAX_BYTES`, `TGBOT_LOCK_PATH` and `TGBOT_LOG_LEVEL` are read from the process
environment, not from `.env` files: set them in the environment locally and in the unit's `.env` on the
server. `TGBOT_ENV_FILE` is also process-environment only: the path is needed before the file is read, so
setting it inside the file changes nothing.

## Running locally

```bash
uv sync --extra dev
cp deploy/env.example .env   # TEST bot token, keys
uv run python -m tgbot
```

`bash deploy/restart.sh` restarts the local process in one command (it stops the previous one by the pid
file in `logs/`). One token, one poller: don't run the production token locally.

## Tests

- `uv run pytest -q`: unit tests and the Bot API layer (`tests/botapi`: a real bot process against a fake
  Telegram and a fake LLM, `tests/fakes`).
- Coverage: `uv run --with pytest-cov pytest --cov=tgbot --cov-report=term-missing`.
- True E2E on Telegram's test DC through Telethon: `deploy/e2e.sh`, setup in
  [`tests_e2e/README.md`](tests_e2e/README.md).
- `ruff check --select F src tests tests_e2e extra_scripts` (install ruff separately, it isn't in `dev`).

CI runs the unit tests and ruff on every push and pull request; a separate workflow scans the whole git
history for secrets with gitleaks.

## Known limitations

- A mode session doesn't expire on its own: leave it with the mode button or `/start`.
- Per-user process memory (Help dialogues, access refusals, menus set, open Help sessions) isn't cleared
  until restart; with hundreds of users that's kilobytes.
- Batch background tasks aren't awaited on shutdown: a batch being collected at that moment is lost.
- The `deploy/tg-bot.service` unit has no extra sandboxing. A drop-in with `NoNewPrivileges=yes`,
  `ProtectSystem=strict`, `ReadWritePaths=/opt/tg-bot/data` (database, clone and log folder) is
  recommended; keep `.env` as `root:root 600`, since systemd reads `EnvironmentFile` as root.

## Deploy

[`deploy/DEPLOY.md`](deploy/DEPLOY.md) (in Russian): first deploy (systemd unit `deploy/tg-bot.service`),
updates with `deploy/update.sh`, common problems.

## Documents

Project documents are in Russian.

- [`CONTEXT.md`](CONTEXT.md): glossary.
- [`docs/adr/`](docs/adr/): decisions: model ladder, OpenRouter only, screenshot to text, template as a copy.
- [`docs/simple-language.md`](docs/simple-language.md): how the bot writes for people.
- [`openspec/specs/`](openspec/specs/): behaviour specs, the only detailed documentation of the machinery.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Report vulnerabilities privately,
not in issues.
