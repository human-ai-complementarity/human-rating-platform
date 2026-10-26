"""`assistance_models` as a dataset_meta key: the wave's model entry per method.

One export file usually serves both arms of a wave, so it declares an entry
(model plus request options) per assisted method. The old single `model` key
is refused. A bare model id is still accepted here, and only here, for
exports written before entries existed: it expands to the entry it stood for.
"""

from __future__ import annotations

import io
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from config import get_settings


def _entry(model: str, **options) -> dict:
    return {
        "model": model,
        "reasoning_effort": None,
        "text_verbosity": None,
        "temperature": None,
        **options,
    }


_TOP_N = _entry("openrouter/anthropic/claude-sonnet-4.6", temperature=0)
_HAAT = _entry("openrouter/google/gemini-3-flash-preview")
_OTHER = _entry("oai/gpt-5.6-luna", reasoning_effort="low", text_verbosity="low")
_MODELS = {"top_n": _TOP_N, "human_as_a_tool": _HAAT}
_BOTH = ["assistance_models.human_as_a_tool", "assistance_models.top_n"]


def _experiment(client: TestClient, **payload) -> dict:
    payload.setdefault("name", "Assistance models meta")
    payload.setdefault("assistance_method", "top_n")
    resp = client.post("/api/admin/experiments", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _upload_csv(client: TestClient, experiment_id: int, meta: dict):
    csv_data = (
        f"#META: {json.dumps(meta)}\n"
        "question_id,question_text,gt_answer,options,question_type\n"
        "q1,Is this useful?,Yes,Yes|No,MC\n"
    )
    return client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("medqa_n1.csv", csv_data, "text/csv")},
    )


def _upload_parquet(client: TestClient, experiment_id: int, meta: dict):
    table = pa.Table.from_pylist(
        [{"question_id": "q1", "question_text": "Is this useful?", "question_type": "MC"}]
    ).replace_schema_metadata({b"dataset_meta": json.dumps(meta).encode("utf-8")})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("medqa_n1.parquet", buf.getvalue(), "application/octet-stream")},
    )


def _params(client: TestClient, experiment_id: int) -> dict | None:
    return client.get(f"/api/admin/experiments/{experiment_id}").json()["assistance_params"]


def _patch_params(client: TestClient, experiment_id: int, params: dict) -> None:
    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}",
        json={"assistance_method": "top_n", "assistance_params": params},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("upload", [_upload_csv, _upload_parquet])
def test_the_map_is_pinned_on_upload(client: TestClient, upload):
    exp = _experiment(client, assistance_params={"n": 4})
    resp = upload(client, exp["id"], {"assistance_models": _MODELS})

    assert resp.status_code == 200, resp.text
    assert resp.json()["meta_applied"] == _BOTH
    assert resp.json()["meta_conflicts"] == []
    assert _params(client, exp["id"]) == {"n": 4, "assistance_models": _MODELS}
    uploads = client.get(f"/api/admin/experiments/{exp['id']}/uploads").json()
    assert uploads[0]["dataset_meta"] == {"assistance_models": _MODELS}


@pytest.mark.parametrize("model", [_OTHER["model"], None])
@pytest.mark.parametrize("upload", [_upload_csv, _upload_parquet])
def test_the_removed_model_key_rejects_the_upload(client: TestClient, upload, model):
    exp = _experiment(client, assistance_params={"n": 4})
    resp = upload(client, exp["id"], {"model": model, "assistance_models": _MODELS})

    assert resp.status_code == 400
    assert "use 'assistance_models'" in resp.json()["detail"]
    # The file is at fault, not a stale admin page.
    assert "reload" not in resp.json()["detail"]
    assert _params(client, exp["id"]) == {"n": 4}


def test_an_upload_without_the_map_leaves_assistance_params_alone(client: TestClient):
    exp = _experiment(client, assistance_params={"n": 4})
    body = _upload_csv(client, exp["id"], {"description": "A guide."}).json()

    assert body["meta_applied"] == ["description"]
    assert _params(client, exp["id"]) == {"n": 4}


def test_a_method_already_set_is_reported_not_clobbered(client: TestClient):
    exp = _experiment(client, assistance_params={"assistance_models": {"top_n": _OTHER}})
    body = _upload_csv(client, exp["id"], {"assistance_models": _MODELS}).json()

    assert body["meta_applied"] == ["assistance_models.human_as_a_tool"]
    assert body["meta_conflicts"] == ["assistance_models.top_n"]
    assert _params(client, exp["id"])["assistance_models"] == {
        "top_n": _OTHER,
        "human_as_a_tool": _HAAT,
    }


def test_a_later_upload_agreeing_is_silent_and_disagreeing_is_reported(client: TestClient):
    exp = _experiment(client)
    _upload_csv(client, exp["id"], {"assistance_models": _MODELS})

    same = _upload_csv(client, exp["id"], {"assistance_models": _MODELS}).json()
    assert same["meta_applied"] == [] and same["meta_conflicts"] == []

    differs = _upload_csv(client, exp["id"], {"assistance_models": {"top_n": _OTHER}}).json()
    assert differs["meta_conflicts"] == ["assistance_models.top_n"]
    assert _params(client, exp["id"])["assistance_models"] == _MODELS


@pytest.mark.parametrize(
    ("cleared", "conflicts"),
    [({"top_n": None}, ["assistance_models.top_n"]), (None, _BOTH)],
)
def test_a_deliberately_cleared_entry_is_not_repinned(client: TestClient, cleared, conflicts):
    exp = _experiment(client)
    _patch_params(client, exp["id"], {"assistance_models": cleared})

    body = _upload_csv(client, exp["id"], {"assistance_models": _MODELS}).json()
    assert sorted(body["meta_conflicts"]) == conflicts
    if cleared is None:
        assert _params(client, exp["id"])["assistance_models"] is None
    else:
        assert _params(client, exp["id"])["assistance_models"]["top_n"] is None


def test_a_patch_of_other_params_keeps_the_map(client: TestClient):
    exp = _experiment(client, assistance_params={"n": 3})
    _upload_csv(client, exp["id"], {"assistance_models": _MODELS})

    _patch_params(client, exp["id"], {"n": 5})
    assert _params(client, exp["id"]) == {"n": 5, "assistance_models": _MODELS}


def test_a_patch_of_one_method_keeps_the_others_and_their_null_markers(client: TestClient):
    exp = _experiment(client)
    _upload_csv(client, exp["id"], {"assistance_models": _MODELS})

    _patch_params(client, exp["id"], {"assistance_models": {"top_n": None}})
    _patch_params(client, exp["id"], {"assistance_models": {"human_as_a_tool": _OTHER}})
    assert _params(client, exp["id"])["assistance_models"] == {
        "top_n": None,
        "human_as_a_tool": _OTHER,
    }

    body = _upload_csv(client, exp["id"], {"assistance_models": _MODELS}).json()
    assert body["meta_conflicts"] == _BOTH
    assert _params(client, exp["id"])["assistance_models"]["top_n"] is None


def test_the_map_inherits_the_config_lock(client: TestClient, sync_engine):
    exp = _experiment(client, assistance_params={"n": 3})
    _upload_csv(client, exp["id"], {"assistance_models": _MODELS})
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET status = 'LAUNCH' WHERE id = :id"), {"id": exp["id"]}
        )
    url = f"/api/admin/experiments/{exp['id']}"

    resent = client.patch(
        url, json={"assistance_method": "top_n", "assistance_params": _params(client, exp["id"])}
    )
    assert resent.status_code == 200, resent.text

    restated = client.patch(
        url,
        json={
            "assistance_method": "top_n",
            "assistance_params": {"assistance_models": {"top_n": _TOP_N}},
        },
    )
    assert restated.status_code == 200, restated.text

    changed = client.patch(
        url,
        json={
            "assistance_method": "top_n",
            "assistance_params": {"assistance_models": {**_MODELS, "top_n": _OTHER}},
        },
    )
    assert changed.status_code == 400
    assert "assistance_params" in changed.json()["detail"]


def test_the_response_reports_the_entry_each_method_would_run_on(client: TestClient):
    llm = get_settings().llm
    entry = _entry("oai/test/top-n-entry", reasoning_effort="high", temperature=0.3)
    assert entry["model"] not in {llm.default_model, llm.decomposition_model}
    haat_default = {**_entry(llm.decomposition_model), "tools": [], "source": "default"}
    top_n_default = {**_entry(llm.default_model, temperature=0), "tools": [], "source": "default"}

    exp = _experiment(client, assistance_method="none")
    assert exp["resolved_models"] == {"human_as_a_tool": haat_default, "top_n": top_n_default}

    _upload_csv(client, exp["id"], {"assistance_models": {"top_n": entry}})
    pinned = {
        "top_n": {**entry, "tools": [], "source": "assistance_models"},
        "human_as_a_tool": haat_default,
    }
    detail = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert detail["resolved_models"] == pinned
    listed = client.get("/api/admin/experiments").json()
    assert next(e for e in listed if e["id"] == exp["id"])["resolved_models"] == pinned

    resp = client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={
            "assistance_method": "none",
            "assistance_params": {"assistance_models": {"top_n": None}},
        },
    )
    assert resp.json()["resolved_models"]["top_n"] == top_n_default


@pytest.mark.parametrize(
    ("assistance_models", "expected"),
    [
        ({"none": _TOP_N}, "none"),
        ({"isd": _HAAT}, "isd"),
        ({"top_n": "claude-sonnet-4.6"}, "assistance_models.top_n"),
        ({"top_n": _entry("claude-sonnet-4-6")}, "assistance_models.top_n"),
        ({"human_as_a_tool": 3}, "assistance_models.human_as_a_tool"),
        ({"top_n": ""}, "assistance_models.top_n"),
        ({"top_n": None}, "assistance_models.top_n"),
        ({"top_n": {"model": _TOP_N["model"], "temperature": 0}}, "missing required key(s)"),
        ({"top_n": _entry(_TOP_N["model"], max_tokens=100)}, "unknown key(s) max_tokens"),
        ({"top_n": _entry(_TOP_N["model"], tools="web_search")}, "'tools' must be a list"),
        ({"top_n": _entry(_TOP_N["model"], tools=["browser"])}, "unknown tool(s) 'browser'"),
        (
            {"top_n": _entry(_TOP_N["model"], tools=["web_search", "web_search"])},
            "'tools' lists web_search more than once",
        ),
        (
            {"human_as_a_tool": _entry(_HAAT["model"], tools=["code_interpreter"])},
            "'assistance_models.human_as_a_tool': 'tools' has code_interpreter, which "
            "openrouter/google/gemini-3-flash-preview cannot use. OpenRouter can only do web search",
        ),
        ({"top_n": _entry(_TOP_N["model"], reasoning_effort="lots")}, "'reasoning_effort'"),
        ({"top_n": _entry(_TOP_N["model"], temperature=2.5)}, "'temperature'"),
        (_TOP_N["model"], "must be a JSON object"),
        ([_TOP_N], "must be a JSON object"),
    ],
)
@pytest.mark.parametrize("upload", [_upload_csv, _upload_parquet])
def test_a_bad_map_rejects_the_upload(client: TestClient, upload, assistance_models, expected):
    exp = _experiment(client)
    resp = upload(client, exp["id"], {"assistance_models": assistance_models})

    assert resp.status_code == 400
    assert expected in resp.json()["detail"]
    assert _params(client, exp["id"]) is None


@pytest.mark.parametrize("upload", [_upload_csv, _upload_parquet])
def test_a_bare_id_is_expanded_to_its_entry_on_upload(client: TestClient, upload):
    """One release's grace for exports written before entries existed: the id
    becomes the entry it stood for, with its method's default options."""
    exp = _experiment(client, assistance_method="none")
    bare = {"top_n": "openrouter/anthropic/claude-sonnet-4.6", "human_as_a_tool": _HAAT["model"]}
    resp = upload(client, exp["id"], {"assistance_models": bare})

    assert resp.status_code == 200, resp.text
    assert resp.json()["meta_applied"] == _BOTH
    expanded = {method: {**entry, "tools": []} for method, entry in _MODELS.items()}
    assert _params(client, exp["id"])["assistance_models"] == expanded

    # The same file again agrees with what it was expanded to.
    again = upload(client, exp["id"], {"assistance_models": bare}).json()
    assert again["meta_applied"] == [] and again["meta_conflicts"] == []
    # So does the same entry stamped in full, with or without its empty tools.
    for spelled in (_MODELS, expanded):
        full = upload(client, exp["id"], {"assistance_models": spelled}).json()
        assert full["meta_applied"] == [] and full["meta_conflicts"] == []


@pytest.mark.parametrize("upload", [_upload_csv, _upload_parquet])
def test_each_methods_tools_are_pinned_and_shown(client: TestClient, upload):
    """The arm's tools ride in with its entry and are what the method runs on."""
    tooled = {
        "top_n": {**_OTHER, "tools": ["web_search", "code_interpreter"]},
        "human_as_a_tool": {**_HAAT, "tools": ["web_search"]},
    }
    exp = _experiment(client, assistance_method="none")
    resp = upload(client, exp["id"], {"assistance_models": tooled})

    assert resp.status_code == 200, resp.text
    assert resp.json()["meta_applied"] == _BOTH
    assert _params(client, exp["id"])["assistance_models"] == tooled
    resolved = client.get(f"/api/admin/experiments/{exp['id']}").json()["resolved_models"]
    assert resolved == {
        method: {**entry, "source": "assistance_models"} for method, entry in tooled.items()
    }
    # Another file of the wave declaring other tools is a conflict, not a re-pin.
    other = {"top_n": {**_OTHER, "tools": ["web_search"]}}
    again = upload(client, exp["id"], {"assistance_models": other}).json()
    assert again["meta_conflicts"] == ["assistance_models.top_n"]


@pytest.mark.parametrize("upload", [_upload_csv, _upload_parquet])
def test_an_empty_tools_list_is_pinned_and_agrees_with_none(client: TestClient, upload):
    """`tools` is optional: an export that stamps `[]` declares the same entry
    as one that leaves it out, so neither re-pins nor conflicts with the other."""
    exp = _experiment(client, assistance_method="none")
    with_tools = {"top_n": {**_TOP_N, "tools": []}}
    resp = upload(client, exp["id"], {"assistance_models": with_tools})

    assert resp.status_code == 200, resp.text
    assert resp.json()["meta_applied"] == ["assistance_models.top_n"]
    assert _params(client, exp["id"])["assistance_models"] == with_tools
    again = upload(client, exp["id"], {"assistance_models": {"top_n": _TOP_N}}).json()
    assert again["meta_applied"] == [] and again["meta_conflicts"] == []
    other = upload(client, exp["id"], {"assistance_models": {"top_n": _OTHER}}).json()
    assert other["meta_conflicts"] == ["assistance_models.top_n"]
