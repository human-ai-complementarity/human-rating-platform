"""Which model each assistance call runs on (#95, #96)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from config import get_settings
from models import Question
from services.assistance.methods.human_as_a_tool import HumanAsAToolMethod
from services.assistance.methods.top_n import TopNAssistance
from services.assistance import model_resolution, registry
from services.assistance.model_resolution import (
    instrument_model_keys,
    resolve_model,
    resolve_model_and_source,
    validate_model_id,
)
from services.assistance.registry import (
    assisted_methods,
    get_method,
    record_role_models,
    resolved_models,
)

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


def test_a_snapshots_role_models_are_a_record_not_an_input():
    """A session snapshot records each role's model as `role_models`. advance()
    gets the snapshot back as its params, and still resolves from the map."""
    default = get_settings().llm.default_model
    recorded = {"role_models": {"answer": _SESSION}}
    assert resolve_model(recorded, "top_n", "answer") == default
    assert resolve_model({**_MAP, **recorded}, "top_n", "answer") == _TOP_N


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


@pytest.mark.parametrize("params", [{}, _MAP, {"assistance_models": {"top_n": _TOP_N}}])
def test_the_display_and_the_call_resolve_through_one_function(params):
    """The admin display asks `resolve_model_and_source` about each method's
    primary role: the question the call itself asks, via `resolve_model`."""
    for name, resolved in resolved_models(params).items():
        role = get_method(name).primary_model_role
        assert resolved == resolve_model_and_source(params, name, role)
        assert resolved[0] == resolve_model(params, name, role)


def test_every_displayed_role_takes_the_map():
    """The display shows primary roles only. They run the model under test, so
    their source is never `override`, which the API's `ResolvedModel` lacks."""
    for name in assisted_methods():
        role = get_method(name).primary_model_role
        params = {"assistance_models": {name: _SESSION}, f"{role}_model": _CONFIDENCE}
        assert resolve_model_and_source(params, name, role) == (_SESSION, "assistance_models")


def test_an_instrument_override_reports_its_source():
    params = {"confidence_model": _CONFIDENCE}
    override = (_CONFIDENCE, "override")
    assert resolve_model_and_source(params, "human_as_a_tool", "confidence") == override
    # Clustering follows confidence, source included.
    assert resolve_model_and_source(params, "human_as_a_tool", "clustering") == override
    default = (get_settings().llm.confidence_model, "default")
    assert resolve_model_and_source({}, "human_as_a_tool", "clustering") == default


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
    """Pins current behaviour: confidence and clustering keep their own models.
    Whether confidence should follow the model under test is open in #148."""
    params = {"assistance_models": {"human_as_a_tool": _HAAT}}
    assert resolve_model(params, "human_as_a_tool", role) == get_settings().llm.confidence_model


def test_today_the_instrument_roles_have_the_override_keys():
    assert instrument_model_keys() == ("confidence_model", "clustering_model")


def test_moving_confidence_onto_the_map_is_one_edit(monkeypatch):
    """If #148 makes confidence inherit, adding it to the inheriting set is
    enough: its key stops being an override, and clustering still follows it."""
    inheriting = frozenset({"answer", "decomposition", "confidence"})
    monkeypatch.setattr(model_resolution, "_INHERITING_ROLES", inheriting)

    assert instrument_model_keys() == ("clustering_model",)
    params = {**_MAP, "confidence_model": _CONFIDENCE}
    assert resolve_model(params, "human_as_a_tool", "confidence") == _HAAT
    assert resolve_model(params, "human_as_a_tool", "clustering") == _HAAT


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


_HAAT_ROLES = {"decomposition": _HAAT, "confidence": _CONFIDENCE}


@pytest.mark.parametrize(
    ("name", "params", "expected"),
    [
        ("none", _MAP, None),
        ("top_n", _MAP, {"answer": _TOP_N}),
        ("human_as_a_tool", {**_MAP, **_INSTRUMENT}, _HAAT_ROLES),
        (
            "human_as_a_tool",
            {**_MAP, **_INSTRUMENT, "confidence_method": "self_consistency"},
            _HAAT_ROLES,
        ),
        (
            "human_as_a_tool",
            {**_MAP, **_INSTRUMENT, "confidence_method": "sampling"},
            {**_HAAT_ROLES, "clustering": _CLUSTERING},
        ),
    ],
)
def test_a_session_records_every_role_its_method_calls(name, params, expected):
    """Clustering only for the confidence method that clusters; a model-free
    method records no map. The params themselves are kept as given."""
    recorded = record_role_models(name, params)
    assert recorded.get("role_models") == expected
    assert {key: value for key, value in recorded.items() if key != "role_models"} == params


@pytest.mark.parametrize(("name", "expected"), [("none", None), ("top_n", {"answer": _TOP_N})])
def test_a_role_models_key_in_the_params_is_never_recorded(name, expected):
    """Create/PATCH reject it; one that reached the row anyway is dropped,
    not copied into the session as if its models ran."""
    stale = {**_MAP, "role_models": {"answer": _SESSION}}
    assert record_role_models(name, stale).get("role_models") == expected


@pytest.mark.parametrize("confidence_method", ["self_report", "self_consistency", "sampling"])
def test_the_estimator_runs_exactly_the_recorded_instrument_models(confidence_method):
    params = {**_MAP, **_INSTRUMENT, "confidence_method": confidence_method}
    estimator = HumanAsAToolMethod()._get_estimator(params)

    ran = {"confidence": getattr(estimator, "_model", None) or estimator._sampling_model}
    if hasattr(estimator, "_clustering_model"):
        ran["clustering"] = estimator._clustering_model
    recorded = record_role_models("human_as_a_tool", params)["role_models"]
    assert recorded == {"decomposition": _HAAT, **ran}


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


def test_each_method_is_registered_under_its_method_key():
    for name in ("none", "top_n", "human_as_a_tool"):
        assert type(get_method(name)).method_key == name


# Subclasses with another key and role: a call site still naming "top_n" or
# "answer" (or "human_as_a_tool"/"decomposition") would resolve the parent's.
class _TopNVariant(TopNAssistance):
    method_key = "top_n_variant"
    primary_model_role = "decomposition"


class _HaaTVariant(HumanAsAToolMethod):
    method_key = "haat_variant"
    primary_model_role = "answer"


_VARIANTS = {"assistance_models": {**_MAP["assistance_models"], "top_n_variant": _SESSION}}


@pytest.mark.asyncio
async def test_top_n_runs_and_records_under_its_own_key_and_role(monkeypatch):
    monkeypatch.setitem(registry._REGISTRY, "top_n_variant", _TopNVariant)
    llm = AsyncMock(side_effect=RuntimeError("stop"))
    with patch("services.assistance.methods.top_n._complete_with_schema_fallback", new=llm):
        await _TopNVariant().start(_question(), _VARIANTS)
        await _TopNVariant().start(_question(), {})

    default = get_settings().llm.decomposition_model
    assert [call.kwargs["model"] for call in llm.call_args_list] == [_SESSION, default]
    assert record_role_models("top_n_variant", _VARIANTS)["role_models"] == {
        "decomposition": _SESSION
    }
    pinned = _TopNVariant().preparation_params({})["assistance_models"]
    assert pinned == {"top_n_variant": default}


@pytest.mark.asyncio
async def test_human_as_a_tool_runs_under_its_own_key_and_role():
    method = _HaaTVariant()
    method._decomposer.start = AsyncMock(side_effect=RuntimeError("stop"))
    method._decomposer.advance = AsyncMock(side_effect=RuntimeError("stop"))
    await method.start(_question(), {"assistance_models": {"haat_variant": _SESSION}})
    await method.advance({}, "{}", _MAP)

    default = get_settings().llm.default_model
    assert method._decomposer.start.call_args.args[3] == _SESSION
    assert method._decomposer.advance.call_args.kwargs["model"] == default
    assert method.preparation_params({})["assistance_models"] == {"haat_variant": default}
