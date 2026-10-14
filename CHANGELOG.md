# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow `tgbot.__version__`.

## [Unreleased]

### Added
- English README (Russian moved to `README.ru.md`), contributing guide, security policy, code of conduct,
  issue forms.
- CI: tests and ruff on push and pull request; gitleaks over the whole history; Dependabot for uv and
  GitHub Actions.
- Pre-commit hook blocks staged secrets when gitleaks is installed.

## [0.1.0]

### Added
- Template extracted from a production bot: Help and Echo modes, message batches, screenshot to text,
  model ladder via OpenRouter, answer checks, cache, incident codes and logs, access control, feedback,
  ru/en.
- OmniRoute as an alternative LLM provider and owner-only access mode.
