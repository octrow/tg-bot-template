# Security policy

**English** | [Русский](SECURITY.ru.md)

## Reporting a vulnerability

Please don't open a public issue. Report it privately through
[GitHub private vulnerability reporting](../../security/advisories/new). I'll reply within 7 days.

## Supported versions

This is a template: a new bot is a copy, and fixes are made only on the latest `main`. Copies don't get
them automatically; port the fix by hand.

## Keys and where they go

The bot reads its keys from the environment or a `.env` file (see `deploy/env.example`); keep it out of git
and readable only by the bot's user (`600`). It sends:

- `TELEGRAM_BOT_TOKEN` to the Telegram Bot API;
- `OPENROUTER_API_KEY` to OpenRouter, or `OMNIROUTE_API_KEY` to your OmniRoute gateway, together with user
  messages and the files the model reads from `TGBOT_KNOWLEDGE_DIRS` and the `TGBOT_REPO_URL` clone;
- `TYPESAFE_API_KEY` to typesafe.ai (Jev), together with message text for classification;
- a token inside `TGBOT_REPO_URL` to the git host, as an HTTP header, not stored in the clone.

The process log masks tokens and keys. Whatever the bot may read is sent to the model provider, so don't
point it at folders that hold secrets.
