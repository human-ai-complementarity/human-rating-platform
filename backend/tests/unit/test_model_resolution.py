"""Which model each assistance call runs on (#95, #96)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from config import get_settings
from models import Question
from services.assistance.methods.human_as_a_tool import HumanAsAToolMethod
from services.assistance.methods.top_n import TopNAssistance
from services.assistance.model_resolution import resolve_model, validate_model_id
from services.assistance.registry import resolved_models

# Sentinels, not real models: a map entry equal to a settings default would
# let a call site that ignores the map pass anyway.
_TOP_N = "openrouter/test/top-n-entry"
_HAAT = "openrouter/test/human-as-a-tool-entry"
_SESSION = "openrouter/test/session-state"
_CONFIDENCE = "openrouter/test/confidence-override"
_CLUSTERING = "openrouter/test/clustering-override"
_MAP = {"assistance_models": {"top_n": _TOP_N, "human_as_a_tool": _HAAT}}


def test_sentinels_differ_from_every_settings_default():
    llm = get_settings().llm
    defaults = {llm.default_model, llm.decomposition_model, llm.confidence_model}
    assert defaults.isdisjoint({_TOP_N, _HAAT, _SESSION, _CONFIDENCE, _CLUSTERING})


def test_role_default_applies_when_nothing_is_pinned():
    llm = get_settings().llm
    assert resolve_model({}, "top_n", "answer") == llm.default_model
    assert resolve_model({}, "human_as_a_tool", "decomposition") == llm.decomposition_model
    assert resolve_model({}, "human_as_a_tool", "confidence") == llm.confidence_model
    assert resolve_model({}, "human_as_a_tool", "clustering") == llm.confidence_model


def test_each_method_resolves_its_own_entry():
    assert resolve_model(_MAP, "top_n", "answer") == _TOP_N
    assert resolve_model(_MAP, "human_as_a_tool", "decomposition") == _HAAT


@pytest.mark.parametrize(
    "params",
    [
        {"assistance_models": {"human_as_a_tool": _HAAT}},
        {"assistance_models": {"top_n": None}},
        {"assistance_models": None},
    ],
)
def test_it_falls_back_to_the_default(params):
    assert resolve_model(params, "top_n", "answer") == get_settings().llm.default_model


def test_a_leftover_model_key_is_ignored():
    """`model` was removed; an old session snapshot may still carry it."""
    default = get_settings().llm.default_model
    assert resolve_model({"model": _SESSION}, "top_n", "answer") == default
    assert resolve_model({**_MAP, "model": _SESSION}, "top_n", "answer") == _TOP_N


def test_a_snapshots_resolved_model_is_a_record_not_an_input():
    """A session snapshot records what ran as `resolved_model`. advance() gets
    the snapshot back as its params, and still resolves from the map."""
    default = get_settings().llm.default_model
    assert resolve_model({"resolved_model": _SESSION}, "top_n", "answer") == default
    assert resolve_model({**_MAP, "resolved_model": _SESSION}, "top_n", "answer") == _TOP_N


def test_resolved_models_gives_every_assisted_method_its_model_and_source():
    llm = get_settings().llm
    params = {"assistance_models": {"top_n": _TOP_N, "human_as_a_tool": None}}
    assert resolved_models(params) == {
        "human_as_a_tool": (llm.decomposition_model, "default"),
        "top_n": (_TOP_N, "assistance_models"),
    }


def test_an_entry_equal_to_the_default_still_reports_the_map():
    default = get_settings().llm.default_model
    resolved = resolved_models({"assistance_models": {"top_n": default}})
    assert resolved["top_n"] == (default, "assistance_models")


def test_the_instrument_takes_its_own_overrides():
    params = {**_MAP, "confidence_model": _CONFIDENCE}
    assert resolve_model(params, "human_as_a_tool", "confidence") == _CONFIDENCE
    assert resolve_model(params, "human_as_a_tool", "clustering") == _CONFIDENCE
    with_clustering = {**params, "clustering_model": _CLUSTERING}
    assert resolve_model(with_clustering, "human_as_a_tool", "clustering") == _CLUSTERING


@pytest.mark.parametrize(
    ("method", "role"), [("top_n", "answer"), ("human_as_a_tool", "decomposition")]
)
def test_the_model_under_test_has_no_role_override(method, role):
    """`assistance_models` already sets these. A second key for the same thing
    would be one more place a model could hide from validation."""
    params = {**_MAP, f"{role}_model": "openrouter/test/role-override"}
    assert resolve_model(params, method, role) == _MAP["assistance_models"][method]


@pytest.mark.parametrize("role", ["confidence", "clustering"])
def test_the_instrument_does_not_inherit_the_map(role):
    """The map holds the model under test; the confidence estimator is the
    platform's measurement instrument. Swapping a pinned frontier model into
    it would change the instrument across datasets and multiply cost by
    num_samples per subtask per round."""
    params = {"assistance_models": {"human_as_a_tool": _HAAT}}
    assert resolve_model(params, "human_as_a_tool", role) == get_settings().llm.confidence_model


@pytest.mark.parametrize("bad", ["gpt-4o", "anthropic/claude-sonnet-4-6", ""])
def test_a_model_the_transport_cannot_parse_is_a_400(bad):
    """_parse_model raises ValueError and both methods swallow it into a NONE
    step, so a typo would otherwise mean every rater silently gets no
    assistance on a study that still looks completed."""
    with pytest.raises(HTTPException) as exc:
        validate_model_id(bad, field="assistance_params.confidence_model")
    assert exc.value.status_code == 400


def test_a_well_formed_model_passes():
    validate_model_id(_CONFIDENCE, field="assistance_params.confidence_model")


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
    [(None, _HAAT), (_SESSION, _SESSION)],
)
@pytest.mark.asyncio
async def test_human_as_a_tool_advance_prefers_the_session_model(state_model, expected):
    method = HumanAsAToolMethod()
    method._decomposer.advance = AsyncMock(side_effect=RuntimeError("stop"))
    await method.advance({"model": state_model}, "{}", _MAP)
    assert method._decomposer.advance.call_args.kwargs["model"] == expected


def test_human_as_a_tool_estimator_ignores_the_map():
    params = {**_MAP, "confidence_method": "sampling"}
    estimator = HumanAsAToolMethod()._get_estimator(params)

    confidence_model = get_settings().llm.confidence_model
    assert estimator._sampling_model == confidence_model
    assert estimator._clustering_model == confidence_model


_INSTRUMENT = {"confidence_model": _CONFIDENCE, "clustering_model": _CLUSTERING}


@pytest.mark.parametrize(
    ("overrides", "confidence_method", "expected"),
    [
        (_INSTRUMENT, "self_report", {"_model": _CONFIDENCE}),
        (_INSTRUMENT, "self_consistency", {"_sampling_model": _CONFIDENCE}),
        (
            _INSTRUMENT,
            "sampling",
            {"_sampling_model": _CONFIDENCE, "_clustering_model": _CLUSTERING},
        ),
        (
            {"confidence_model": _CONFIDENCE},
            "sampling",
            {"_sampling_model": _CONFIDENCE, "_clustering_model": _CONFIDENCE},
        ),
    ],
)
def test_human_as_a_tool_estimator_uses_the_instrument_overrides(
    overrides, confidence_method, expected
):
    params = {**_MAP, "confidence_method": confidence_method, **overrides}
    estimator = HumanAsAToolMethod()._get_estimator(params)

    assert {attr: getattr(estimator, attr) for attr in expected} == expected


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
