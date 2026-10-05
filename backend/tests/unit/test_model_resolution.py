"""Which model each assistance method runs on (`resolve_model`)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from config import get_settings
from models import Question
from services.assistance.methods.human_as_a_tool import HumanAsAToolMethod
from services.assistance.methods.top_n import TopNAssistance
from services.assistance.model_resolution import resolve_model

# Sentinels, not real models: a map entry equal to a settings default would
# let a call site that ignores the map pass anyway.
_TOP_N = "openrouter/test/top-n-entry"
_HAAT = "openrouter/test/human-as-a-tool-entry"
_OVERRIDE = "openrouter/test/explicit-override"
_MAP = {"assistance_models": {"top_n": _TOP_N, "human_as_a_tool": _HAAT}}
_DEFAULT = "openrouter/default"


def test_sentinels_differ_from_every_settings_default():
    llm = get_settings().llm
    defaults = {llm.default_model, llm.decomposition_model, llm.confidence_model}
    assert defaults.isdisjoint({_TOP_N, _HAAT, _OVERRIDE})


def test_each_method_resolves_its_own_entry():
    assert resolve_model(_MAP, "top_n", _DEFAULT) == _TOP_N
    assert resolve_model(_MAP, "human_as_a_tool", _DEFAULT) == _HAAT


def test_an_explicit_model_wins_over_the_map():
    params = {**_MAP, "model": _OVERRIDE}
    assert resolve_model(params, "top_n", _DEFAULT) == _OVERRIDE
    assert resolve_model(params, "human_as_a_tool", _DEFAULT) == _OVERRIDE


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"assistance_models": {"human_as_a_tool": _HAAT}},
        {"assistance_models": {"top_n": None}},
        {"assistance_models": None},
        {"model": None},
    ],
)
def test_it_falls_back_to_the_default(params):
    assert resolve_model(params, "top_n", _DEFAULT) == _DEFAULT


def test_a_cleared_model_falls_through_to_the_map():
    assert resolve_model({**_MAP, "model": None}, "top_n", _DEFAULT) == _TOP_N


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
    assert llm.call_args.kwargs["model"] == _TOP_N


@pytest.mark.asyncio
async def test_human_as_a_tool_start_uses_its_entry():
    method = HumanAsAToolMethod()
    method._decomposer.start = AsyncMock(side_effect=RuntimeError("stop"))
    await method.start(_question(), _MAP)
    assert method._decomposer.start.call_args.args[3] == _HAAT


@pytest.mark.parametrize(
    ("state_model", "expected"),
    [(None, _HAAT), (_OVERRIDE, _OVERRIDE)],
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
    assert llm.call_args.kwargs["model"] == settings.default_model
    assert method._decomposer.start.call_args.args[3] == settings.decomposition_model
