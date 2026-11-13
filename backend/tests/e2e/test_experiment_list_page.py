"""The paged experiment list, GET /api/admin/experiments/page.

Covers the envelope (items, total, skip, limit, counts), limit bounds, every
filter, which filters `counts` ignores, search across group, dataset and tag
names, the needs-attention SQL agreeing with each row's own flag, and sort
orders that page stably.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from schemas import EXPERIMENT_PAGE_DEFAULT_LIMIT, EXPERIMENT_PAGE_MAX_LIMIT
from test_characterization import (
    _create_experiment,
    _get_next_question,
    _insert_round,
    _mark_experiment_status,
    _start_session,
    _submit,
    _unique_name,
    _upload_questions,
)

PAGE = "/api/admin/experiments/page"


def _page(client: TestClient, **params) -> dict:
    response = client.get(PAGE, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _ids(body: dict) -> list[int]:
    return [item["id"] for item in body["items"]]


def _make(client: TestClient, **fields) -> dict:
    """An experiment with the given create fields (a unique name by default)."""
    payload = {"name": _unique_name("experiment"), "num_ratings_per_question": 2, **fields}
    response = client.post("/api/admin/experiments", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _archive(client: TestClient, experiment_id: int) -> None:
    assert client.post(f"/api/admin/experiments/{experiment_id}/archive").status_code == 200


def _dataset(client: TestClient, name: str, waves: list[str]) -> dict:
    response = client.post("/api/admin/datasets", json={"name": name, "waves": waves})
    assert response.status_code == 200, response.text
    return response.json()


def _group(client: TestClient, name: str, dataset_id: int, wave: str) -> dict:
    response = client.post(
        "/api/admin/experiment-groups",
        json={"name": name, "dataset_id": dataset_id, "wave": wave},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- Envelope, paging and limits ----------------------------------------


def test_page_envelope_when_empty(client: TestClient):
    body = _page(client)
    assert body == {
        "items": [],
        "total": 0,
        "skip": 0,
        "limit": EXPERIMENT_PAGE_DEFAULT_LIMIT,
        "counts": {"DRAFT": 0, "LAUNCH": 0, "FINISHED": 0, "archived": 0, "needs_attention": 0},
    }


def test_page_route_leaves_single_experiment_route_working(client: TestClient):
    created = _create_experiment(client)
    response = client.get(f"/api/admin/experiments/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_pages_report_total_and_cover_every_row_once(client: TestClient):
    created = [_create_experiment(client)["id"] for _ in range(7)]

    seen: list[int] = []
    for skip in (0, 3, 6):
        body = _page(client, skip=skip, limit=3)
        assert body["total"] == 7
        assert body["skip"] == skip
        assert body["limit"] == 3
        seen.extend(_ids(body))

    # Newest first by default; no row repeats across pages and none is skipped.
    assert seen == sorted(created, reverse=True)

    past_the_end = _page(client, skip=50, limit=3)
    assert past_the_end["items"] == []
    assert past_the_end["total"] == 7


def test_page_items_are_full_experiment_responses(client: TestClient):
    created = _create_experiment(client)
    item = _page(client)["items"][0]
    listed = next(
        i for i in client.get("/api/admin/experiments").json() if i["id"] == created["id"]
    )
    assert item == listed


def test_limit_bounds(client: TestClient):
    for _ in range(3):
        _create_experiment(client)

    assert _page(client, limit=EXPERIMENT_PAGE_MAX_LIMIT)["limit"] == EXPERIMENT_PAGE_MAX_LIMIT
    assert len(_page(client, limit=1)["items"]) == 1

    for params in (
        {"limit": 0},
        {"limit": EXPERIMENT_PAGE_MAX_LIMIT + 1},
        {"skip": -1},
        {"sort": "nonsense"},
        {"archived": "maybe"},
        {"status": "PAUSED"},
    ):
        assert client.get(PAGE, params=params).status_code == 422, params


# --- Selection filters (status, archived, needs_attention) and counts ---


def test_archived_scope(client: TestClient):
    active = _create_experiment(client)
    archived = _create_experiment(client)
    _archive(client, archived["id"])

    assert _ids(_page(client)) == [active["id"]]
    assert _ids(_page(client, archived="exclude")) == [active["id"]]
    assert _ids(_page(client, archived="only")) == [archived["id"]]
    both = _page(client, archived="include")
    assert set(_ids(both)) == {active["id"], archived["id"]}
    assert both["total"] == 2


def test_status_filter_narrows_total_but_not_counts(client: TestClient, sync_engine):
    drafts = [_create_experiment(client)["id"] for _ in range(2)]
    launched = _create_experiment(client)["id"]
    _mark_experiment_status(sync_engine, launched, "LAUNCH")
    finished = _create_experiment(client)["id"]
    _mark_experiment_status(sync_engine, finished, "FINISHED")
    archived_draft = _create_experiment(client)["id"]
    _archive(client, archived_draft)

    expected_counts = {
        "DRAFT": 2,
        "LAUNCH": 1,
        "FINISHED": 1,
        "archived": 1,
        "needs_attention": 0,
    }
    for status, expected in (
        ("DRAFT", set(drafts)),
        ("LAUNCH", {launched}),
        ("FINISHED", {finished}),
    ):
        body = _page(client, status=status)
        assert set(_ids(body)) == expected
        assert body["total"] == len(expected)
        assert body["counts"] == expected_counts

    # Status and archived combine: an archived draft is a DRAFT among archived rows.
    body = _page(client, status="DRAFT", archived="only")
    assert _ids(body) == [archived_draft]
    assert body["counts"] == expected_counts

    body = _page(client, status="DRAFT", archived="include")
    assert set(_ids(body)) == {*drafts, archived_draft}


def test_needs_attention_filter_matches_each_rows_flag(client: TestClient, sync_engine):
    """Every case `compute_attention_reason` distinguishes, built from round
    rows: the SQL filter must pick out exactly the rows whose response says
    needs_attention, and count them."""

    def experiment(status: str, round_statuses: list[str]) -> int:
        experiment_id = _create_experiment(client)["id"]
        _mark_experiment_status(sync_engine, experiment_id, status)
        for number, round_status in enumerate(round_statuses):
            _insert_round(
                sync_engine,
                experiment_id=experiment_id,
                round_number=number,
                status=round_status,
            )
        return experiment_id

    flagged = {
        # A draft round waits to be published, whatever the experiment's status.
        experiment("DRAFT", ["UNPUBLISHED"]),
        experiment("LAUNCH", ["COMPLETED", "UNPUBLISHED"]),
        # Every round closed: launch another round, or finish (no questions,
        # so the target counts as met).
        experiment("LAUNCH", ["COMPLETED"]),
        experiment("LAUNCH", ["AWAITING_REVIEW", "COMPLETED"]),
    }
    unflagged = {
        experiment("DRAFT", []),
        # A closed pilot on a draft is not an action: the main round is next.
        experiment("DRAFT", ["COMPLETED"]),
        experiment("LAUNCH", []),
        # A round still collecting means wait.
        experiment("LAUNCH", ["COMPLETED", "ACTIVE"]),
        experiment("LAUNCH", ["PAUSED"]),
        # FINISHED is terminal, even with a stray draft round.
        experiment("FINISHED", ["UNPUBLISHED"]),
        experiment("FINISHED", ["COMPLETED"]),
    }
    # An archived row is flagged on its own, but sits outside the default
    # scope and outside counts.needs_attention, which covers active rows.
    archived_flagged = experiment("DRAFT", ["UNPUBLISHED"])
    _archive(client, archived_flagged)

    every = _page(client, archived="include", limit=EXPERIMENT_PAGE_MAX_LIMIT)
    by_flag = {item["id"]: item["needs_attention"] for item in every["items"]}
    assert {i for i, flag in by_flag.items() if flag} == flagged | {archived_flagged}

    only_flagged = _page(client, needs_attention=True)
    assert set(_ids(only_flagged)) == flagged
    assert only_flagged["total"] == len(flagged)
    assert all(item["attention_reason"] for item in only_flagged["items"])

    only_unflagged = _page(client, needs_attention=False)
    assert set(_ids(only_unflagged)) == unflagged
    assert only_unflagged["total"] == len(unflagged)

    assert only_flagged["counts"]["needs_attention"] == len(flagged)
    assert only_unflagged["counts"]["needs_attention"] == len(flagged)
    assert _page(client, archived="only", needs_attention=True)["total"] == 1

    # Combined with a status: flagged LAUNCH rows only.
    launch_flagged = _page(client, needs_attention=True, status="LAUNCH")
    assert launch_flagged["total"] == 3


# --- Scope filters (group, wave, dataset, tag, method, search) ----------


def test_group_wave_and_dataset_filters(client: TestClient):
    medqa = _dataset(client, "medqa", ["fall25", "sp26"])
    swe = _dataset(client, "swe-bench", ["fall25"])
    medqa_fall = _group(client, "MedQA Fall", medqa["id"], "fall25")
    medqa_spring = _group(client, "MedQA Spring", medqa["id"], "sp26")
    swe_fall = _group(client, "SWE Fall", swe["id"], "fall25")

    in_medqa_fall = _make(client, group_id=medqa_fall["id"])["id"]
    in_medqa_spring = _make(client, group_id=medqa_spring["id"])["id"]
    in_swe_fall = _make(client, group_id=swe_fall["id"])["id"]
    ungrouped = _make(client)["id"]

    assert _ids(_page(client, group_id=medqa_fall["id"])) == [in_medqa_fall]
    assert _ids(_page(client, ungrouped=True)) == [ungrouped]
    assert set(_ids(_page(client, wave="FALL25"))) == {in_medqa_fall, in_swe_fall}
    assert set(_ids(_page(client, dataset_id=medqa["id"]))) == {in_medqa_fall, in_medqa_spring}
    both = _page(client, dataset_id=medqa["id"], wave="sp26")
    assert _ids(both) == [in_medqa_spring]
    assert both["total"] == 1

    # Scope filters narrow the counts too.
    assert _page(client, dataset_id=medqa["id"])["counts"]["DRAFT"] == 2
    assert _page(client, ungrouped=True)["counts"]["DRAFT"] == 1

    conflict = client.get(PAGE, params={"group_id": medqa_fall["id"], "ungrouped": True})
    assert conflict.status_code == 400
    assert "ungrouped" in conflict.json()["detail"]


def test_tag_filter_is_exact_and_case_insensitive(client: TestClient):
    tagged = _make(client, tags=["Pilot"])["id"]
    _make(client, tags=["pilot-2"])
    _make(client)

    assert _ids(_page(client, tag="pilot")) == [tagged]
    assert _ids(_page(client, tag="PILOT")) == [tagged]
    assert _page(client, tag="pil")["total"] == 0


def test_assistance_method_filter(client: TestClient):
    assisted = _make(client, assistance_method="top_n")["id"]
    control = _make(client)["id"]

    assert _ids(_page(client, assistance_method="top_n")) == [assisted]
    assert _ids(_page(client, assistance_method="none")) == [control]
    assert _page(client, assistance_method="human_as_a_tool")["total"] == 0


def test_search_matches_names_group_dataset_and_tags(client: TestClient):
    dataset = _dataset(client, "Zebrafish-QA", ["fall25"])
    group = _group(client, "Kestrel collection", dataset["id"], "fall25")

    by_public = _make(client, name="Walrus public name")["id"]
    by_internal = _make(client, internal_name="Walrus internal")["id"]
    in_group = _make(client, group_id=group["id"])["id"]
    by_tag = _make(client, tags=["heron-project"])["id"]
    _make(client, name="Unrelated")

    assert set(_ids(_page(client, search="walrus"))) == {by_public, by_internal}
    # Group and dataset names reach every experiment in the group.
    assert _ids(_page(client, search="kestrel")) == [in_group]
    assert _ids(_page(client, search="zebrafish")) == [in_group]
    # Tags match on a substring here, unlike the exact `tag` filter.
    assert _ids(_page(client, search="HERON")) == [by_tag]

    # The search narrows counts as well as items.
    assert _page(client, search="walrus")["counts"]["DRAFT"] == 2


def test_search_treats_wildcards_literally(client: TestClient):
    literal = _make(client, name="Top 10% study")["id"]
    _make(client, name="Top 100 study")

    assert _ids(_page(client, search="10%")) == [literal]
    assert _page(client, search="   ")["total"] == 2


# --- Sorting ------------------------------------------------------------


def test_sort_oldest_and_newest(client: TestClient):
    created = [_create_experiment(client)["id"] for _ in range(4)]
    assert _ids(_page(client)) == sorted(created, reverse=True)
    assert _ids(_page(client, sort="newest")) == sorted(created, reverse=True)
    assert _ids(_page(client, sort="oldest")) == sorted(created)


def test_sort_name_prefers_internal_name_and_ignores_case(client: TestClient):
    charlie = _make(client, name="aaa public", internal_name="Charlie")["id"]
    alpha = _make(client, name="alpha")["id"]
    bravo = _make(client, name="Bravo")["id"]

    assert _ids(_page(client, sort="name")) == [alpha, bravo, charlie]


def test_sort_spend(client: TestClient, sync_engine):
    cheap = _create_experiment(client)["id"]
    _insert_round(sync_engine, experiment_id=cheap, round_number=0, total_cost=100)
    dear = _create_experiment(client)["id"]
    _insert_round(sync_engine, experiment_id=dear, round_number=0, total_cost=300)
    _insert_round(sync_engine, experiment_id=dear, round_number=1, total_cost=None)
    unsynced = _create_experiment(client)["id"]

    items = _page(client, sort="spend")["items"]
    assert [item["id"] for item in items] == [dear, cheap, unsynced]
    assert [item["spend_minor_units"] for item in items] == [300, 100, 0]


def test_sort_ratings(client: TestClient):
    def rated(count: int) -> int:
        experiment_id = _create_experiment(client)["id"]
        _upload_questions(client, experiment_id)
        if count:
            session = _start_session(client, experiment_id, prolific_pid=f"PID_{experiment_id}")
            for _ in range(count):
                question = _get_next_question(client, session)
                assert question is not None
                _submit(client, session, question)
        return experiment_id

    one = rated(1)
    none = rated(0)
    two = rated(2)

    items = _page(client, sort="ratings")["items"]
    assert [item["id"] for item in items] == [two, one, none]
    assert [item["rating_count"] for item in items] == [2, 1, 0]


def test_every_sort_pages_stably_through_ties(client: TestClient):
    # Same name, no spend, no ratings: every sort key ties, so only the id
    # tie-breaker keeps pages from overlapping or skipping rows.
    created = {_make(client, name="Same name")["id"] for _ in range(5)}

    for sort in ("newest", "oldest", "name", "spend", "ratings"):
        seen: list[int] = []
        for skip in range(0, 6, 2):
            seen.extend(_ids(_page(client, sort=sort, skip=skip, limit=2)))
        assert len(seen) == len(set(seen)) == 5, sort
        assert set(seen) == created, sort

    assert _ids(_page(client, sort="name")) == sorted(created)
    assert _ids(_page(client, sort="spend")) == sorted(created, reverse=True)
