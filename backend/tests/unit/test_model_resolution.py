"""What each assistance method runs on and with (`resolve_assistance_model`)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from config import get_settings
from models import Question
from services.assistance.methods.human_as_a_tool import HumanAsAToolMethod
from services.assistance.methods.top_n import TopNAssistance
from services.assistance.model_resolution import AssistanceModel, resolve_assistance_model
from services.assistance.registry import resolved_models

# Sentinels, not real models: a map entry equal to a settings default would
# let a call site that ignores the map pass anyway.
_TOP_N = AssistanceModel(
    model="openai/test/top-n-entry", reasoning_effort="low", text_verbosity="low"
)
_HAAT = AssistanceModel(model="openrouter/test/human-as-a-tool-entry", temperature=0.7)
_SESSION = AssistanceModel(model="openrouter/test/session-state", temperature=1)
_MAP = {"assistance_models": {"top_n": _TOP_N.to_dict(), "human_as_a_tool": _HAAT.to_dict()}}
_DEFAULT = AssistanceModel(model="openrouter/default", temperature=0)


def test_sentinels_differ_from_every_settings_default():
    llm = get_settings().llm
    defaults = {llm.default_model, llm.decomposition_model, llm.confidence_model}
    assert defaults.isdisjoint({_TOP_N.model, _HAAT.model, _SESSION.model})


def test_each_method_resolves_its_own_entry():
    assert resolve_assistance_model(_MAP, "top_n", _DEFAULT) == _TOP_N
    assert resolve_assistance_model(_MAP, "human_as_a_tool", _DEFAULT) == _HAAT


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"assistance_models": {"human_as_a_tool": _HAAT.to_dict()}},
        {"assistance_models": {"top_n": None}},
        {"assistance_models": {"top_n": {}}},
        {"assistance_models": {"top_n": {"model": ""}}},
        {"assistance_models": None},
    ],
)
def test_it_falls_back_to_the_default(params):
    assert resolve_assistance_model(params, "top_n", _DEFAULT) == _DEFAULT


def test_a_leftover_model_key_is_ignored():
    """`model` was removed; an old session snapshot may still carry it."""
    assert resolve_assistance_model({"model": _SESSION.model}, "top_n", _DEFAULT) == _DEFAULT
    assert resolve_assistance_model({**_MAP, "model": _SESSION.model}, "top_n", _DEFAULT) == _TOP_N


def test_a_legacy_string_entry_keeps_the_default_options():
    """A row or session written before entries carried options ran on those."""
    params = {"assistance_models": {"top_n": "openrouter/test/legacy"}}
    assert resolve_assistance_model(params, "top_n", _DEFAULT) == AssistanceModel(
        model="openrouter/test/legacy", temperature=0
    )


def test_an_entry_missing_a_key_takes_that_key_from_the_default():
    entry = {"model": "openrouter/test/partial", "reasoning_effort": "high"}
    params = {"assistance_models": {"top_n": entry}}
    assert resolve_assistance_model(params, "top_n", _DEFAULT) == AssistanceModel(
        model="openrouter/test/partial", reasoning_effort="high", temperature=0
    )


def test_to_dict_round_trips_through_json():
    assert AssistanceModel.from_entry(json.loads(json.dumps(_TOP_N.to_dict())), _DEFAULT) == _TOP_N


def test_resolved_models_gives_every_assisted_method_its_entry_and_source():
    llm = get_settings().llm
    params = {"assistance_models": {"top_n": _TOP_N.to_dict(), "human_as_a_tool": None}}
    assert resolved_models(params) == {
        "human_as_a_tool": (AssistanceModel(model=llm.decomposition_model), "default"),
        "top_n": (_TOP_N, "assistance_models"),
    }


def test_the_platform_defaults_spell_out_what_each_method_always_sent():
    """Top-N ranked at temperature 0; Human-as-a-Tool sent no options."""
    llm = get_settings().llm
    assert resolved_models({}) == {
        "human_as_a_tool": (AssistanceModel(model=llm.decomposition_model), "default"),
        "top_n": (AssistanceModel(model=llm.default_model, temperature=0), "default"),
    }


def test_an_entry_equal_to_the_default_still_reports_the_map():
    default = resolved_models({})["top_n"][0]
    resolved = resolved_models({"assistance_models": {"top_n": default.to_dict()}})
    assert resolved["top_n"] == (default, "assistance_models")


def _question() -> Question:
    return Question(
        id=1,
        experiment_id=1,
        question_id="q1",
        question_text="Q?",
        options="A|B",
        question_type="MC",
    )


@pytest.mark.asyncio
async def test_top_n_start_uses_its_entry():
    llm = AsyncMock(side_effect=RuntimeError("stop"))
    with patch("services.assistance.methods.top_n._complete_with_schema_fallback", new=llm):
        await TopNAssistance().start(_question(), _MAP)
    assert llm.call_args.kwargs["entry"] == _TOP_N


@pytest.mark.asyncio
async def test_top_n_sends_its_entry_options_to_the_client():
    complete = AsyncMock(return_value='{"candidates": [{"option_index": 1, "confidence": 80}]}')
    with patch("services.assistance.methods.top_n.complete", new=complete):
        step = await TopNAssistance().start(_question(), _MAP)
    assert step.payload["parse_status"] == "clean"
    sent = complete.call_args.kwargs
    assert sent["model"] == _TOP_N.model
    assert sent["reasoning_effort"] == "low"
    assert sent["text_verbosity"] == "low"
    assert sent["temperature"] is None


@pytest.mark.asyncio
async def test_top_n_records_what_the_provider_said_when_it_fails():
    llm = AsyncMock(side_effect=RuntimeError("Unsupported parameter: temperature"))
    with patch("services.assistance.methods.top_n._complete_with_schema_fallback", new=llm):
        step = await TopNAssistance().start(_question(), _MAP)
    assert step.failure_reason == "provider_error"
    assert step.failure_detail == "RuntimeError: Unsupported parameter: temperature"


@pytest.mark.asyncio
async def test_human_as_a_tool_start_uses_its_entry():
    method = HumanAsAToolMethod()
    method._decomposer.start = AsyncMock(side_effect=RuntimeError("stop"))
    await method.start(_question(), _MAP)
    assert method._decomposer.start.call_args.args[3] == _HAAT


@pytest.mark.parametrize(
    ("state_model", "expected"),
    [
        (None, _HAAT),
        (_SESSION.to_dict(), _SESSION),
        # A session started before entries carried options.
        (_SESSION.model, AssistanceModel(model=_SESSION.model)),
    ],
)
@pytest.mark.asyncio
async def test_human_as_a_tool_advance_prefers_the_session_model(state_model, expected):
    method = HumanAsAToolMethod()
    method._decomposer.advance = AsyncMock(side_effect=RuntimeError("stop"))
    await method.advance({"model": state_model}, "{}", _MAP)
    assert method._decomposer.advance.call_args.kwargs["model"] == expected


@pytest.mark.asyncio
async def test_without_params_methods_keep_their_defaults():
    llm = AsyncMock(side_effect=RuntimeError("stop"))
    with patch("services.assistance.methods.top_n._complete_with_schema_fallback", new=llm):
        await TopNAssistance().start(_question(), {})
    method = HumanAsAToolMethod()
    method._decomposer.start = AsyncMock(side_effect=RuntimeError("stop"))
    await method.start(_question(), {})

    settings = get_settings().llm
    assert llm.call_args.kwargs["entry"] == AssistanceModel(
        model=settings.default_model, temperature=0
    )
    assert method._decomposer.start.call_args.args[3] == AssistanceModel(
        model=settings.decomposition_model
    )
