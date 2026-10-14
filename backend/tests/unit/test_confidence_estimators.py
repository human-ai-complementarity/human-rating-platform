"""Tests for the sampling and self-consistency confidence estimators (#148).

Both score agreement with the subtask's ``my_answer`` — the answer the rater
sees pre-filled — not with whatever answer the samples settle on. The LLM is
mocked: sampling calls return canned ANSWER:/CONFIDENCE: responses, and the
clustering call groups the numbered answers by case-insensitive text.
"""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, patch

import pytest
from services.assistance.confidence import (
    SamplingConfidenceEstimator,
    SelfConsistencyConfidenceEstimator,
)

_COMPLETE = "services.assistance.confidence.complete"


def _fake_llm(samples: list[tuple[str, int]], clustering: str | None = None) -> AsyncMock:
    """Return a ``complete`` mock serving ``samples`` in order, then clustering.

    ``clustering`` overrides the clustering response (e.g. to make it fail);
    by default answers are clustered by case-insensitive text.
    """
    queue = [f"ANSWER: {a}\nCONFIDENCE: {c}" for a, c in samples]

    async def complete(messages, **kwargs):
        prompt = messages[0]["content"]
        if prompt.startswith("Cluster the following answers"):
            if clustering is not None:
                return clustering
            answers = re.findall(r"^\d+\. (.*)$", prompt, re.MULTILINE)
            labels: dict[str, int] = {}
            ids = [labels.setdefault(a.lower(), len(labels)) for a in answers]
            return json.dumps({"cluster_ids": ids})
        return queue.pop(0)

    return AsyncMock(side_effect=complete)


def _subtask(my_answer: str | None) -> dict:
    st = {"index": 0, "question": "What is the capital of France?"}
    if my_answer is not None:
        st["my_answer"] = my_answer
    return st


def _clustered_answers(mock: AsyncMock) -> list[str]:
    for call in mock.await_args_list:
        prompt = call.args[0][0]["content"]
        if prompt.startswith("Cluster the following answers"):
            return re.findall(r"^\d+\. (.*)$", prompt, re.MULTILINE)
    raise AssertionError("no clustering call")


# The bug in #148: most samples settle confidently on an answer that is not
# the pre-filled my_answer.
_MOSTLY_LYON = [("Lyon", 95)] * 4 + [("Paris", 60)]


# ---------------------------------------------------------------------------
# SamplingConfidenceEstimator
# ---------------------------------------------------------------------------


def _sampling() -> SamplingConfidenceEstimator:
    return SamplingConfidenceEstimator(
        sampling_model="sampler", clustering_model="clusterer", num_samples=5
    )


@pytest.mark.asyncio
async def test_sampling_high_when_samples_agree_with_my_answer():
    llm = _fake_llm([("Paris", 90), ("paris", 90), ("Paris", 90), ("Paris", 90), ("Paris", 90)])

    with patch(_COMPLETE, new=llm):
        scores = await _sampling().estimate_batch("Q?", [_subtask("Paris")])

    assert scores == [90]
    # my_answer rides along in the one clustering call: K samples + 1 clustering.
    assert llm.await_count == 6
    assert _clustered_answers(llm)[-1] == "Paris"


@pytest.mark.asyncio
async def test_sampling_low_when_samples_mostly_disagree_with_my_answer():
    llm = _fake_llm(_MOSTLY_LYON)

    with patch(_COMPLETE, new=llm):
        scores = await _sampling().estimate_batch("Q?", [_subtask("Paris")])

    # One of five samples agrees, at 60% confidence: 0.6 / 5. Scoring the
    # largest cluster (Lyon) instead would give 95.
    assert scores == [12]


@pytest.mark.asyncio
async def test_sampling_zero_when_no_sample_agrees_with_my_answer():
    llm = _fake_llm([("Lyon", 95)] * 5)

    with patch(_COMPLETE, new=llm):
        scores = await _sampling().estimate_batch("Q?", [_subtask("Paris")])

    assert scores == [0]


@pytest.mark.asyncio
@pytest.mark.parametrize("my_answer", [None, "", "   "])
async def test_sampling_without_my_answer_scores_largest_cluster(my_answer):
    llm = _fake_llm(_MOSTLY_LYON)

    with patch(_COMPLETE, new=llm):
        scores = await _sampling().estimate_batch("Q?", [_subtask(my_answer)])

    assert scores == [95]
    assert _clustered_answers(llm) == ["Lyon"] * 4 + ["Paris"]


@pytest.mark.asyncio
async def test_sampling_clustering_failure_keeps_majority_fallback():
    llm = _fake_llm(_MOSTLY_LYON, clustering="not json")

    with patch(_COMPLETE, new=llm):
        scores = await _sampling().estimate_batch("Q?", [_subtask("Paris")])

    # String-matching majority vote, as before: mean confidence of the Lyons.
    assert scores == [95]


# ---------------------------------------------------------------------------
# SelfConsistencyConfidenceEstimator
# ---------------------------------------------------------------------------


def _self_consistency() -> SelfConsistencyConfidenceEstimator:
    return SelfConsistencyConfidenceEstimator(sampling_model="sampler", num_samples=5)


@pytest.mark.asyncio
async def test_self_consistency_high_when_samples_agree_with_my_answer():
    llm = _fake_llm([("Paris", 50), ("paris", 50), ("Paris", 50), ("Paris", 50), ("Paris", 50)])

    with patch(_COMPLETE, new=llm):
        scores = await _self_consistency().estimate_batch("Q?", [_subtask("Paris")])

    assert scores == [100]


@pytest.mark.asyncio
async def test_self_consistency_low_when_samples_mostly_disagree_with_my_answer():
    llm = _fake_llm(_MOSTLY_LYON)

    with patch(_COMPLETE, new=llm):
        scores = await _self_consistency().estimate_batch("Q?", [_subtask("Paris")])

    # One of five samples matches my_answer. The majority (Lyon) would give 80.
    assert scores == [20]


@pytest.mark.asyncio
@pytest.mark.parametrize("my_answer", [None, ""])
async def test_self_consistency_without_my_answer_scores_majority(my_answer):
    llm = _fake_llm(_MOSTLY_LYON)

    with patch(_COMPLETE, new=llm):
        scores = await _self_consistency().estimate_batch("Q?", [_subtask(my_answer)])

    assert scores == [80]
