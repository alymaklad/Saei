"""
Fitting the prompt and the answer inside a local model's context.

The failure this pins: "CV rewrite came back empty (finish_reason=unknown)" on
Ollama. Nothing raised, HTTP 200, an empty string back, and a whole run
wasted. Two causes, both invisible from the message:

  ROOM.  Ollama counts the prompt AND the generated tokens against num_ctx and
         truncates the prompt silently when they do not fit. The rewrite is the
         longest prompt in the app -- profile JSON, a whole scraped posting,
         target terms -- and it reserved 3,000 tokens of output inside a fixed
         8,192. A long posting is all it takes.

  THINKING. qwen3 and friends emit a <think> block before answering, billed
         against the same output budget. A 4B model can spend all of it
         reasoning and answer nothing.

Neither can be reproduced against a real Ollama here, so what is tested is the
arithmetic and the wiring: that the context is sized from the actual prompt,
that thinking is turned off for the models that have it, and that an empty
response is retried once and then explained with numbers rather than a list of
guesses.
"""
from unittest.mock import MagicMock, patch

import pytest

import config
from agents import cv_rewriter_agent, llm


# ---- context sizing ----------------------------------------------------------

def test_the_context_covers_the_prompt_and_the_answer(monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_NUM_CTX", 8192)
    monkeypatch.setattr(config, "OLLAMA_NUM_CTX_MAX", 16384)
    prompt = "x" * 24000                      # ~7.5k tokens
    context = llm.context_for(prompt, 3000)   # + 3k of output
    assert context > llm.estimate_tokens(prompt) + 3000
    assert context <= config.OLLAMA_NUM_CTX_MAX


def test_a_short_prompt_does_not_shrink_the_context(monkeypatch):
    """The configured value is a floor, not a target: shrinking below it would
    make short calls behave differently for no reason."""
    monkeypatch.setattr(config, "OLLAMA_NUM_CTX", 8192)
    assert llm.context_for("hello", 256) == 8192


def test_the_ceiling_is_respected(monkeypatch):
    """A pathological posting must not ask for a context that will not load."""
    monkeypatch.setattr(config, "OLLAMA_NUM_CTX_MAX", 16384)
    assert llm.context_for("x" * 500000, 3000) == 16384


def test_the_estimate_errs_high():
    """Guessing low means a silently truncated prompt; guessing high costs a
    little KV cache on a local model. The asymmetry is deliberate."""
    text = "word " * 1000          # 5000 chars, ~1000 real tokens
    assert llm.estimate_tokens(text) > 1000


def test_the_ollama_client_is_built_with_the_sized_context(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3:4b")
    monkeypatch.setattr(config, "OLLAMA_NUM_CTX", 8192)
    monkeypatch.setattr(config, "OLLAMA_NUM_CTX_MAX", 16384)
    built = {}

    class FakeChat:
        def __init__(self, **kwargs):
            built.update(kwargs)

    with patch.object(llm, "_chat_ollama_class", return_value=FakeChat):
        llm.get_llm(temperature=0.3, max_tokens=3000, prompt_text="x" * 24000)

    assert built["num_ctx"] > 8192, "a long prompt must widen the window"
    assert built["num_predict"] == 3000


# ---- thinking ----------------------------------------------------------------

@pytest.mark.parametrize("model, thinks", [
    ("qwen3:4b", True), ("qwen3:8b-q4_K_M", True), ("deepseek-r1:8b", True),
    ("llama3.1:8b", False), ("mistral:7b", False), ("nomic-embed-text", False),
])
def test_thinking_models_are_recognised_by_family(model, thinks):
    """Matched on the family because the tag carries a size and a
    quantisation."""
    assert llm._is_thinking_model(model) is thinks


def test_thinking_is_disabled_for_a_reasoning_model(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3:4b")
    monkeypatch.setattr(config, "OLLAMA_THINKING", False)
    assert llm.prepare_system("Do the thing").endswith("/no_think")


def test_the_control_token_is_not_sent_to_models_that_do_not_know_it(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_MODEL", "llama3.1:8b")
    assert llm.prepare_system("Do the thing") == "Do the thing"


def test_a_hosted_provider_never_sees_the_control_token(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "groq")
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3:4b")
    assert llm.prepare_system("Do the thing") == "Do the thing"


def test_thinking_can_be_turned_back_on(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3:4b")
    monkeypatch.setattr(config, "OLLAMA_THINKING", True)
    assert llm.prepare_system("Do the thing") == "Do the thing"


def test_the_reasoning_flag_is_only_passed_when_the_class_accepts_it(monkeypatch):
    """langchain-ollama gained `reasoning` in a later version; passing it to an
    older class is a TypeError, so the signature decides."""
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3:4b")
    monkeypatch.setattr(config, "OLLAMA_THINKING", False)

    class Old:
        def __init__(self, model=None, base_url=None, temperature=None,
                     num_ctx=None, num_predict=None):
            self.kwargs = locals()

    class New(Old):
        def __init__(self, reasoning=None, **kwargs):
            super().__init__(**kwargs)
            self.reasoning = reasoning

    with patch.object(llm, "_chat_ollama_class", return_value=Old):
        llm.get_llm()  # must not raise
    with patch.object(llm, "_chat_ollama_class", return_value=New):
        built = llm.get_llm()
    assert built.reasoning is False


# ---- the job description is the unbounded input ------------------------------

def test_a_long_posting_is_trimmed_from_the_end(monkeypatch):
    monkeypatch.setattr(config, "REWRITE_JD_MAX_CHARS", 6000)
    trimmed = cv_rewriter_agent._trim_job_description("R" * 100 + "x" * 20000)
    assert len(trimmed) < 6100
    assert trimmed.startswith("R" * 100), "the requirements are at the top"
    assert "truncated" in trimmed


def test_a_normal_posting_is_untouched(monkeypatch):
    monkeypatch.setattr(config, "REWRITE_JD_MAX_CHARS", 6000)
    posting = "We need a Python engineer. " * 50
    assert cv_rewriter_agent._trim_job_description(posting) == posting


# ---- empty responses ---------------------------------------------------------

def _empty():
    resp = MagicMock()
    resp.content = ""
    resp.response_metadata = {}
    return resp


def _full():
    resp = MagicMock()
    resp.content = ('{"summary": "ok", '
                    '"experience": [{"ref": 0, "bullets": ["Did the thing"]}], '
                    '"projects": []}')
    resp.response_metadata = {"done_reason": "stop"}
    return resp


def _reasoning():
    """What qwen3:4b actually returned on a real run: thousands of characters
    of deliberation, no CV, and not a `<think>` tag in sight."""
    resp = MagicMock()
    resp.content = (
        "We are given the candidate profile and the job description. We must "
        "tailor the CV. Steps: 1. Identify the target terms. But wait, the "
        "problem says the target terms are computed from the history. "
        "However, looking at the problem statement again... " * 40)
    resp.response_metadata = {"done_reason": "length"}
    return resp


def test_an_empty_response_is_retried_once_before_failing():
    """A wasted rewrite is slow on a local model and metered on a hosted one,
    so one retry is worth it -- with a blunter instruction, since the first
    attempt's phrasing is what did not work."""
    calls = []

    def fake_get_llm(temperature=0.0, max_tokens=None, prompt_text=""):
        calls.append(prompt_text)
        client = MagicMock()
        client.invoke.return_value = _full() if len(calls) == 2 else _empty()
        return client

    with patch.object(cv_rewriter_agent, "get_llm", side_effect=fake_get_llm):
        document = cv_rewriter_agent._ask_for_document("prompt")

    assert document["experience"], "the contract is a parsed document"
    assert len(calls) == 2
    assert "JSON object only" in calls[1], "the retry says it more bluntly"


def test_reasoning_instead_of_a_cv_is_not_accepted_as_one():
    """The bug this guard exists for. 13,000 characters of "We are given the
    candidate profile... but wait" parsed as no JSON, the old code fell through
    to `return content`, and the model's monologue was rendered into a
    tailored-CV PDF and scored."""
    client = MagicMock()
    client.invoke.return_value = _reasoning()
    with patch.object(cv_rewriter_agent, "get_llm", return_value=client), \
         pytest.raises(RuntimeError) as excinfo:
        cv_rewriter_agent._ask_for_document("prompt")

    message = str(excinfo.value)
    assert "not a CV" in message
    assert "We are given" in message, "quote what came back so it is diagnosable"


def test_a_reasoning_first_answer_is_still_usable(monkeypatch):
    """A model that thinks in tags and THEN answers has answered. The document
    is in there; only the reasoning has to come off."""
    resp = MagicMock()
    resp.content = ("<think>The candidate has FastAPI experience, so...</think>\n"
                    '{"summary": "ok", "experience": [{"ref": 0, "bullets": ["x"]}]}')
    resp.response_metadata = {}
    client = MagicMock()
    client.invoke.return_value = resp
    with patch.object(cv_rewriter_agent, "get_llm", return_value=client):
        document = cv_rewriter_agent._ask_for_document("prompt")
    assert document["experience"][0]["bullets"] == ["x"]


def test_a_truncated_thought_counts_as_nothing(monkeypatch):
    """An unclosed <think> means the budget ran out mid-deliberation: there is
    no answer after it, and treating the reasoning as content would put it in
    the PDF."""
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    resp = MagicMock()
    resp.content = "<think>Let me consider the candidate's experience with"
    resp.response_metadata = {"done_reason": "length"}
    client = MagicMock()
    client.invoke.return_value = resp
    with patch.object(cv_rewriter_agent, "get_llm", return_value=client), \
         pytest.raises(RuntimeError) as excinfo:
        cv_rewriter_agent._ask_for_document("prompt")
    assert "context" in str(excinfo.value), "an empty answer is the room diagnostic"


def test_two_empty_responses_raise_with_the_numbers(monkeypatch):
    """The old message listed three possibilities and left the reader to guess.
    These are the values that tell them apart."""
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3:4b")

    client = MagicMock()
    client.invoke.return_value = _empty()
    with patch.object(cv_rewriter_agent, "get_llm", return_value=client), \
         pytest.raises(RuntimeError) as excinfo:
        cv_rewriter_agent._ask_for_document("x" * 12000)

    message = str(excinfo.value)
    assert "qwen3:4b" in message
    assert "OLLAMA_NUM_CTX_MAX" in message and "REWRITE_JD_MAX_CHARS" in message
    assert "tokens" in message and "context" in message


def test_the_thinking_advice_appears_only_when_thinking_is_on(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3:4b")

    monkeypatch.setattr(config, "OLLAMA_THINKING", False)
    off = cv_rewriter_agent._empty_response_message(_empty(), "prompt", 3000)
    monkeypatch.setattr(config, "OLLAMA_THINKING", True)
    on = cv_rewriter_agent._empty_response_message(_empty(), "prompt", 3000)

    assert "OLLAMA_THINKING" in on
    assert "OLLAMA_THINKING" not in off, (
        "advice to turn off something already off is noise")


def test_a_hosted_provider_gets_hosted_advice(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "groq")
    message = cv_rewriter_agent._empty_response_message(_empty(), "prompt", 3000)
    assert "quota" in message
    assert "OLLAMA_NUM_CTX_MAX" not in message


def test_ollamas_own_stop_reason_is_reported(monkeypatch):
    """Ollama reports `done_reason`, not `finish_reason` -- reading only the
    latter is why the original error said "finish_reason=unknown" and told the
    user nothing at all."""
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    resp = _empty()
    resp.response_metadata = {"done_reason": "length"}
    assert "length" in cv_rewriter_agent._empty_response_message(resp, "p", 3000)


# ---- reasoning is stripped, everywhere -----------------------------------------

def test_visible_content_strips_a_closed_think_block():
    resp = MagicMock()
    resp.content = "<think>deliberating</think>\n{\"ok\": true}"
    assert llm.visible_content(resp) == '{"ok": true}'


def test_visible_content_returns_nothing_for_an_unclosed_thought():
    resp = MagicMock()
    resp.content = "<think>deliberating and never finishing"
    assert llm.visible_content(resp) == ""


def test_the_user_turn_carries_the_switch_too(monkeypatch):
    """Which conversation turn Qwen3 honours /no_think in depends on the chat
    template a build ships, so it goes on both."""
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3:4b")
    monkeypatch.setattr(config, "OLLAMA_THINKING", False)
    assert llm.no_think_prefix().startswith("/no_think")

    monkeypatch.setattr(config, "OLLAMA_MODEL", "llama3.1:8b")
    assert llm.no_think_prefix() == ""


# ---- a zero score must never stand in for a failed extraction ------------------

def test_no_requirements_from_a_real_posting_raises(monkeypatch):
    """The screenshot this came from: 0% Critical, and every bucket labelled
    "not applicable to this posting" — for a posting that plainly asked for
    things. The model had answered the extraction call with its own reasoning,
    the JSON parse produced nothing, and a confident wrong zero came out four
    layers downstream. Fail where the failure is."""
    from agents import ats_agent

    resp = MagicMock()
    resp.content = "We are given a job description. Let me think about what it asks for..."
    resp.response_metadata = {}
    client = MagicMock()
    client.invoke.return_value = resp

    posting = "We are hiring a Python engineer with FastAPI and PostgreSQL. " * 8
    with patch.object(ats_agent, "get_llm", return_value=client), \
         pytest.raises(RuntimeError) as excinfo:
        ats_agent.extract_requirements(posting)

    message = str(excinfo.value)
    assert "no requirements" in message
    assert str(len(posting.strip())) in message, "say how big the posting was"


def test_a_genuinely_empty_posting_does_not_raise():
    """The guard is about a failed parse, not about a caller passing nothing."""
    from agents import ats_agent

    resp = MagicMock()
    resp.content = '{"requirements": [], "years_experience_required": null, "seniority": null}'
    resp.response_metadata = {}
    client = MagicMock()
    client.invoke.return_value = resp

    with patch.object(ats_agent, "get_llm", return_value=client):
        result = ats_agent.extract_requirements("")
    assert result["requirements"] == []


def test_extraction_reads_past_a_think_block():
    """A model that reasons in tags and then answers has answered."""
    from agents import ats_agent

    resp = MagicMock()
    resp.content = ('<think>The posting asks for Python and FastAPI.</think>\n'
                    '{"requirements": [{"name": "Python", '
                    '"category": "technical_skill", "importance": "required"}], '
                    '"years_experience_required": 3, "seniority": "mid"}')
    resp.response_metadata = {}
    client = MagicMock()
    client.invoke.return_value = resp

    with patch.object(ats_agent, "get_llm", return_value=client):
        result = ats_agent.extract_requirements("Hiring a Python engineer. " * 20)
    assert [r["name"] for r in result["requirements"]] == ["Python"]
    assert result["years_experience_required"] == 3
