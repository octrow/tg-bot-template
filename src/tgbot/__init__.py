"""Шаблон Telegram-бота.

Единственный источник версии: `pyproject.toml` читает `__version__` отсюда
(`[tool.setuptools.dynamic]`), поэтому бампать её нужно ровно в этом файле.
"""

import os

# `RetryAfter.retry_after` и другие периоды PTB - `timedelta` уже сейчас, как
# станет в следующей мажорной версии (docs PTB: opt-in через `PTB_TIMEDELTA`);
# без этого каждое чтение даёт PTBDeprecationWarning. Явное значение из
# окружения не перебиваем.
os.environ.setdefault("PTB_TIMEDELTA", "1")

__version__ = "0.1.0"
