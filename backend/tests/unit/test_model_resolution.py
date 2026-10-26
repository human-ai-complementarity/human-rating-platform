"""Which model each assistance call runs on, and with what (#95, #96, #148)."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from config import LLMSettings, get_settings
from models import Question
from services.assistance.methods.human_as_a_tool import HumanAsAToolMethod
from services.assistance.methods.top_n import TopNAssistance
from services.assistance import model_resolution, registry
from services.assistance.model_resolution import (
    AssistanceModel,
    instrument_model_keys,
    pinned_model,
    resolve_model,
    resolve_model_and_source,
    same_entry,
    validate_model_entry,
    validate_model_id,
)
from services.assistance.registry import (
    assisted_methods,
    expand_bare_entry,
    get_method,
    record_role_models,
    resolved_models,
)

# Sentinels, not real models: a map entry equal to a settings default would
# let a call site that ignores the map pass anyway. Each carries options, so a
# call site that keeps the model but drops them shows too.
_TOP_N = AssistanceModel("openrouter/test/top-n-entry", reasoning_effort="low", temperature=0.5)
_HAAT = AssistanceModel("openrouter/test/human-as-a-tool-entry", text_verbosity="low")
_SESSION = AssistanceModel("openrouter/test/session-state", temperature=1)
_CONFIDENCE = "openrouter/test/confidence-override"
_CLUSTERING = "openrouter/test/clustering-override"
_MAP = {"assistance_models": {"top_n": _TOP_N.to_dict(), "human_as_a_tool": _HAAT.to_dict()}}


def _bare(model: str, **options) -> AssistanceModel:
    """`model` with no options sent, unless given: how an instrument runs."""
    return AssistanceModel(model, **options)


def test_sentinels_differ_from_every_settings_default():
    llm = get_settings().llm
    defaults = {
        llm.default_model,
        llm.decomposition_model,
        llm.confidence_model,
        llm.clustering_model,
    }
    assert defaults.isdisjoint(
        {_TOP_N.model, _HAAT.model, _SESSION.model, _CONFIDENCE, _CLUSTERING}
    )


def test_role_default_applies_when_nothing_is_pinned():
    """Each role's settings model, with the options it always sent: Top-N
    ranks at temperature 0, the rest send none."""
    llm = get_settings().llm
    assert resolve_model({}, "top_n", "answer") == _bare(llm.default_model, temperature=0)
    assert resolve_model({}, "human_as_a_tool", "decomposition") == _bare(llm.decomposition_model)
    assert resolve_model({}, "human_as_a_tool", "confidence") == _bare(llm.confidence_model)
    assert resolve_model({}, "human_as_a_tool", "clustering") == _bare(llm.clustering_model)


def test_clustering_defaults_to_its_own_setting(monkeypatch):
    """`LLM__CLUSTERING_MODEL`, not confidence's default (#151)."""
    llm = get_settings().llm
    monkeypatch.setattr(llm, "clustering_model", _CLUSTERING)
    assert resolve_model_and_source({}, "human_as_a_tool", "clustering") == (
        _bare(_CLUSTERING),
        "default",
    )
    assert resolve_model({}, "human_as_a_tool", "confidence") == _bare(llm.confidence_model)


def test_the_clustering_setting_moved_no_default():
    """Clustering got its own setting at confidence's default, so it still
    runs where it did."""
    assert LLMSettings().clustering_model == LLMSettings().confidence_model


@pytest.mark.parametrize(
    ("method", "role", "expected"),
    [
        ("top_n", "answer", _TOP_N),
        ("human_as_a_tool", "decomposition", _HAAT),
        # #148: confidence and clustering run the model under test too, with
        # their own options: sampling's temperature is the instrument's setting.
        ("human_as_a_tool", "confidence", _bare(_HAAT.model)),
        ("human_as_a_tool", "clustering", _bare(_HAAT.model)),
    ],
)
def test_every_role_resolves_its_methods_entry(method, role, expected):
    assert resolve_model_and_source(_MAP, method, role) == (expected, "assistance_models")


@pytest.mark.parametrize(
    "params",
    [
        {"assistance_models": {"human_as_a_tool": _HAAT.to_dict()}},
        {"assistance_models": {"top_n": None}},
        {"assistance_models": {"top_n": {}}},
        {"assistance_models": {"top_n": {"model": ""}}},
        {"assistance_models": None},
    ],
)
def test_it_falls_back_to_the_default(params):
    assert resolve_model(params, "top_n", "answer") == resolve_model({}, "top_n", "answer")


def test_a_bare_id_in_the_map_runs_with_the_roles_default_options():
    """A row or session snapshot written before entries carried options."""
    params = {"assistance_models": {"top_n": "openrouter/test/legacy"}}
    assert resolve_model(params, "top_n", "answer") == _bare(
        "openrouter/test/legacy", temperature=0
    )
    params = {"assistance_models": {"human_as_a_tool": "openrouter/test/legacy"}}
    assert resolve_model(params, "human_as_a_tool", "decomposition") == _bare(
        "openrouter/test/legacy"
    )


def test_an_entry_missing_a_key_takes_the_roles_default_for_it():
    params = {"assistance_models": {"top_n": {"model": "openrouter/test/partial"}}}
    assert resolve_model(params, "top_n", "answer") == _bare(
        "openrouter/test/partial", temperature=0
    )


def test_pinned_model_reads_the_entry_as_declared():
    """The launch gate's test: a bare id has no options, an object its own."""
    assert pinned_model(_MAP, "top_n") == _TOP_N
    assert pinned_model({"assistance_models": {"top_n": "openrouter/x"}}, "top_n") == _bare(
        "openrouter/x"
    )
    assert pinned_model({}, "top_n") is None
    assert pinned_model({"assistance_models": {"top_n": None}}, "top_n") is None


def test_expanding_a_bare_id_gives_the_entry_it_stood_for():
    """The question upload's grace for exports written before entries existed."""
    assert (
        expand_bare_entry("top_n", "openrouter/x") == _bare("openrouter/x", temperature=0).to_dict()
    )
    assert expand_bare_entry("human_as_a_tool", "openrouter/x") == _bare("openrouter/x").to_dict()


def test_a_leftover_model_key_is_ignored():
    """`model` was removed; an old session snapshot may still carry it."""
    default = resolve_model({}, "top_n", "answer")
    assert resolve_model({"model": _SESSION.model}, "top_n", "answer") == default
    assert resolve_model({**_MAP, "model": _SESSION.model}, "top_n", "answer") == _TOP_N


def test_a_snapshots_role_models_are_a_record_not_an_input():
    """A session snapshot records each role's model as `role_models`. advance()
    gets the snapshot back as its params, and still resolves from the map."""
    default = resolve_model({}, "top_n", "answer")
    recorded = {"role_models": {"answer": _SESSION.to_dict()}}
    assert resolve_model(recorded, "top_n", "answer") == default
    assert resolve_model({**_MAP, **recorded}, "top_n", "answer") == _TOP_N


def test_resolved_models_gives_every_assisted_method_its_entry_and_source():
    llm = get_settings().llm
    params = {"assistance_models": {"top_n": _TOP_N.to_dict(), "human_as_a_tool": None}}
    assert resolved_models(params) == {
        "human_as_a_tool": (_bare(llm.decomposition_model), "default"),
        "top_n": (_TOP_N, "assistance_models"),
    }


def test_an_entry_equal_to_the_default_still_reports_the_map():
    default = resolve_model({}, "top_n", "answer")
    resolved = resolved_models({"assistance_models": {"top_n": default.to_dict()}})
    assert resolved["top_n"] == (default, "assistance_models")


@pytest.mark.parametrize("params", [{}, _MAP, {"assistance_models": {"top_n": _TOP_N.to_dict()}}])
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
        params = {"assistance_models": {name: _SESSION.to_dict()}, f"{role}_model": _CONFIDENCE}
        assert resolve_model_and_source(params, name, role) == (_SESSION, "assistance_models")


@pytest.mark.parametrize(
    ("role", "model"), [("confidence", _CONFIDENCE), ("clustering", _CLUSTERING)]
)
def test_an_instrument_override_reports_its_source(role, model):
    params = {f"{role}_model": model}
    assert resolve_model_and_source(params, "human_as_a_tool", role) == (_bare(model), "override")


@pytest.mark.parametrize("params", [{}, _MAP], ids=["default", "map"])
@pytest.mark.parametrize(
    ("role", "other", "model"),
    [("confidence", "clustering", _CONFIDENCE), ("clustering", "confidence", _CLUSTERING)],
)
def test_overriding_one_instrument_role_leaves_the_other(params, role, other, model):
    """Each resolves on its own (#151): clustering no longer follows confidence."""
    overridden = {**params, f"{role}_model": model}
    before = resolve_model_and_source(params, "human_as_a_tool", other)
    assert resolve_model_and_source(overridden, "human_as_a_tool", other) == before


def test_an_override_beats_the_map():
    params = {**_MAP, "confidence_model": _CONFIDENCE}
    assert resolve_model(params, "human_as_a_tool", "decomposition") == _HAAT
    assert resolve_model(params, "human_as_a_tool", "confidence") == _bare(_CONFIDENCE)
    assert resolve_model(params, "human_as_a_tool", "clustering") == _bare(_HAAT.model)
    with_clustering = {**params, "clustering_model": _CLUSTERING}
    assert resolve_model(with_clustering, "human_as_a_tool", "clustering") == _bare(_CLUSTERING)
    only_clustering = {**_MAP, "clustering_model": _CLUSTERING}
    assert resolve_model(only_clustering, "human_as_a_tool", "confidence") == _bare(_HAAT.model)
    assert resolve_model(only_clustering, "human_as_a_tool", "clustering") == _bare(_CLUSTERING)


@pytest.mark.parametrize(
    ("method", "role"), [("top_n", "answer"), ("human_as_a_tool", "decomposition")]
)
def test_the_model_under_test_has_no_role_override(method, role):
    """The method's `assistance_models` entry already is the primary role's
    setting, so a `<role>_model` key for it is ignored."""
    params = {**_MAP, f"{role}_model": "openrouter/test/role-override"}
    assert resolve_model(params, method, role).to_dict() == _MAP["assistance_models"][method]


@pytest.mark.parametrize("role", ["confidence", "clustering"])
def test_another_methods_entry_is_not_inherited(role):
    params = {"assistance_models": {"top_n": _TOP_N.to_dict()}}
    default = getattr(get_settings().llm, f"{role}_model")
    assert resolve_model(params, "human_as_a_tool", role) == _bare(default)


def test_the_overrides_keep_their_keys_on_the_map():
    """Inheriting the map did not retire the escape hatch, or its validation."""
    assert instrument_model_keys() == ("confidence_model", "clustering_model")


def test_giving_a_role_an_override_is_one_edit(monkeypatch):
    """The keys are derived from the override roles: adding one adds its key."""
    overrides = frozenset({"decomposition", "confidence", "clustering"})
    monkeypatch.setattr(model_resolution, "_OVERRIDE_ROLES", overrides)

    assert instrument_model_keys() == (
        "decomposition_model",
        "confidence_model",
        "clustering_model",
    )
    params = {**_MAP, "decomposition_model": _SESSION.model}
    assert resolve_model_and_source(params, "human_as_a_tool", "decomposition") == (
        _bare(_SESSION.model),
        "override",
    )


@pytest.mark.parametrize("bad", ["gpt-4o", "anthropic/claude-sonnet-4-6", "openai/gpt-4o", ""])
def test_a_model_the_transport_cannot_parse_is_a_400(bad):
    """`parse_model` raises ValueError and both methods swallow it into a NONE
    step, so a typo would otherwise mean every rater silently gets no
    assistance on a study that still looks completed."""
    with pytest.raises(HTTPException) as exc:
        validate_model_id(bad, field="assistance_params.confidence_model")
    assert exc.value.status_code == 400


def test_a_well_formed_model_passes():
    validate_model_id(_CONFIDENCE, field="assistance_params.confidence_model")
    validate_model_id("oai/gpt-5.6-luna", field="assistance_params.confidence_model")


def test_a_complete_entry_passes_and_a_bare_id_does_not():
    validate_model_entry(_TOP_N.to_dict(), field="x")
    with pytest.raises(HTTPException) as exc:
        validate_model_entry(_TOP_N.model, field="x")
    assert "bare model id" in exc.value.detail


def _without_tools(entry: AssistanceModel) -> dict:
    return {key: value for key, value in entry.to_dict().items() if key != "tools"}


# What an entry with tools records and displays. Validation refuses it until
# the transport sends tools, so it is built here, not written through the API.
_TOOLED = replace(_HAAT, tools=("web_search", "code_interpreter"))


def test_tools_are_optional_and_left_out_is_none():
    """`tools` came after the other options: an entry from an older export,
    declared without it, stays valid and has no tools. The rest stay required."""
    validate_model_entry(_without_tools(_TOP_N), field="x")
    validate_model_entry({**_TOP_N.to_dict(), "tools": []}, field="x")
    assert AssistanceModel.from_entry(_without_tools(_TOP_N)) == _TOP_N
    assert _TOP_N.tools == ()
    with pytest.raises(HTTPException) as exc:
        validate_model_entry({"model": _TOP_N.model, "tools": []}, field="x")
    assert "missing required key(s) reasoning_effort, text_verbosity, temperature." in (
        exc.value.detail
    )


_FIELD = "assistance_params.assistance_models.top_n"


@pytest.mark.parametrize(
    ("tools", "expected"),
    [
        (None, "'tools' must be a list of tool names from web_search, code_interpreter"),
        ("web_search", "'tools' must be a list"),
        ({"web_search": True}, "'tools' must be a list"),
        (
            ["browser"],
            "'tools' has unknown tool(s) \"browser\"; allowed: web_search, code_interpreter",
        ),
        (["web_search", 1], "'tools' has unknown tool(s) 1;"),
        (["Web_Search"], 'unknown tool(s) "Web_Search"'),
        ([None, {"name": "web_search"}], 'unknown tool(s) null, {"name": "web_search"};'),
        (["web_search", "web_search"], "'tools' lists web_search more than once"),
    ],
)
def test_bad_tools_are_a_400_naming_the_field(tools, expected):
    with pytest.raises(HTTPException) as exc:
        validate_model_entry({**_TOP_N.to_dict(), "tools": tools}, field=_FIELD)
    assert exc.value.status_code == 400
    assert exc.value.detail.startswith(f"Invalid model entry in {_FIELD}: ")
    assert expected in exc.value.detail


@pytest.mark.parametrize(
    "tools", [["web_search"], ["code_interpreter"], ["code_interpreter", "web_search"]]
)
def test_tools_are_refused_until_the_transport_sends_them(tools):
    """Never accepted and then silently run without."""
    with pytest.raises(HTTPException) as exc:
        validate_model_entry({**_TOP_N.to_dict(), "tools": tools}, field=_FIELD)
    assert exc.value.status_code == 400
    assert f"in {_FIELD}: tools are not supported yet" in exc.value.detail


def test_an_entry_with_tools_round_trips_and_stays_hashable():
    """Stored, tools are a JSON list; read back, a tuple, so Top-N can keep
    the entry in its schema memo's sets."""
    stored = json.loads(json.dumps(_TOOLED.to_dict()))
    assert stored["tools"] == ["web_search", "code_interpreter"]
    assert AssistanceModel.from_entry(stored) == _TOOLED
    assert {_TOOLED, AssistanceModel.from_entry(stored)} == {_TOOLED}
    assert _TOOLED != _HAAT
    # A stored null (never written by a validated path) reads as no tools.
    assert AssistanceModel.from_entry({**_HAAT.to_dict(), "tools": None}) == _HAAT


def test_a_left_out_tools_and_an_empty_one_are_the_same_entry():
    """So a re-upload spelling it the other way is not reported as a conflict."""
    assert same_entry(_without_tools(_TOP_N), _TOP_N.to_dict())
    assert same_entry(_TOP_N.to_dict(), _without_tools(_TOP_N))
    assert not same_entry(_HAAT.to_dict(), _TOOLED.to_dict())
    assert not same_entry(_TOP_N.to_dict(), {**_TOP_N.to_dict(), "temperature": 1})
    # As before: a bare id or a cleared entry is not the declared entry.
    assert not same_entry(_TOP_N.model, _TOP_N.to_dict())
    assert not same_entry(None, _TOP_N.to_dict())
    # Only the optional keys are filled in: a missing temperature is not null.
    partial = {"model": _TOP_N.model, "reasoning_effort": None, "text_verbosity": None}
    assert not same_entry(partial, {**partial, "temperature": None})
    assert not same_entry({**_TOP_N.to_dict(), "tools": 5}, _TOP_N.to_dict())


def test_tools_are_not_passed_to_the_transport_yet():
    assert _HAAT.call_kwargs() == _without_tools(_HAAT)
    # One stored anyway fails the call, rather than run without its tools.
    with pytest.raises(ValueError, match="^tools are not supported yet; "):
        _TOOLED.call_kwargs()


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
    assert sent["text_verbosity"] is None
    assert sent["temperature"] == 0.5


@pytest.mark.asyncio
async def test_a_stored_entry_with_tools_is_a_provider_error_not_a_call_without_them():
    """Validation refuses tools; an entry holding some anyway is never run
    without them, and the session says why it got no assistance."""
    params = {
        "assistance_models": {"top_n": _TOOLED.to_dict(), "human_as_a_tool": _TOOLED.to_dict()}
    }
    llm = AsyncMock(return_value="{}")
    with (
        patch("services.assistance.methods.top_n.complete", new=llm),
        patch("services.assistance.methods.human_as_a_tool.decomposer.complete", new=llm),
    ):
        steps = [
            await TopNAssistance().start(_question(), params),
            await HumanAsAToolMethod().start(_question(), params),
        ]
    llm.assert_not_called()
    for step in steps:
        assert step.failure_reason == "provider_error"
        assert step.failure_detail == (
            f"ValueError: tools are not supported yet; {_TOOLED.model} has "
            "web_search, code_interpreter"
        )


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
        (_SESSION.model, _bare(_SESSION.model)),
    ],
)
@pytest.mark.asyncio
async def test_human_as_a_tool_advance_prefers_the_session_model(state_model, expected):
    method = HumanAsAToolMethod()
    method._decomposer.advance = AsyncMock(side_effect=RuntimeError("stop"))
    await method.advance({"model": state_model}, "{}", _MAP)
    assert method._decomposer.advance.call_args.kwargs["model"] == expected


@pytest.mark.parametrize(
    ("confidence_method", "attrs"),
    [
        ("self_report", ("_model",)),
        ("self_consistency", ("_sampling_model",)),
        ("sampling", ("_sampling_model", "_clustering_model")),
    ],
)
def test_human_as_a_tool_estimator_runs_on_the_map(confidence_method, attrs):
    """The instruments take the entry's model id; their options are their own."""
    params = {**_MAP, "confidence_method": confidence_method}
    estimator = HumanAsAToolMethod()._get_estimator(params)

    assert {attr: getattr(estimator, attr) for attr in attrs} == dict.fromkeys(attrs, _HAAT.model)


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
            # Clustering stays on the map: confidence's override is its own.
            {"confidence_model": _CONFIDENCE},
            "sampling",
            {"_sampling_model": _CONFIDENCE, "_clustering_model": _HAAT.model},
        ),
    ],
)
def test_human_as_a_tool_estimator_uses_the_instrument_overrides(
    overrides, confidence_method, expected
):
    """The overrides still beat the map."""
    params = {**_MAP, "confidence_method": confidence_method, **overrides}
    estimator = HumanAsAToolMethod()._get_estimator(params)

    assert {attr: getattr(estimator, attr) for attr in expected} == expected


_HAAT_INSTRUMENT = _bare(_HAAT.model).to_dict()
_HAAT_ROLES = {"decomposition": _HAAT.to_dict(), "confidence": _bare(_CONFIDENCE).to_dict()}


@pytest.mark.parametrize(
    ("name", "params", "expected"),
    [
        ("none", _MAP, None),
        ("top_n", _MAP, {"answer": _TOP_N.to_dict()}),
        (
            "human_as_a_tool",
            _MAP,
            {"decomposition": _HAAT.to_dict(), "confidence": _HAAT_INSTRUMENT},
        ),
        (
            "human_as_a_tool",
            {**_MAP, "confidence_method": "sampling"},
            {
                "decomposition": _HAAT.to_dict(),
                "confidence": _bare(_HAAT.model, temperature=0.7).to_dict(),
                "clustering": _bare(_HAAT.model, temperature=0).to_dict(),
            },
        ),
        ("human_as_a_tool", {**_MAP, **_INSTRUMENT}, _HAAT_ROLES),
        (
            "human_as_a_tool",
            {**_MAP, **_INSTRUMENT, "confidence_method": "self_consistency"},
            {**_HAAT_ROLES, "confidence": _bare(_CONFIDENCE, temperature=0.7).to_dict()},
        ),
        (
            "human_as_a_tool",
            {**_MAP, **_INSTRUMENT, "confidence_method": "sampling"},
            {
                **_HAAT_ROLES,
                "confidence": _bare(_CONFIDENCE, temperature=0.7).to_dict(),
                "clustering": _bare(_CLUSTERING, temperature=0).to_dict(),
            },
        ),
    ],
)
def test_a_session_records_every_role_its_method_calls(name, params, expected):
    """Clustering only for the confidence method that clusters; a model-free
    method records no map. Each role is recorded as the entry it runs: the
    model and the options sent, so the sampling estimators show their 0.7 and
    clustering its 0. The params themselves are kept as given."""
    recorded = record_role_models(name, params)
    assert recorded.get("role_models") == expected
    assert {key: value for key, value in recorded.items() if key != "role_models"} == params


@pytest.mark.parametrize(
    ("name", "expected"), [("none", None), ("top_n", {"answer": _TOP_N.to_dict()})]
)
def test_a_role_models_key_in_the_params_is_never_recorded(name, expected):
    """Create/PATCH reject it; one that reached the row anyway is dropped,
    not copied into the session as if its models ran."""
    stale = {**_MAP, "role_models": {"answer": _SESSION.to_dict()}}
    assert record_role_models(name, stale).get("role_models") == expected


@pytest.mark.parametrize("overrides", [{}, _INSTRUMENT], ids=["map", "overrides"])
@pytest.mark.parametrize("confidence_method", ["self_report", "self_consistency", "sampling"])
def test_a_session_records_each_roles_tools_and_the_instruments_get_none(
    confidence_method, overrides
):
    """The model under test is recorded with its entry's tools; confidence
    scoring and clustering never get tools, whatever the entry says."""
    params = {
        "assistance_models": {"human_as_a_tool": _TOOLED.to_dict()},
        "confidence_method": confidence_method,
        **overrides,
    }
    recorded = record_role_models("human_as_a_tool", params)["role_models"]
    assert {role: entry["tools"] for role, entry in recorded.items()} == {
        "decomposition": ["web_search", "code_interpreter"],
        **dict.fromkeys(HumanAsAToolMethod.model_roles(params)[1:], []),
    }
    top_n = record_role_models("top_n", {"assistance_models": {"top_n": _TOOLED.to_dict()}})
    assert top_n["role_models"] == {"answer": _TOOLED.to_dict()}


@pytest.mark.parametrize("confidence_method", ["self_report", "self_consistency", "sampling"])
def test_the_instruments_record_the_temperature_the_estimator_sends(confidence_method):
    """The record and the call take their temperatures from one place."""
    params = {**_MAP, "confidence_method": confidence_method}
    models = HumanAsAToolMethod.role_models(params)
    estimator = HumanAsAToolMethod()._get_estimator(params)
    assert models["confidence"].temperature == getattr(estimator, "_temperature", None)
    if "clustering" in models:
        assert models["clustering"].temperature == estimator._clustering_temperature


@pytest.mark.parametrize("overrides", [{}, _INSTRUMENT], ids=["map", "overrides"])
@pytest.mark.parametrize("confidence_method", ["self_report", "self_consistency", "sampling"])
def test_the_estimator_runs_exactly_the_recorded_instrument_models(confidence_method, overrides):
    params = {**_MAP, **overrides, "confidence_method": confidence_method}
    estimator = HumanAsAToolMethod()._get_estimator(params)

    ran = {"confidence": getattr(estimator, "_model", None) or estimator._sampling_model}
    if hasattr(estimator, "_clustering_model"):
        ran["clustering"] = estimator._clustering_model
    recorded = record_role_models("human_as_a_tool", params)["role_models"]
    assert {role: entry["model"] for role, entry in recorded.items()} == {
        "decomposition": _HAAT.model,
        **ran,
    }


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("top_n", {}),
        ("top_n", _MAP),
        ("human_as_a_tool", {}),
        ("human_as_a_tool", _MAP),
        ("human_as_a_tool", {"confidence_method": "sampling"}),
        ("human_as_a_tool", {**_MAP, "confidence_method": "sampling"}),
        ("human_as_a_tool", {**_MAP, "confidence_model": _CONFIDENCE}),
        ("human_as_a_tool", {"confidence_model": _CONFIDENCE, "confidence_method": "sampling"}),
        ("human_as_a_tool", {"clustering_model": _CLUSTERING, "confidence_method": "sampling"}),
        ("human_as_a_tool", {**_MAP, **_INSTRUMENT, "confidence_method": "sampling"}),
    ],
)
def test_preparation_pins_the_models_the_foreground_would_run(name, params, monkeypatch):
    """A prepared session runs on `preparation_params`, a foreground one on the
    params as stored: every role resolves the same either way. Without a map
    entry, pinning one must not pull confidence or clustering off its own
    default."""
    # Distinct defaults, so a role left to follow the newly pinned entry shows.
    for role in ("decomposition", "confidence", "clustering"):
        monkeypatch.setattr(get_settings().llm, f"{role}_model", f"openrouter/test/{role}-default")
    cls = registry._REGISTRY[name]
    assert cls.role_models(get_method(name).preparation_params(params)) == cls.role_models(params)


@pytest.mark.asyncio
async def test_without_params_methods_keep_their_defaults():
    llm = AsyncMock(side_effect=RuntimeError("stop"))
    with patch("services.assistance.methods.top_n._complete_with_schema_fallback", new=llm):
        await TopNAssistance().start(_question(), {})
    method = HumanAsAToolMethod()
    method._decomposer.start = AsyncMock(side_effect=RuntimeError("stop"))
    await method.start(_question(), {})

    settings = get_settings().llm
    assert llm.call_args.kwargs["entry"] == _bare(settings.default_model, temperature=0)
    assert method._decomposer.start.call_args.args[3] == _bare(settings.decomposition_model)


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


_VARIANTS = {
    "assistance_models": {**_MAP["assistance_models"], "top_n_variant": _SESSION.to_dict()}
}


@pytest.mark.asyncio
async def test_top_n_runs_and_records_under_its_own_key_and_role(monkeypatch):
    monkeypatch.setitem(registry._REGISTRY, "top_n_variant", _TopNVariant)
    llm = AsyncMock(side_effect=RuntimeError("stop"))
    with patch("services.assistance.methods.top_n._complete_with_schema_fallback", new=llm):
        await _TopNVariant().start(_question(), _VARIANTS)
        await _TopNVariant().start(_question(), {})

    default = _bare(get_settings().llm.decomposition_model)
    assert [call.kwargs["entry"] for call in llm.call_args_list] == [_SESSION, default]
    assert record_role_models("top_n_variant", _VARIANTS)["role_models"] == {
        "decomposition": _SESSION.to_dict()
    }
    pinned = _TopNVariant().preparation_params({})["assistance_models"]
    assert pinned == {"top_n_variant": default.to_dict()}


@pytest.mark.asyncio
async def test_human_as_a_tool_runs_under_its_own_key_and_role():
    method = _HaaTVariant()
    method._decomposer.start = AsyncMock(side_effect=RuntimeError("stop"))
    method._decomposer.advance = AsyncMock(side_effect=RuntimeError("stop"))
    await method.start(_question(), {"assistance_models": {"haat_variant": _SESSION.to_dict()}})
    await method.advance({}, "{}", _MAP)

    default = _bare(get_settings().llm.default_model, temperature=0)
    assert method._decomposer.start.call_args.args[3] == _SESSION
    assert method._decomposer.advance.call_args.kwargs["model"] == default
    assert method.preparation_params({})["assistance_models"] == {"haat_variant": default.to_dict()}
