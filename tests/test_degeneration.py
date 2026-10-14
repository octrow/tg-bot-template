"""Выродившийся ответ (спека answer-degeneration-check)."""

from types import SimpleNamespace

import httpx
import pytest

from tgbot import config, db, degeneration as d
from tgbot.agent import ladder

from .conftest import log_rows, make_update

# Куски реальных ответов s125 и s126.
S125 = (
    "Для полноценного наброска нужно дляcontracts и трафикTelephoneoperator. "
    "Такоймаterchacciatore точноsettled стeward 穹商务 покрываетисdолshritво. "
    "Это миниmally ENTсистемы бюджетамwasher账."
)
S126 = "Ответ про лимит молчания: страновCMJr универсальнаsemiатматической massiveentionской امین 商务 бюджетamи."
NORMAL = (
    "Webhook приходит в flow_engine, а звонок 3f1c2a9e-1b2c-4d5e-8f90-123456789abc "
    "уходит в voicemail-детектор. Настройка `max_silence` (сек.) лежит в docs/agent.md."
)


@pytest.mark.parametrize("text, at_least", [(S125, 5), (S126, 5)])
def test_incident_answers_are_many_places(text, at_least):
    assert len(d.suspicious_spans(text)) >= at_least


@pytest.mark.parametrize("text", ["Сработал voicemailов на звонке.", "Это описано в docе проекта."])
def test_single_glued_word_is_one_place(text):
    assert len(d.suspicious_spans(text)) == 1


def test_normal_answer_with_latin_terms_has_no_places():
    assert d.suspicious_spans(NORMAL) == []


def test_fragment_is_the_sentence_and_its_neighbours():
    filler = [f"Предложение номер {i} про настройку агента." for i in range(60)]
    filler[30] = "Здесь сработал voicemailов на звонке."
    text = " ".join(filler)
    assert len(text) > 2500

    sample = d.fragments(text, d.suspicious_spans(text))

    assert sample == " ".join(filler[29:32])


def test_fragment_without_sentence_bounds_is_cut_at_a_space():
    text = "слово " * 200 + "voicemailов " + "слово " * 200

    sample = d.fragments(text, d.suspicious_spans(text))

    assert len(sample) <= 2 * d.SIDE_LIMIT + len("voicemailов")
    assert sample.startswith("слово") and sample.endswith("слово")


def test_close_places_are_merged_and_total_is_capped():
    text = "Тут voicemailов и docе рядом. " + "Длинное предложение без мест. " * 10
    assert d.fragments(text, d.suspicious_spans(text)).count("voicemailов") == 1

    many = " ".join(f"Слово{i} cтрока." for i in range(400))  # латинская c в 'cтрока'
    assert len(d.fragments(many, d.suspicious_spans(many))) <= d.TOTAL_LIMIT


def test_config_without_key_disables_the_judge():
    assert config.load_config({"TELEGRAM_BOT_TOKEN": "t"}, env_files=()).typesafe_api_key is None
    loaded = config.load_config({"TELEGRAM_BOT_TOKEN": "t", "TYPESAFE_API_KEY": "k"}, env_files=())
    assert loaded.typesafe_api_key == "k"


def _jev(outcome):
    """Клиент Jev: число - вероятность мусора, исключение - сбой, int - HTTP-код."""

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            assert url == d.JEV_URL and json["model"] == "jev-latest"
            if isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, int):
                return httpx.Response(outcome, request=httpx.Request("POST", url))
            body = {"answers": {"verdict": {"type": "choice", "probabilities": {
                "garbage": outcome, "normal": 1 - outcome}}}}
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    return _Client


@pytest.mark.parametrize("outcome, garbage, logged", [
    (0.8, True, {"verdict": "garbage", "garbage": 0.8}),
    (0.1, False, {"verdict": "normal", "garbage": 0.1}),
    (500, False, None),
    (httpx.ReadTimeout("slow"), False, {"reason": "timeout"}),
])
async def test_judge_outcomes(tmp_path, monkeypatch, outcome, garbage, logged):
    conn = db.connect(tmp_path / "db.sqlite")
    monkeypatch.setattr(httpx, "AsyncClient", _jev(outcome))

    assert await d.judge(conn, 42, "тест", "key", "фрагмент") is garbage

    row = log_rows(conn, "llm_judge")[0]
    assert row["context"] == "тест"
    if logged is None:
        assert row["reason"].startswith("error")
    else:
        assert logged.items() <= row.items()


async def test_no_key_means_normal_without_a_call(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    monkeypatch.setattr(httpx, "AsyncClient", _jev(AssertionError("вызова быть не должно")))

    assert await d.is_degenerate("voicemailов и docе", conn=conn, user_id=42) == 0
    assert log_rows(conn, "llm_judge")[0]["reason"] == "no_key"


class _Answers:
    """OpenRouter отвечает текстом из `replies` по очереди (chat completions)."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = 0

    def client(self):
        outer = self

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, headers=None, json=None):
                outer.calls += 1
                text = outer.replies[min(outer.calls, len(outer.replies)) - 1]
                return httpx.Response(200, json={"choices": [{"message": {"content": text}}]},
                                      request=httpx.Request("POST", url))

        return _Client


def _config():
    return SimpleNamespace(
        repo_path=None,
        openrouter_api_key="sk-or", openrouter_models=("z-ai/glm-5.3-flash",),
        llm_provider="openrouter", agent_cache_ttl_s=3600, typesafe_api_key=None,
    )


async def test_degenerate_answer_is_rejected_not_cached(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    answers = _Answers([S125])
    monkeypatch.setattr(httpx, "AsyncClient", answers.client())
    message = make_update(42).effective_message

    result = await ladder.call_agent(
        _config(), conn, "вопрос", 42, "reference_question", message=message, light=True,
    )

    assert result is None
    assert all("дляcontracts" not in r for r in message.replies)
    rejected = log_rows(conn, "llm_rejected")[0]
    assert rejected["defect"] == "degenerate" and rejected["context"] == "reference_question"
    assert rejected["places"] >= 5 and rejected["head"].startswith("Для полноценного")
    assert log_rows(conn, "llm_judge") == []  # явный брак - без судьи

    await ladder.call_agent(_config(), conn, "вопрос", 42, "reference_question", light=True)

    assert answers.calls == 2 and log_rows(conn, "agent_cache_hit") == []


async def test_normal_answer_passes(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    monkeypatch.setattr(httpx, "AsyncClient", _Answers([NORMAL]).client())

    result = await ladder.call_agent(_config(), conn, "вопрос", 42, "reference_question", light=True)

    assert result == NORMAL
    assert log_rows(conn, "llm_rejected") == []


async def test_judge_logs_its_input_and_question(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "db.sqlite")
    monkeypatch.setattr(httpx, "AsyncClient", _jev(0.1))
    await d.judge(conn, 42, "тест", "key", "фрагмент")
    row = log_rows(conn, "llm_judge")[0]
    assert row["input"] == "фрагмент" and row["question"] == d.JEV_QUESTION
