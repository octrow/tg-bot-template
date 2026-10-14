"""Инструменты чтения агента и обзор дерева (`tgbot.agent.tools`)."""

import httpx

from tgbot import db
from tgbot.agent import openrouter, tools

from .conftest import log_rows
from .fakes.openai_client import FakeOpenAIClient, FakeOpenAIResponse, openai_payloads, responses_reply


async def test_openai_file_tools_stay_inside_allowed_roots(tmp_path):
    """Путь наружу разрешённых каталогов читать нельзя."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "secret.txt").write_text("нельзя", encoding="utf-8")
    content, files = await tools.run_tool("read_file", [docs], {"path": "../secret.txt"})

    assert "не найден" in content
    assert files == []


async def test_openai_grep_and_glob_find_files(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("строка про CSAT\nдругая строка", encoding="utf-8")
    assert "a.md" in (await tools.run_tool("glob", [docs], {"pattern": "**/*.md"}))[0]
    hit, _ = await tools.run_tool("grep", [docs], {"pattern": "CSAT", "glob": "**/*.md"})
    assert "a.md:1: строка про CSAT" in hit


async def test_glob_accepts_absolute_pattern_from_model(tmp_path):
    """Модель называет пути как хочет - на абсолютном шаблоне Path.glob падает
    ValueError, и это выглядело отказом провайдера."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "spec.md").write_text("текст", encoding="utf-8")

    content, _ = await tools.run_tool("glob", [docs], {"pattern": f"{docs}/**/*.md"})

    assert "spec.md" in content


async def test_tool_failure_does_not_break_the_answer(tmp_path):
    """Падение инструмента - наша ошибка, модель должна получить текст и
    продолжить, а не потерять рекомендацию целиком."""
    content, files = await tools.run_tool("read_file", [tmp_path], {"path": None})

    assert "не" in content.lower()
    assert files == []


async def test_overview_built_once_and_rebuilt_after_repo_update(tmp_path, monkeypatch):
    """Обзор дерева уходит в каждый запрос, но меняется только после git pull -
    строить его заново на каждое обращение к OpenAI незачем."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "spec.md").write_text("текст", encoding="utf-8")
    walks: list[str] = []
    original = tools._iter_files

    def counting_iter_files(roots, pattern):
        walks.append(pattern)
        return original(roots, pattern)

    monkeypatch.setattr(tools, "_iter_files", counting_iter_files)
    monkeypatch.setattr(httpx, "AsyncClient", FakeOpenAIClient)
    tools.reset_overview()

    await openrouter.ask_openai("вопрос", api_key="sk-x", model="m", roots=[docs])
    await openrouter.ask_openai("ещё вопрос", api_key="sk-x", model="m", roots=[docs])

    assert len(walks) == 1  # второй запрос взял готовый обзор

    tools.reset_overview()  # клон обновился
    await openrouter.ask_openai("третий вопрос", api_key="sk-x", model="m", roots=[docs])

    assert len(walks) == 2


async def test_overview_is_capped(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(tools.OVERVIEW_LIMIT + 20):
        (docs / f"f{i}.md").write_text("x", encoding="utf-8")

    lines = (await tools.overview([docs])).splitlines()

    assert len(lines) == tools.OVERVIEW_LIMIT + 1  # заголовок + лимит путей


async def test_overview_excludes_hidden_dirs_and_shows_docs(tmp_path):
    """Служебные каталоги (скрытые, начинающиеся с точки) не должны занимать
    обзор дерева - модель должна увидеть документацию задач, а не
    `.claude/skills/**`."""
    hidden = tmp_path / ".claude" / "skills"
    hidden.mkdir(parents=True)
    for i in range(5):
        (hidden / f"skill{i}.md").write_text("x", encoding="utf-8")
    docs = tmp_path / "docs" / "patches"
    docs.mkdir(parents=True)
    (docs / "patch100-foo.md").write_text("текст", encoding="utf-8")

    overview = await tools.overview([tmp_path])

    assert ".claude" not in overview
    assert "patch100-foo.md" in overview


async def test_overview_empty_when_nothing_left_after_filtering(tmp_path):
    hidden = tmp_path / ".git"
    hidden.mkdir()
    (hidden / "config").write_text("x", encoding="utf-8")

    overview = tools._build_overview([tmp_path])

    assert overview == ""


async def test_tool_budget_stops_reading(tmp_path, monkeypatch):
    """Каждый круг переотправляет историю - без потолка на прочитанное запрос
    растёт квадратично по деньгам. Снятие инструментов по бюджету - другая
    причина, чем отказ провайдера: событие `tools_stripped` и деградация
    (пункт 5) по нему не появляются (design решение 3)."""
    conn = db.connect(tmp_path / "db.sqlite")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "big.md").write_text("я" * tools.READ_MAX_CHARS * 2, encoding="utf-8")
    openai_payloads.clear()

    class _AlwaysReads(FakeOpenAIClient):
        async def post(self, url, headers=None, json=None):
            openai_payloads.append("tools" in json)
            if "tools" not in json:
                return FakeOpenAIResponse(responses_reply("ответ"))
            return FakeOpenAIResponse({
                "id": "resp-1",
                "output": [{
                    "type": "function_call", "call_id": "c",
                    "name": "read_file", "arguments": '{"path": "big.md"}',
                }],
            })

    monkeypatch.setattr(httpx, "AsyncClient", _AlwaysReads)
    monkeypatch.setattr(tools, "TOOL_BUDGET_CHARS", tools.READ_MAX_CHARS)

    answer = await openrouter.ask_openai(
        "вопрос", api_key="sk-x", model="m", conn=conn, user_id=42,
        context_label="тест", roots=[docs],
    )

    assert answer == "ответ"
    assert openai_payloads == [True, False]  # после исчерпания бюджета инструментов нет
    assert answer.tools_stripped is False
    assert log_rows(conn, "tools_stripped") == []
    assert log_rows(conn, "llm_response")[-1]["degraded"] is False


async def test_tool_read_returns_typical_archive_doc_whole(tmp_path):
    """Медиана документа архива ~18k символов - должен дойти до модели
    целиком, без обрезки."""
    docs = tmp_path / "docs"
    docs.mkdir()
    text = "я" * 18_000
    (docs / "typical.md").write_text(text, encoding="utf-8")

    content, files = await tools.run_tool("read_file", [docs], {"path": "typical.md"})

    assert content == text
    assert files == [str(docs / "typical.md")]


async def test_three_full_reads_in_a_row_do_not_exhaust_budget(tmp_path, monkeypatch):
    """Три чтения подряд по READ_MAX_CHARS символов не должны отклоняться по
    исчерпанию общего бюджета - бюджет рассчитан ровно на три таких чтения."""
    conn = db.connect(tmp_path / "db.sqlite")
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in ("one.md", "two.md", "three.md"):
        (docs / name).write_text("я" * tools.READ_MAX_CHARS, encoding="utf-8")

    names = iter(["one.md", "two.md", "three.md"])

    class _ReadsThreeThenAnswers(FakeOpenAIClient):
        async def post(self, url, headers=None, json=None):
            try:
                name = next(names)
            except StopIteration:
                return FakeOpenAIResponse(responses_reply("ответ"))
            return FakeOpenAIResponse({
                "id": "resp-1",
                "output": [{
                    "type": "function_call", "call_id": "c",
                    "name": "read_file", "arguments": f'{{"path": "{name}"}}',
                }],
            })

    monkeypatch.setattr(httpx, "AsyncClient", _ReadsThreeThenAnswers)

    answer = await openrouter.ask_openai(
        "вопрос", api_key="sk-x", model="m", conn=conn, user_id=42,
        context_label="тест", roots=[docs],
    )

    assert answer == "ответ"
    assert answer.files_read == [
        str(docs / "one.md"), str(docs / "two.md"), str(docs / "three.md")
    ]
    assert answer.tools_stripped is False
    assert log_rows(conn, "tools_stripped") == []


async def test_tool_read_marks_truncation_with_shown_and_total_size(tmp_path):
    """Усечённый файл SHALL нести пометку с числом отданных символов и полным
    размером - иначе модель принимает начало документа за весь документ."""
    docs = tmp_path / "docs"
    docs.mkdir()
    total = tools.READ_MAX_CHARS + 5_000
    (docs / "long.md").write_text("я" * total, encoding="utf-8")

    content, files = await tools.run_tool("read_file", [docs], {"path": "long.md"})

    assert content.startswith("я" * tools.READ_MAX_CHARS)
    assert str(tools.READ_MAX_CHARS) in content
    assert str(total) in content
    assert files == [str(docs / "long.md")]


async def test_tool_read_no_truncation_notice_when_file_fits(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    text = "я" * (tools.READ_MAX_CHARS - 1)
    (docs / "short.md").write_text(text, encoding="utf-8")

    content, _ = await tools.run_tool("read_file", [docs], {"path": "short.md"})

    assert content == text


async def test_tool_read_no_truncation_notice_when_file_exactly_at_ceiling(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    text = "я" * tools.READ_MAX_CHARS
    (docs / "exact.md").write_text(text, encoding="utf-8")

    content, _ = await tools.run_tool("read_file", [docs], {"path": "exact.md"})

    assert content == text


async def test_tool_read_missing_file_has_no_truncation_notice(tmp_path):
    content, files = await tools.run_tool("read_file", [tmp_path], {"path": "nope.md"})

    assert "не найден" in content
    assert "Показано" not in content
    assert files == []


def _sandbox(tmp_path):
    """root с симлинком наружу (`root/lnk -> ../secret`) и секретом рядом."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.md").write_text("открытый текст", encoding="utf-8")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "s.txt").write_text("СЕКРЕТ", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=СЕКРЕТ", encoding="utf-8")
    (root / "lnk").symlink_to("../secret")
    (root / "envlink").symlink_to("../.env")
    return root, secret


async def test_glob_does_not_leave_roots(tmp_path):
    """Шаблон с `..`, симлинк наружу и абсолютный путь вне корней не находят
    чужих файлов (спека codebase-context)."""
    root, secret = _sandbox(tmp_path)
    for pattern in ("../secret/*", "lnk/*", "*", "**/*", f"{secret}/*", "../.env", "envlink"):
        content, _ = await tools.run_tool("glob", [root], {"pattern": pattern})
        assert "s.txt" not in content, pattern
        assert ".env" not in content and "envlink" not in content, pattern


async def test_grep_does_not_leave_roots(tmp_path):
    root, secret = _sandbox(tmp_path)
    for glob in ("../secret/*", "lnk/*", "**/*", f"{secret}/*", "../.env", "envlink", "*"):
        content, _ = await tools.run_tool("grep", [root], {"pattern": "СЕКРЕТ", "glob": glob})
        assert "СЕКРЕТ" not in content, glob
    hit, _ = await tools.run_tool("grep", [root], {"pattern": "открытый"})
    assert "a.md" in hit


async def test_read_file_refuses_symlink_out_of_roots(tmp_path):
    root, _ = _sandbox(tmp_path)
    for path in ("lnk/s.txt", "../secret/s.txt", str(tmp_path / "secret" / "s.txt"), "envlink"):
        content, files = await tools.run_tool("read_file", [root], {"path": path})
        assert "СЕКРЕТ" not in content, path
        assert files == []


def _with_git(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text(
        "url = https://ghp_token@github.com/o/r", encoding="utf-8"
    )
    (root / ".env").write_text("ghp_token", encoding="utf-8")
    (root / "docs" / ".hidden").mkdir(parents=True)
    (root / "docs" / ".hidden" / "x.md").write_text("ghp_token", encoding="utf-8")
    (root / "docs" / "a.md").write_text("открыто", encoding="utf-8")
    (root / "gitlink").symlink_to(".git/config")
    return root


async def test_dot_paths_are_not_readable(tmp_path):
    """`.git/config` хранит URL клона, `.env` - ключи: точечные пути закрыты
    для всех трёх инструментов."""
    root = _with_git(tmp_path)
    for path in (".git/config", ".env", "docs/.hidden/x.md", "gitlink", "docs/../.git/config"):
        content, files = await tools.run_tool("read_file", [root], {"path": path})
        assert "ghp_token" not in content, path
        assert files == []
    for pattern in ("**/.git/*", ".git/*", ".*", "**/*", "docs/.hidden/*", "**/.*", "gitlink"):
        content, _ = await tools.run_tool("glob", [root], {"pattern": pattern})
        assert ".git" not in content and ".env" not in content, pattern
        assert ".hidden" not in content and "gitlink" not in content, pattern
        grep, _ = await tools.run_tool("grep", [root], {"pattern": "ghp", "glob": pattern})
        assert "ghp_token" not in grep, pattern
    content, files = await tools.run_tool("read_file", [root], {"path": "docs/a.md"})
    assert content == "открыто"


async def test_grep_rejects_too_long_pattern(tmp_path):
    (tmp_path / "a.md").write_text("aaa", encoding="utf-8")
    pattern = "a" * (tools.GREP_PATTERN_MAX + 1)

    content, _ = await tools.run_tool("grep", [tmp_path], {"pattern": pattern})

    assert "длинн" in content


async def test_grep_reads_only_head_of_huge_file(tmp_path):
    """Огромный файл читается только до потолка: хвост за ним не ищется."""
    big = tmp_path / "big.txt"
    with big.open("w", encoding="utf-8") as fh:
        fh.write("x\n" * (tools.GREP_FILE_MAX_BYTES // 2 + 10))
        fh.write("ХВОСТ\n")
    (tmp_path / "small.md").write_text("ХВОСТ рядом", encoding="utf-8")

    content, _ = await tools.run_tool("grep", [tmp_path], {"pattern": "ХВОСТ"})

    assert "small.md" in content
    assert "big.txt" not in content


async def test_grep_scans_limited_number_of_files(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "GREP_MAX_FILES", 3)
    for i in range(6):
        (tmp_path / f"f{i}.md").write_text("нет совпадения", encoding="utf-8")
    (tmp_path / "z.md").write_text("ИСКОМОЕ", encoding="utf-8")

    content, _ = await tools.run_tool("grep", [tmp_path], {"pattern": "ИСКОМОЕ"})

    assert "z.md" not in content
