"""The data migration that folds `assistance_params["model"]` into `assistance_models`."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic/versions/20261006000000_fold_assistance_model_into_map.py"
)
_spec = importlib.util.spec_from_file_location("fold_model_rev", _PATH)
assert _spec is not None and _spec.loader is not None
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)
fold = _migration.fold_model_into_map

_M = "openrouter/test/model"
_UPLOADED = "openrouter/test/uploaded"


@pytest.mark.parametrize("method", ["top_n", "human_as_a_tool"])
def test_an_assisted_method_gets_the_model(method):
    assert fold({"n": 3, "model": _M}, method) == {"n": 3, "assistance_models": {method: _M}}


def test_it_overwrites_that_methods_entry_and_keeps_the_others():
    params = {"model": _M, "assistance_models": {"top_n": _UPLOADED, "human_as_a_tool": _UPLOADED}}
    assert fold(params, "top_n") == {
        "assistance_models": {"top_n": _M, "human_as_a_tool": _UPLOADED}
    }


def test_it_overwrites_a_cleared_entry():
    params = {"model": _M, "assistance_models": {"top_n": None}}
    assert fold(params, "top_n") == {"assistance_models": {"top_n": _M}}


def test_it_replaces_a_cleared_map():
    params = {"model": _M, "assistance_models": None}
    assert fold(params, "top_n") == {"assistance_models": {"top_n": _M}}


def test_none_fills_every_assisted_method():
    assert fold({"model": _M}, "none") == {
        "assistance_models": {"top_n": _M, "human_as_a_tool": _M}
    }


def test_none_fills_only_absent_entries():
    params = {"model": _M, "assistance_models": {"top_n": _UPLOADED}}
    assert fold(params, "none") == {
        "assistance_models": {"top_n": _UPLOADED, "human_as_a_tool": _M}
    }


def test_none_keeps_a_cleared_entry():
    params = {"model": _M, "assistance_models": {"top_n": None}}
    assert fold(params, "none") == {"assistance_models": {"top_n": None, "human_as_a_tool": _M}}


@pytest.mark.parametrize("model", [None, "", 5])
@pytest.mark.parametrize("method", ["top_n", "none"])
def test_a_null_empty_or_non_string_model_is_dropped(model, method):
    params = {"n": 3, "model": model, "assistance_models": {"top_n": _UPLOADED}}
    assert fold(params, method) == {"n": 3, "assistance_models": {"top_n": _UPLOADED}}


def test_a_lone_null_model_leaves_empty_params():
    assert fold({"model": None}, "top_n") == {}


def test_params_without_model_are_untouched():
    params = {"n": 3, "assistance_models": {"top_n": _UPLOADED}}
    assert fold(params, "top_n") is params


def test_it_does_not_mutate_its_input():
    params = {"model": _M, "assistance_models": {"top_n": _UPLOADED}}
    fold(params, "none")
    assert params == {"model": _M, "assistance_models": {"top_n": _UPLOADED}}
