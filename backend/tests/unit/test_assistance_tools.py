"""The inference arm's tools in assistance (inference-pipeline#224).

The transport: an `oai/` call with tools goes to OpenAI's Responses API, an
`openrouter/` one stays a chat completion with OpenRouter's web search server
tool, and a call without tools is the chat completion it always was. The
methods: the model under test gets its entry's tools on every call it makes,
and the confidence and clustering instruments never get any.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
import pytest
from openai.types.responses import Response

from config import LLMSettings, get_settings
from models import Question, StepType
from services.assistance.llm import EmptyTruncatedReplyError, FailedResponseError, complete
from services.assistance.methods.human_as_a_tool import HumanAsAToolMethod
from services.assistance.methods.top_n import (
    TopNAssistance,
    _SCHEMA_ACCEPTED_MODELS,
    _SCHEMA_PROBE_LOCKS,
    _SCHEMA_REJECTED_MODELS,
    _top_n_response_format,
)
from services.assistance.model_resolution import AssistanceModel
from services.assistance.registry import record_role_models

_MESSAGES = [{"role": "user", "content": "hi"}]

_SCHEMA = {
    "type": "json_schema",
    "json_schema": {"name": "top_n_candidates", "strict": True, "schema": {"type": "object"}},
}

# An entry giving the model under test both tools, and the same without.
_TOOLED = AssistanceModel(
    "oai/test/tooled", reasoning_effort="low", tools=("web_search", "code_interpreter")
)
_PLAIN = replace(_TOOLED, tools=())
_OPENAI_TOOLS = [
    {"type": "web_search"},
    {"type": "code_interpreter", "container": {"type": "auto"}},
]


@pytest.fixture(autouse=True)
def _clear_top_n_schema_memo():
    def _reset():
        _SCHEMA_REJECTED_MODELS.clear()
        _SCHEMA_ACCEPTED_MODELS.clear()
        _SCHEMA_PROBE_LOCKS.clear()

    _reset()
    yield
    _reset()


@pytest.fixture
def keys(monkeypatch):
    llm = get_settings().llm
    monkeypatch.setattr(llm, "oai_api_key", "sk-oai-test")
    monkeypatch.setattr(llm, "openrouter_api_key", "sk-or-test")


def _settings() -> LLMSettings:
    return LLMSettings(openrouter_api_key="sk-or-test", oai_api_key="sk-oai-test")


def _question() -> Question:
    return Question(
        id=1,
        experiment_id=1,
        question_id="q1",
        question_text="Q?",
        options="A|B",
        question_type="MC",
    )


def _message(text: str, item_id: str = "msg_1") -> dict:
    return {
        "type": "message",
        "id": item_id,
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


_WEB_SEARCH_CALL = {
    "type": "web_search_call",
    "id": "ws_1",
    "status": "completed",
    "action": {"type": "search", "query": "q"},
}
_CODE_CALL = {
    "type": "code_interpreter_call",
    "id": "ci_1",
    "code": "print(1)",
    "container_id": "cntr_1",
    "outputs": [{"type": "logs", "logs": "1"}],
    "status": "completed",
}
_REASONING = {"type": "reasoning", "id": "rs_1", "summary": []}
_CUT_OFF = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
_FAILED = {"status": "failed", "error": {"code": "server_error", "message": "boom"}}


def _response(output: list[dict], **fields) -> Response:
    """A Responses API result as the SDK parses one off the wire."""
    return Response.model_validate(
        {
            "id": "resp_1",
            "object": "response",
            "created_at": 0,
            "model": "test/tooled",
            "output": output,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "status": "completed",
            **fields,
        }
    )


def _chat(content: str = "ok") -> SimpleNamespace:
    choice = SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")
    return SimpleNamespace(choices=[choice])


def _client(response: Response | None = None, chat: str = "ok") -> MagicMock:
    """A client answering both APIs, so a test sees which one each call used."""
    client = MagicMock()
    client.responses.create = AsyncMock(return_value=response or _response([_message("ok")]))
    client.chat.completions.create = AsyncMock(return_value=_chat(chat))
    return client


async def _complete(client: MagicMock, **kwargs) -> str:
    with patch("services.assistance.llm._get_client", return_value=client):
        return await complete(_MESSAGES, settings=_settings(), **kwargs)


def _api_status_error(status: int) -> openai.APIStatusError:
    response = httpx.Response(status, request=httpx.Request("POST", "https://example"))
    return openai.BadRequestError("rejected", response=response, body=None)


# --- The transport ----------------------------------------------------------


@pytest.mark.asyncio
async def test_oai_with_tools_calls_the_responses_api_with_exactly_this():
    """The pipeline answerer's request: the messages as the input, OpenAI's
    hosted tools, the same output cap, the options in the Responses API's
    spelling, and a chat `response_format` as `text.format`."""
    client = _client()
    text = await _complete(
        client,
        model="oai/gpt-5.6-luna",
        tools=("web_search", "code_interpreter"),
        reasoning_effort="low",
        text_verbosity="medium",
        temperature=0.5,
        response_format=_SCHEMA,
    )
    assert text == "ok"
    client.chat.completions.create.assert_not_called()
    assert client.responses.create.call_args.kwargs == {
        "model": "gpt-5.6-luna",
        "input": _MESSAGES,
        "tools": _OPENAI_TOOLS,
        "max_output_tokens": 4096,
        "store": False,
        "temperature": 0.5,
        "reasoning": {"effort": "low"},
        "text": {
            "verbosity": "medium",
            "format": {
                "type": "json_schema",
                "name": "top_n_candidates",
                "strict": True,
                "schema": {"type": "object"},
            },
        },
    }


@pytest.mark.asyncio
async def test_oai_with_tools_sends_no_option_it_was_not_given():
    client = _client()
    await _complete(client, model="oai/gpt-5.6-luna", tools=["web_search"])
    assert client.responses.create.call_args.kwargs == {
        "model": "gpt-5.6-luna",
        "input": _MESSAGES,
        "tools": [{"type": "web_search"}],
        "max_output_tokens": 4096,
        "store": False,
    }


@pytest.mark.asyncio
async def test_a_json_object_format_keeps_its_shape_on_the_responses_api():
    client = _client()
    await _complete(
        client,
        model="oai/gpt-5.6-luna",
        tools=["code_interpreter"],
        response_format={"type": "json_object"},
    )
    assert client.responses.create.call_args.kwargs["text"] == {"format": {"type": "json_object"}}


@pytest.mark.asyncio
async def test_openrouter_web_search_stays_a_chat_completion_with_its_server_tool():
    """OpenRouter's `openrouter:web_search` server tool, as the pipeline's
    answerer sends it, beside the options and the structured output request.
    No `provider` block: #142 dropped `require_parameters`."""
    client = _client()
    await _complete(
        client,
        model="openrouter/openai/gpt-6-luna",
        tools=["web_search"],
        reasoning_effort="low",
        temperature=0,
        response_format=_SCHEMA,
    )
    client.responses.create.assert_not_called()
    assert client.chat.completions.create.call_args.kwargs == {
        "model": "openai/gpt-6-luna",
        "messages": _MESSAGES,
        "max_tokens": 4096,
        "response_format": _SCHEMA,
        "temperature": 0,
        "extra_body": {
            "reasoning": {"effort": "low"},
            "tools": [{"type": "openrouter:web_search"}],
        },
    }


@pytest.mark.asyncio
async def test_openrouter_web_search_alone_sends_only_the_server_tool():
    client = _client()
    await _complete(client, model="openrouter/openai/gpt-6-luna", tools=["web_search"])
    assert client.chat.completions.create.call_args.kwargs == {
        "model": "openai/gpt-6-luna",
        "messages": _MESSAGES,
        "max_tokens": 4096,
        "extra_body": {"tools": [{"type": "openrouter:web_search"}]},
    }


@pytest.mark.asyncio
async def test_openrouter_refuses_code_interpreter_before_calling():
    """Validation refuses it at write; a call never runs the entry without it."""
    client = _client()
    with pytest.raises(ValueError, match="OpenRouter can only do web search; use an oai/ model"):
        await _complete(
            client,
            model="openrouter/openai/gpt-6-luna",
            tools=["web_search", "code_interpreter"],
        )
    client.chat.completions.create.assert_not_called()


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (
            "oai/gpt-5.6-luna",
            {
                "model": "gpt-5.6-luna",
                "messages": _MESSAGES,
                "max_completion_tokens": 4096,
                "response_format": _SCHEMA,
                "temperature": 0,
                "reasoning_effort": "low",
            },
        ),
        (
            "openrouter/openai/gpt-6-luna",
            {
                "model": "openai/gpt-6-luna",
                "messages": _MESSAGES,
                "max_tokens": 4096,
                "response_format": _SCHEMA,
                "temperature": 0,
                "extra_body": {"reasoning": {"effort": "low"}},
            },
        ),
    ],
)
@pytest.mark.parametrize("no_tools", [{}, {"tools": ()}], ids=["omitted", "empty"])
@pytest.mark.asyncio
async def test_without_tools_both_providers_send_the_chat_completion_they_always_did(
    model, expected, no_tools
):
    client = _client()
    with patch("services.assistance.llm._get_client", return_value=client) as get_client:
        await complete(
            _MESSAGES,
            settings=_settings(),
            model=model,
            reasoning_effort="low",
            temperature=0,
            response_format=_SCHEMA,
            **no_tools,
        )
    client.responses.create.assert_not_called()
    assert client.chat.completions.create.call_args.kwargs == expected
    # The chat timeout, not the tool one.
    assert get_client.call_args.args[2:] == (60, 2)


@pytest.mark.parametrize("model", ["oai/gpt-5.6-luna", "openrouter/openai/gpt-6-luna"])
@pytest.mark.asyncio
async def test_a_call_with_tools_gets_the_longer_tool_timeout(model):
    """It runs its searches or code before it answers, and sends nothing until then."""
    with patch("services.assistance.llm._get_client", return_value=_client()) as get_client:
        await complete(_MESSAGES, settings=_settings(), model=model, tools=["web_search"])
    assert get_client.call_args.args[2:] == (120, 2)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ([_message("answer")], "answer"),
        # The reasoning and the tool calls are not the reply.
        ([_REASONING, _WEB_SEARCH_CALL, _CODE_CALL, _message("answer")], "answer"),
        # A message written before a tool call is not the answer: the last one is.
        ([_message("Searching first."), _WEB_SEARCH_CALL, _message("answer", "msg_2")], "answer"),
        # Completed with no message: an empty reply, as on chat completions.
        ([_REASONING], ""),
    ],
    ids=["message", "after-tool-calls", "last-message", "no-message"],
)
@pytest.mark.asyncio
async def test_the_reply_is_the_last_messages_text(output, expected):
    text = await _complete(_client(_response(output)), model="oai/x", tools=["web_search"])
    assert text == expected


@pytest.mark.parametrize(
    "output",
    [
        [_REASONING, _WEB_SEARCH_CALL],
        # Cut off after a message, mid tool call: that message was a preamble.
        [_message("I'll search first."), _WEB_SEARCH_CALL, _REASONING],
    ],
    ids=["no-message", "after-a-preamble"],
)
@pytest.mark.asyncio
async def test_a_reply_cut_off_at_the_cap_with_no_text_is_an_empty_truncated_reply(output):
    response = _response(output, **_CUT_OFF)
    with pytest.raises(
        EmptyTruncatedReplyError,
        match=r"^LLM reply from oai/x hit max_output_tokens=4096 with no content; ",
    ):
        await _complete(_client(response), model="oai/x", tools=["web_search"])


@pytest.mark.asyncio
async def test_a_reply_cut_off_with_some_text_is_returned_for_the_caller_to_parse():
    response = _response([_message('{"candidates": [')], **_CUT_OFF)
    text = await _complete(_client(response), model="oai/x", tools=["web_search"])
    assert text == '{"candidates": ['


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        (
            _response([], status="incomplete", incomplete_details={"reason": "content_filter"}),
            "LLM response from oai/x is incomplete (content_filter) with no content",
        ),
        # A failed response's partial text is not a reply.
        (_response([_message("partial")], **_FAILED), "LLM response from oai/x failed: "),
    ],
    ids=["content-filter", "failed"],
)
@pytest.mark.asyncio
async def test_a_failed_or_filtered_response_is_a_provider_error(response, detail):
    with pytest.raises(FailedResponseError) as info:
        await _complete(_client(response), model="oai/x", tools=["web_search"])
    assert str(info.value).startswith(detail)
    # The methods' provider-error handlers catch RuntimeError.
    assert isinstance(info.value, RuntimeError)


# --- Top-N --------------------------------------------------------------------

_CANDIDATES = json.dumps({"candidates": [{"option_index": 1, "confidence": 80, "rationale": "r"}]})


def _top_n(entry: AssistanceModel) -> dict:
    return {"assistance_models": {"top_n": entry.to_dict()}}


@pytest.mark.asyncio
async def test_top_n_runs_its_entrys_tools_on_the_responses_api_with_its_schema(keys):
    client = _client(_response([_WEB_SEARCH_CALL, _message(_CANDIDATES)]))
    with patch("services.assistance.llm._get_client", return_value=client):
        step = await TopNAssistance().start(_question(), _top_n(_TOOLED))

    assert step.type == StepType.DISPLAY
    client.chat.completions.create.assert_not_called()
    sent = client.responses.create.call_args.kwargs
    assert sent["tools"] == _OPENAI_TOOLS
    assert sent["reasoning"] == {"effort": "low"}
    schema = _top_n_response_format(multiple_choice=True, n=2, option_count=2)["json_schema"]
    assert sent["text"] == {"format": {"type": "json_schema", **schema}}
    assert _SCHEMA_ACCEPTED_MODELS == {_TOOLED}


@pytest.mark.asyncio
async def test_top_n_keeps_its_schema_fallback_and_memo_per_entry_with_tools():
    """A refused schema is retried unconstrained, still with the tools, and
    remembered for the entry with tools: the same model without them has its
    own verdict."""
    llm = AsyncMock(side_effect=[_api_status_error(400), _CANDIDATES, _CANDIDATES, _CANDIDATES])
    with patch("services.assistance.methods.top_n.complete", new=llm):
        first = await TopNAssistance().start(_question(), _top_n(_TOOLED))
        second = await TopNAssistance().start(_question(), _top_n(_TOOLED))
        plain = await TopNAssistance().start(_question(), _top_n(_PLAIN))

    assert [step.type for step in (first, second, plain)] == [StepType.DISPLAY] * 3
    calls = [call.kwargs for call in llm.call_args_list]
    assert [call["tools"] for call in calls] == [_TOOLED.tools] * 3 + [()]
    assert ["response_format" in call for call in calls] == [True, False, False, True]
    assert _SCHEMA_REJECTED_MODELS == {_TOOLED}
    assert _SCHEMA_ACCEPTED_MODELS == {_PLAIN}


def _no_endpoints() -> SimpleNamespace:
    """OpenRouter's 200 with no choices: no endpoint takes these parameters."""
    return SimpleNamespace(choices=[], error={"message": "No endpoints found", "code": 404})


@pytest.mark.parametrize(
    ("entry", "api", "refusal", "schema_key", "sent_tools"),
    [
        (_TOOLED, "responses", _api_status_error(400), "text", _OPENAI_TOOLS),
        (
            AssistanceModel("openrouter/test/tooled", tools=("web_search",)),
            "chat",
            _no_endpoints(),
            "response_format",
            [{"type": "openrouter:web_search"}],
        ),
    ],
    ids=["oai", "openrouter"],
)
@pytest.mark.asyncio
async def test_top_n_falls_back_through_the_transport_with_its_tools(
    keys, entry, api, refusal, schema_key, sent_tools
):
    """The provider refuses the schema with the tools on; the unconstrained
    retry keeps the tools, and the verdict is remembered for the entry."""
    reply = (
        _response([_WEB_SEARCH_CALL, _message(_CANDIDATES)])
        if api == "responses"
        else _chat(_CANDIDATES)
    )
    client = _client()
    create = client.responses.create if api == "responses" else client.chat.completions.create
    create.side_effect = [refusal, reply]
    with patch("services.assistance.llm._get_client", return_value=client):
        step = await TopNAssistance().start(_question(), _top_n(entry))

    assert step.type == StepType.DISPLAY
    calls = [call.kwargs for call in create.call_args_list]
    assert [schema_key in call for call in calls] == [True, False]
    tools = [call["tools"] if api == "responses" else call["extra_body"]["tools"] for call in calls]
    assert tools == [sent_tools] * 2
    assert _SCHEMA_REJECTED_MODELS == {entry}


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        (
            _response([_REASONING], **_CUT_OFF),
            "EmptyTruncatedReplyError: LLM reply from oai/test/tooled hit "
            "max_output_tokens=4096 with no content; reasoning or tool calls may have used "
            "the whole budget",
        ),
        (
            _response([], **_FAILED),
            "FailedResponseError: LLM response from oai/test/tooled failed: server_error: boom",
        ),
    ],
    ids=["cut-off", "failed"],
)
@pytest.mark.asyncio
async def test_a_failed_responses_call_is_a_provider_error_with_its_detail(keys, response, detail):
    client = _client(response)
    with patch("services.assistance.llm._get_client", return_value=client):
        top_n = await TopNAssistance().start(_question(), _top_n(_TOOLED))
        haat = await HumanAsAToolMethod().start(
            _question(), {"assistance_models": {"human_as_a_tool": _TOOLED.to_dict()}}
        )

    for step in (top_n, haat):
        assert step.type == StepType.NONE
        assert step.failure_reason == "provider_error"
        assert step.failure_detail == detail


# --- Human-as-a-Tool ------------------------------------------------------------

_DECOMPOSITION = json.dumps(
    {
        "done": False,
        "evaluation_objective": "o",
        "subtasks": [
            {
                "index": 0,
                "question": "Is it A?",
                "type": "binary",
                "options": None,
                "my_answer": "yes",
                "my_answer_index": None,
                "evidence": "",
            }
        ],
    }
)


def _tools_sent(call) -> list | None:
    """The tools a call was sent, on either API."""
    return call.kwargs.get("tools") or call.kwargs.get("extra_body", {}).get("tools")


@pytest.mark.parametrize(
    ("entry", "decomposition_tools"),
    [
        (_TOOLED, _OPENAI_TOOLS),
        (
            AssistanceModel("openrouter/test/tooled", tools=("web_search",)),
            [{"type": "openrouter:web_search"}],
        ),
    ],
    ids=["oai", "openrouter"],
)
@pytest.mark.parametrize("confidence_method", ["self_report", "self_consistency", "sampling"])
@pytest.mark.asyncio
async def test_only_the_model_under_test_gets_tools(
    keys, entry, decomposition_tools, confidence_method
):
    """Decomposition runs the entry with its tools. Confidence scoring and
    clustering run the same model with none, whatever the entry says; the
    session records exactly that."""
    params = {
        "assistance_models": {"human_as_a_tool": entry.to_dict()},
        "confidence_method": confidence_method,
        "num_samples": 2,
    }

    async def chat(**kwargs):
        searching = "tools" in kwargs.get("extra_body", {})
        return _chat(_DECOMPOSITION if searching else "ANSWER: yes\nCONFIDENCE: 80")

    client = _client(_response([_WEB_SEARCH_CALL, _message(_DECOMPOSITION)]))
    client.chat.completions.create.side_effect = chat
    with patch("services.assistance.llm._get_client", return_value=client):
        step = await HumanAsAToolMethod().start(_question(), params)

    assert step.type == StepType.ASK_INPUT
    calls = [
        *client.responses.create.call_args_list,
        *client.chat.completions.create.call_args_list,
    ]
    instruments = {"self_report": 1, "self_consistency": 2, "sampling": 3}[confidence_method]
    assert [_tools_sent(call) for call in calls] == [decomposition_tools] + [None] * instruments
    assert {call.kwargs["model"] for call in calls} == {"test/tooled"}

    recorded = record_role_models("human_as_a_tool", params)["role_models"]
    assert {role: sent["tools"] for role, sent in recorded.items()} == {
        "decomposition": list(entry.tools),
        **dict.fromkeys(list(recorded)[1:], []),
    }


@pytest.mark.asyncio
async def test_advance_and_its_fallback_synthesis_send_the_pinned_entrys_tools():
    """The session pins the entry at start; later rounds run it, tools included."""
    state = {
        "question_text": "Q?",
        "options": "A|B",
        "iteration": 1,
        "max_rounds": 5,
        "subtasks": [],
        "history": [],
        "model": _TOOLED.to_dict(),
    }
    # Done without a synthesis answer: the decomposer asks once more for one.
    llm = AsyncMock(side_effect=['{"done": true}', '{"answer": "A", "reasoning": "r"}'])
    with patch("services.assistance.methods.human_as_a_tool.decomposer.complete", new=llm):
        step = await HumanAsAToolMethod().advance(state, "{}", {})

    assert step.type == StepType.COMPLETE
    assert [call.kwargs["tools"] for call in llm.call_args_list] == [_TOOLED.tools] * 2
    assert [call.kwargs["model"] for call in llm.call_args_list] == [_TOOLED.model] * 2
