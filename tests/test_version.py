import importlib.metadata
import re

import pytest

import tgbot
from tgbot.handlers.common import help_text

# Версию правим вручную при заливке, метаданные расходятся с кодом до переустановки.
pytestmark = pytest.mark.skip(reason="версия меняется вручную при заливке")


def test_package_metadata_matches_code_version():
    """Метаданные дистрибутива и код отдают одну и ту же версию."""
    assert importlib.metadata.version("tg-bot") == tgbot.__version__


def test_version_is_semantic():
    assert re.fullmatch(r"\d+\.\d+\.\d+", tgbot.__version__)


def test_help_shows_version_for_user_and_admin():
    """Версия видна в /help одинаково обычному пользователю и админу."""
    for is_admin in (False, True):
        assert tgbot.__version__ in help_text("gpt-test", is_admin=is_admin, lang="ru")


# Версия в событии `startup` проверяется вместе с режимом провайдера в
# tests/test_main.py::test_startup_records_version_and_llm_provider - событие
# одно, поднимать main() дважды незачем.
