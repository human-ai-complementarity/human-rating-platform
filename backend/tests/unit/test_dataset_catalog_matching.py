from __future__ import annotations

from datetime import UTC, datetime

from models import Experiment
from services.admin.dataset_catalog import (
    COLLECTIONS,
    PIPELINE_DATASETS,
    Collection,
    _check_collection,
    match_card_name,
    wave_tokens,
)

ROSTER = [name for name, _ in PIPELINE_DATASETS]

# `IDS` from the pipeline's scripts/sp26_pull_platform_ratings.sh at the
# snapshot commit, copied verbatim: "the id list below IS the wave".
SP26_PULL_IDS = (
    "65 84 124 67 117 127 68 80 128 71 85 126 72 120 131 75 122 125 83 121 130 82 123 129 "
    "135 136 236 133 134"
)


def test_roster_has_scheduled_cards_only():
    assert len(ROSTER) == len({name.lower() for name in ROSTER})
    assert "trace_sample" not in ROSTER
    # Excluded from sum26 by inference-pipeline #187; no other wave schedules it.
    assert "steganographic_collusion" not in ROSTER
    # Collected in sp26 but no longer scheduled: a collection, not a roster card.
    assert "safeagentbench_abstracted" not in ROSTER
    assert {"gpqa_diamond", "culturalbench_hard", "find_the_flaws_cels_lojban_match"} <= set(ROSTER)


def test_sp26_collections_are_exactly_the_pipeline_pull():
    sp26 = {eid for eid, collection in COLLECTIONS.items() if collection.wave == "sp26"}
    assert sp26 == {int(eid) for eid in SP26_PULL_IDS.split()}


def test_every_collection_names_a_known_card_arm_and_wave():
    collected_only = {"safeagentbench_abstracted"}
    for eid, collection in COLLECTIONS.items():
        assert collection.card in set(ROSTER) | collected_only, eid
        assert collection.arm in {"none", "top_n", "human_as_a_tool"}, eid
        assert collection.wave in {"sp26", "sum26"}, eid
    assert {eid for eid, c in COLLECTIONS.items() if c.wave == "sum26"} == {76, 78}


def test_match_card_name_uses_pipeline_export_prefix():
    assert match_card_name("culturalbench_hard_n300.parquet", ROSTER) == "culturalbench_hard"
    assert match_card_name("culturalbench_hard.csv", ROSTER) == "culturalbench_hard"
    assert match_card_name("CULTURALBENCH_HARD_n1.CSV", ROSTER) == "culturalbench_hard"
    assert (
        match_card_name("exports/culturalbench_hard_n300.parquet", ROSTER) == "culturalbench_hard"
    )
    # card names are matched case-insensitively and returned verbatim
    assert match_card_name("facts_search_public_n300.csv", ROSTER) == "FACTS_search_public"


def test_match_card_name_gives_each_card_only_its_own_exports():
    names = ["safeagentbench", "safeagentbench_abstracted"]
    assert match_card_name("safeagentbench_abstracted_n10.parquet", names) == (
        "safeagentbench_abstracted"
    )
    assert match_card_name("safeagentbench_n10.parquet", names) == "safeagentbench"


def test_match_card_name_refuses_a_name_that_fits_two_cards():
    assert match_card_name("x_n1.csv", ["x", "x_n1"]) is None


def test_match_card_name_accepts_anything_after_the_count():
    assert match_card_name("liars_bench_n464_fixed.csv", ROSTER) == "liars_bench"
    assert match_card_name("gpqa_diamond_n20.tar.gz", ROSTER) == "gpqa_diamond"


def test_match_card_name_only_accepts_the_export_shape():
    """A name that merely starts with a card is not that card's export.

    A `{card}_*` prefix rule attached these to the shorter card.
    """
    assert match_card_name("safeagentbench_abstracted_n10.parquet", ROSTER) is None
    assert match_card_name("primevul_cwe_n300.parquet", ROSTER) is None
    assert match_card_name("primevul_audit_n300.parquet", ROSTER) is None
    assert match_card_name("gpqa_diamond_v2.csv", ROSTER) is None
    # nothing goes between card and count; export_study.py never writes a wave there
    assert match_card_name("shade_arena_fall25_n20.parquet", ROSTER) is None


def test_match_card_name_rejects_unrelated_files():
    assert match_card_name("questions.csv", ROSTER) is None
    assert match_card_name("sample_questions.csv", ROSTER) is None
    assert match_card_name("culturalbench.csv", ROSTER) is None


def test_wave_tokens_finds_every_token_case_insensitively():
    assert wave_tokens(["SUM26 - MARS - find_the_flaws"]) == {"sum26"}
    assert wave_tokens(["shade arena", "shade_arena_fall25_n20.parquet"]) == {"fall25"}
    assert wave_tokens(["fall25 and sp26"]) == {"fall25", "sp26"}
    assert wave_tokens(["SPAR - Shade Arena - ISD", "shade_arena_n106.csv"]) == set()


def _experiment(**fields) -> Experiment:
    fields.setdefault("name", "run")
    fields.setdefault("assistance_method", "none")
    return Experiment(num_ratings_per_question=1, **fields)


LONGBENCH = Collection("longbenchv2", "none", "sp26")


def test_check_collection_accepts_a_matching_experiment():
    assert _check_collection(_experiment(), ["longbenchv2_n392.csv"], LONGBENCH, ROSTER) is None


def test_check_collection_refuses_an_archived_experiment():
    archived = _experiment(archived_at=datetime.now(UTC))
    assert _check_collection(archived, ["longbenchv2_n392.csv"], LONGBENCH, ROSTER) == (
        "archived",
        "",
    )


def test_check_collection_refuses_uploads_of_another_card():
    reason, detail = _check_collection(_experiment(), ["longsafety_n569.csv"], LONGBENCH, ROSTER)
    assert reason == "card_mismatch"
    assert "longsafety_n569.csv" in detail
    reason, _ = _check_collection(_experiment(), ["questions.csv"], LONGBENCH, ROSTER)
    assert reason == "card_mismatch"


def test_check_collection_refuses_any_upload_that_is_not_the_listed_card():
    """Every upload must agree, not just one of them."""
    for extra in ("longsafety_n569.csv", "questions.csv"):
        reason, detail = _check_collection(
            _experiment(), ["longbenchv2_n392.csv", extra], LONGBENCH, ROSTER
        )
        assert reason == "card_mismatch"
        assert extra in detail
        assert "longbenchv2_n392.csv" not in detail


def test_check_collection_refuses_an_experiment_without_uploads():
    reason, detail = _check_collection(_experiment(), [], LONGBENCH, ROSTER)
    assert reason == "card_mismatch"
    assert "no uploads" in detail


def test_check_collection_refuses_another_arm():
    top_n = _experiment(assistance_method="top_n")
    reason, detail = _check_collection(top_n, ["longbenchv2_n392.csv"], LONGBENCH, ROSTER)
    assert reason == "arm_mismatch"
    assert "top_n" in detail


def test_check_collection_refuses_names_carrying_another_wave():
    named = _experiment(internal_name="SUM26 - Long Bench V2")
    reason, detail = _check_collection(named, ["longbenchv2_n392.csv"], LONGBENCH, ROSTER)
    assert reason == "wave_conflict"
    assert "sum26" in detail
    # the recorded wave's own token is agreement, not conflict
    agreeing = _experiment(internal_name="SP26 - Long Bench V2")
    assert _check_collection(agreeing, ["longbenchv2_n392.csv"], LONGBENCH, ROSTER) is None
    # ...unless another wave's token appears beside it
    both = _experiment(name="SP26 Long Bench V2", internal_name="SUM26 rerun")
    reason, detail = _check_collection(both, ["longbenchv2_n392.csv"], LONGBENCH, ROSTER)
    assert reason == "wave_conflict"
    assert "sp26, sum26" in detail
