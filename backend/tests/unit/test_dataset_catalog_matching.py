from __future__ import annotations

from services.admin.dataset_catalog import PIPELINE_DATASETS, infer_wave, match_card_name


def test_catalog_has_scheduled_cards_only():
    names = [name for name, _ in PIPELINE_DATASETS]
    assert len(names) == len(set(name.lower() for name in names))
    assert "trace_sample" not in names
    assert "gpqa_diamond" in names
    assert "culturalbench_hard" in names
    assert "find_the_flaws_cels_lojban_match" in names


def test_match_card_name_uses_pipeline_export_prefix():
    assert match_card_name("culturalbench_hard_n300.parquet") == "culturalbench_hard"
    assert match_card_name("culturalbench_hard.csv") == "culturalbench_hard"
    assert match_card_name("CULTURALBENCH_HARD_n1.CSV") == "culturalbench_hard"
    assert match_card_name("exports/culturalbench_hard_n300.parquet") == "culturalbench_hard"


def test_match_card_name_prefers_longest_card():
    """Longest-wins, exercised against explicit names rather than the roster.

    No card currently in PIPELINE_DATASETS is a prefix of another — the pair
    that used to demonstrate this (safeagentbench / safeagentbench_abstracted)
    left the roster when the snapshot was corrected. The guarantee still has to
    hold for the next such pair, so pin it directly on the function.
    """
    names = ["safeagentbench", "safeagentbench_abstracted"]
    assert match_card_name("safeagentbench_abstracted_n10.parquet", names) == (
        "safeagentbench_abstracted"
    )
    assert match_card_name("safeagentbench_n10.parquet", names) == "safeagentbench"


def test_match_card_name_matches_real_cards():
    assert match_card_name("bbeh_safety_n50.csv") == "bbeh_safety"
    assert match_card_name("bbeh_mini_n50.csv") == "bbeh_mini"


def test_match_card_name_accepts_the_export_shapes():
    # optional wave infix between card and count
    assert match_card_name("shade_arena_fall25_n20.parquet") == "shade_arena"
    # anything after the count
    assert match_card_name("gpqa_diamond_n20_seed1.csv") == "gpqa_diamond"
    assert match_card_name("gpqa_diamond_n20.tar.gz") == "gpqa_diamond"


def test_match_card_name_only_accepts_the_export_shape():
    """A name that merely starts with a card is not that card's export.

    The earlier `{card}_*` prefix rule attached these to the shorter card once
    the longer one left the roster.
    """
    assert match_card_name("safeagentbench_abstracted_n10.parquet") is None
    assert match_card_name("primevul_cwe_n300.parquet") is None
    assert match_card_name("primevul_audit_n300.parquet") is None
    assert match_card_name("gpqa_diamond_v2.csv") is None
    # a wave-looking infix that isn't a known token is not an infix
    assert match_card_name("shade_arena_winter30_n20.parquet") is None


def test_match_card_name_rejects_unrelated_files():
    assert match_card_name("questions.csv") is None
    assert match_card_name("sample_questions.csv") is None
    assert match_card_name("culturalbench.csv") is None


def test_infer_wave_singleton_wins():
    assert infer_wave(["no wave here"], ["sp26"]) == "sp26"


def test_infer_wave_reads_token_from_text():
    assert infer_wave(["bbeh mini sp26 rerun"], ["fall25", "sp26"]) == "sp26"
    assert infer_wave(["shade_arena_fall25_n20.parquet"], ["fall25", "sp26"]) == "fall25"


def test_infer_wave_ambiguous_or_missing_is_none():
    assert infer_wave(["no signal"], ["fall25", "sp26"]) is None
    assert infer_wave(["fall25 and sp26"], ["fall25", "sp26"]) is None
    assert infer_wave(["sum26 only"], ["fall25", "sp26"]) is None
