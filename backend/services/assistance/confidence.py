"""Confidence estimation for assistance subtasks.

A ConfidenceEstimator takes a question and a list of subtasks (each with a
question and the AI's current best answer) and returns a confidence score
0–100 for each subtask.

Three implementations are provided:

  LLMConfidenceEstimator (default)
      Single-call self-reporting: asks the LLM to rate its own confidence for
      each subtask in one batch call. Fast and cheap.

  SamplingConfidenceEstimator
      Multi-sample clustering method ported from human-ai-complementarity:
      1. Generate K independent responses per subtask (with temperature > 0)
      2. Cluster the responses together with the subtask's ``my_answer``
         semantically, using a fast clustering model (one call)
      3. Confidence = summed self-reported confidence of the responses in
         my_answer's cluster, divided by K — so it is high only when the
         samples agree with the answer the rater is shown
      Without a my_answer, step 3 scores the winning (largest) cluster
      instead. More accurate than self-reporting but uses K+1 LLM calls per
      subtask. Configure via assistance_params: confidence_method="sampling".

  SelfConsistencyConfidenceEstimator
      K samples per subtask; confidence = fraction that match my_answer (or
      the majority answer, without one). No clustering call.
      Configure via assistance_params: confidence_method="self_consistency".
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from collections import Counter

from config import get_settings

from .llm import complete

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt: single-call self-report (LLMConfidenceEstimator)
# ---------------------------------------------------------------------------

_SELF_REPORT_SYSTEM = """\
You are a calibration assistant. Given a question and a list of subtasks with \
the AI's current best answer for each, rate the AI's confidence in each answer \
on a scale of 0–100:

  100 = completely certain, no meaningful chance of being wrong
   75 = fairly confident, minor residual doubt
   50 = uncertain, roughly a coin flip
    0 = completely uncertain, no real basis for the answer

Respond with JSON only — no explanation, no markdown fences:
{{"scores": [<score_for_index_0>, <score_for_index_1>, ...]}}\
"""

# ---------------------------------------------------------------------------
# Prompt: per-subtask sampling (SamplingConfidenceEstimator)
# ---------------------------------------------------------------------------

_SAMPLING_PROMPT = """\
CONTEXT (for reference only — do NOT answer this question):
{original_question}

---

YOUR TASK: Answer the following sub-question. Focus ONLY on this sub-question.

Sub-question: {subtask_question}

Respond in EXACTLY this format — no other text:
ANSWER: [your answer]
CONFIDENCE: [0-100]\
"""

_CLUSTERING_PROMPT = """\
Cluster the following answers to the same question by semantic equivalence \
(paraphrases, equivalent choices, and equivalent numeric values should share a cluster).

Question: {question}

Answers:
{numbered_answers}

Output ONLY valid JSON with one field "cluster_ids": a list of integers of \
length exactly {n}. Use labels 0..K-1. Semantically identical answers must \
share the same id.

Example: {{"cluster_ids": [0, 1, 0, 2]}}\
"""


# ---------------------------------------------------------------------------
# Shared sampling helper
# ---------------------------------------------------------------------------


# The instruments' own request options. The sampling estimators need variety
# across samples; clustering wants a deterministic grouping. Human-as-a-Tool
# records these per role (`role_models`) from the same values it passes here.
SAMPLING_TEMPERATURE = 0.7
CLUSTERING_TEMPERATURE = 0


async def _generate_samples(
    original_question: str,
    subtask_question: str,
    model: str,
    num_samples: int,
    temperature: float,
) -> list[dict]:
    """Generate ``num_samples`` independent responses in ANSWER:/CONFIDENCE: format."""
    settings = get_settings()
    prompt = _SAMPLING_PROMPT.format(
        original_question=original_question,
        subtask_question=subtask_question,
    )
    tasks = [
        complete(
            [{"role": "user", "content": prompt}],
            model=model,
            settings=settings.llm,
            temperature=temperature,
        )
        for _ in range(num_samples)
    ]
    raw_responses = await asyncio.gather(*tasks, return_exceptions=True)
    responses = []
    for raw in raw_responses:
        if isinstance(raw, Exception):
            logger.warning("Sample failed: %s", raw)
            continue
        parsed = _parse_answer_confidence(raw)
        if parsed:
            responses.append(parsed)
    return responses


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class ConfidenceEstimator(ABC):
    @abstractmethod
    async def estimate_batch(self, question_text: str, subtasks: list[dict]) -> list[int]:
        """Return a confidence score (0–100) for each subtask, in order.

        Args:
            question_text: The original question being answered.
            subtasks:      List of subtask dicts with at minimum ``question``
                           and ``my_answer`` keys.

        Returns:
            List of ints of length ``len(subtasks)``, each clamped to 0–100.
        """


# ---------------------------------------------------------------------------
# LLMConfidenceEstimator — single-call self-report
# ---------------------------------------------------------------------------


class LLMConfidenceEstimator(ConfidenceEstimator):
    """Asks the LLM to self-report confidence for all subtasks in one call."""

    def __init__(self, model: str) -> None:
        self._model = model

    async def estimate_batch(self, question_text: str, subtasks: list[dict]) -> list[int]:
        if not subtasks:
            return []

        settings = get_settings()
        subtask_lines = "\n\n".join(
            f"Subtask {st['index']}:\n"
            f"  Question: {st['question']}\n"
            f"  AI's answer: {st.get('my_answer') or '(none)'}"
            for st in subtasks
        )
        user_msg = f"Question: {question_text}\n\n{subtask_lines}"

        raw = await complete(
            [
                {"role": "system", "content": _SELF_REPORT_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            model=self._model,
            settings=settings.llm,
        )

        content = re.sub(r"```json?\n?|```\n?", "", raw).strip()
        try:
            scores = [int(s) for s in json.loads(content)["scores"]]
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            logger.warning("Failed to parse confidence scores: %r", raw)
            return [50] * len(subtasks)

        if len(scores) != len(subtasks):
            logger.warning(
                "Confidence score count mismatch: got %d, expected %d", len(scores), len(subtasks)
            )
            scores = (scores + [50] * len(subtasks))[: len(subtasks)]

        return [max(0, min(100, s)) for s in scores]


# ---------------------------------------------------------------------------
# SamplingConfidenceEstimator — K-sample + semantic clustering
# ---------------------------------------------------------------------------


class SamplingConfidenceEstimator(ConfidenceEstimator):
    """Multi-sample confidence estimator ported from human-ai-complementarity.

    For each subtask:
      1. Generate ``num_samples`` independent responses using ``sampling_model``
         at ``temperature`` > 0, each in ANSWER: / CONFIDENCE: format.
      2. Cluster the responses plus the subtask's ``my_answer`` semantically
         in one ``clustering_model`` call.
      3. Confidence = summed self-reported confidence of the responses in
         my_answer's cluster divided by the number of responses, expressed as
         0–100. All samples agreeing gives their mean confidence; each one
         that disagrees pulls the score down.

    With no ``my_answer``, step 3 falls back to the mean confidence of the
    winning (largest) cluster; if clustering fails, to a string-matching
    majority vote.

    All subtasks in a batch are processed concurrently.
    """

    def __init__(
        self,
        sampling_model: str,
        clustering_model: str,
        num_samples: int = 5,
        temperature: float = SAMPLING_TEMPERATURE,
        clustering_temperature: float = CLUSTERING_TEMPERATURE,
    ) -> None:
        self._sampling_model = sampling_model
        self._clustering_model = clustering_model
        self._num_samples = num_samples
        self._temperature = temperature
        self._clustering_temperature = clustering_temperature

    async def estimate_batch(self, question_text: str, subtasks: list[dict]) -> list[int]:
        if not subtasks:
            return []
        scores = await asyncio.gather(*[self._score_subtask(question_text, st) for st in subtasks])
        return list(scores)

    async def _score_subtask(self, question_text: str, subtask: dict) -> int:
        """Score a single subtask; returns 0–100."""
        responses = await self._sample(question_text, subtask["question"])
        if not responses:
            return 50

        my_answer = (subtask.get("my_answer") or "").strip()
        confidence = await self._cluster_and_score(subtask["question"], responses, my_answer)
        # Source uses 0–1; convert to 0–100
        score = confidence * 100 if confidence is not None else 50
        return max(0, min(100, round(score)))

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------

    async def _sample(self, original_question: str, subtask_question: str) -> list[dict]:
        return await _generate_samples(
            original_question,
            subtask_question,
            self._sampling_model,
            self._num_samples,
            self._temperature,
        )

    # ------------------------------------------------------------------
    # Clustering
    # ------------------------------------------------------------------

    async def _cluster_and_score(
        self, subtask_question: str, responses: list[dict], my_answer: str = ""
    ) -> float | None:
        """Cluster responses and score their agreement with ``my_answer`` (0–1).

        ``my_answer`` is clustered last, alongside the responses, so placing it
        costs no extra call. Without it, returns the mean confidence of the
        winning cluster.
        """
        answers = [r["answer"] for r in responses]
        to_cluster = [*answers, my_answer] if my_answer else answers
        semantic_ids = await self._cluster_answers(subtask_question, to_cluster)

        if semantic_ids is None:
            # Fallback: string-matching majority vote
            return _compute_direct_confidence(responses, _select_best_answer(responses))

        if my_answer:
            return _confidence_weighted_share(responses, semantic_ids[:-1], semantic_ids[-1])

        # Winning cluster = largest; tie-break by mean confidence
        cluster_counts = Counter(semantic_ids)
        winning_id = cluster_counts.most_common(1)[0][0]
        return _compute_direct_confidence_by_cluster(responses, semantic_ids, winning_id)

    async def _cluster_answers(self, question: str, answers: list[str]) -> list[int] | None:
        """Return semantic cluster ids for each answer, or None on failure."""
        if not answers:
            return None

        settings = get_settings()
        numbered = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(answers))
        prompt = _CLUSTERING_PROMPT.format(
            question=question,
            numbered_answers=numbered,
            n=len(answers),
        )

        try:
            raw = await complete(
                [{"role": "user", "content": prompt}],
                model=self._clustering_model,
                settings=settings.llm,
                temperature=self._clustering_temperature,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            logger.warning("Clustering call failed: %s", e)
            return None

        try:
            out = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Could not parse clustering response: %r", raw[:200])
            return None

        cluster_ids = out.get("cluster_ids")
        if not isinstance(cluster_ids, list) or len(cluster_ids) != len(answers):
            logger.warning("Invalid cluster_ids in response: %r", out)
            return None

        # Remap to contiguous 0..K-1
        remap: dict[int, int] = {}
        mapped = []
        for x in cluster_ids:
            if x not in remap:
                remap[x] = len(remap)
            mapped.append(remap[x])
        return mapped


# ---------------------------------------------------------------------------
# SelfConsistencyConfidenceEstimator — K-sample majority vote
# ---------------------------------------------------------------------------


class SelfConsistencyConfidenceEstimator(ConfidenceEstimator):
    """Confidence = fraction of K samples that agree with the subtask's ``my_answer``.

    Agreement is ``_matches_my_answer``. With no ``my_answer``, falls back to
    the fraction agreeing with the majority answer. Does not use
    self-reported confidence scores — purely based on how consistently the
    model gives the answer the rater is shown.
    Configure via assistance_params: confidence_method="self_consistency".
    """

    def __init__(
        self,
        sampling_model: str,
        num_samples: int = 5,
        temperature: float = SAMPLING_TEMPERATURE,
    ) -> None:
        self._sampling_model = sampling_model
        self._num_samples = num_samples
        self._temperature = temperature

    async def estimate_batch(self, question_text: str, subtasks: list[dict]) -> list[int]:
        if not subtasks:
            return []
        scores = await asyncio.gather(*[self._score_subtask(question_text, st) for st in subtasks])
        return list(scores)

    async def _score_subtask(self, question_text: str, subtask: dict) -> int:
        responses = await _generate_samples(
            question_text,
            subtask["question"],
            self._sampling_model,
            self._num_samples,
            self._temperature,
        )
        answers = [r["answer"] for r in responses]

        if not answers:
            return 50

        my_answer = (subtask.get("my_answer") or "").strip()
        if my_answer:
            agreeing = sum(_matches_my_answer(ans, my_answer, subtask) for ans in answers)
            confidence = agreeing / len(answers)
            return max(0, min(100, round(confidence * 100)))

        # No answer to check against: majority vote via simple equivalence
        clusters: list[list[int]] = []
        for i, ans in enumerate(answers):
            for cluster in clusters:
                if _simple_equivalence_check(ans, answers[cluster[0]]):
                    cluster.append(i)
                    break
            else:
                clusters.append([i])

        winning_size = max(len(c) for c in clusters)
        confidence = winning_size / len(answers)
        return max(0, min(100, round(confidence * 100)))


# ---------------------------------------------------------------------------
# Helpers (ported from compute_confidence.py / confidence_delegation.py)
# ---------------------------------------------------------------------------


def _parse_answer_confidence(response: str) -> dict | None:
    """Extract answer and confidence from ANSWER:/CONFIDENCE: format."""
    conf_match = re.search(r"(?i)CONFIDENCE\s*:\s*([0-9]*\.?[0-9]+)", response)
    if not conf_match:
        return None

    answer_match = re.search(r"(?i)ANSWER\s*:\s*(.+?)(?=\s*CONFIDENCE\s*:)", response, re.DOTALL)
    if not answer_match:
        return None

    try:
        conf = float(conf_match.group(1))
        if conf > 1.0:
            conf /= 100.0
        conf = max(0.0, min(1.0, conf))
    except ValueError:
        return None

    return {"answer": answer_match.group(1).strip(), "confidence": conf}


def _simple_equivalence_check(text1: str, text2: str) -> bool:
    """Lightweight semantic equivalence check (ported from compute_confidence.py)."""
    if not text1 or not text2:
        return False
    t1, t2 = text1.lower().strip(), text2.lower().strip()
    if t1 == t2:
        return True
    nums1 = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", t1)
    nums2 = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", t2)
    if nums1 and nums2:
        try:
            v1, v2 = float(nums1[-1]), float(nums2[-1])
            if abs(v1 - v2) / max(abs(v1), abs(v2), 1e-10) < 0.01:
                return True
        except ValueError:
            pass
    return False


def _matches_my_answer(answer: str, my_answer: str, subtask: dict) -> bool:
    """Whether a sample gives the subtask's pre-filled ``my_answer``.

    Samples see only the sub-question, so they answer free-form ("Yes, it
    does.") while ``my_answer`` is in the decomposer's normalised form ("yes",
    an option's text, a short phrase). Both sides are reduced to that form
    before ``_simple_equivalence_check``.
    """
    return _simple_equivalence_check(
        _answer_form(answer, subtask), _answer_form(my_answer, subtask)
    )


def _answer_form(text: str, subtask: dict) -> str:
    """Leading yes/no for binary subtasks, the longest option the text starts
    with for multiple choice; otherwise the text minus trailing punctuation."""
    text = text.strip()
    if subtask.get("type") == "binary":
        m = re.match(r"(yes|no)\b", text, re.IGNORECASE)
        if m:
            return m.group(1)
    elif subtask.get("type") == "multiple_choice":
        options = [
            opt.strip()
            for opt in subtask.get("options") or []
            if isinstance(opt, str)
            and opt.strip()
            and re.match(re.escape(opt.strip()) + r"(?!\w)", text, re.IGNORECASE)
        ]
        if options:
            return max(options, key=len)
    return text.rstrip(".!? \t\n")


def _select_best_answer(responses: list[dict]) -> str:
    """Majority-vote best answer using simple equivalence (fallback path)."""
    valid = [r for r in responses if r.get("answer")]
    if not valid:
        return ""
    answers = [r["answer"] for r in valid]
    clusters: list[list[int]] = []
    for i, ans in enumerate(answers):
        for cluster in clusters:
            if _simple_equivalence_check(ans, answers[cluster[0]]):
                cluster.append(i)
                break
        else:
            clusters.append([i])
    best_cluster = max(
        clusters,
        key=lambda c: (
            len(c),
            sum(valid[i].get("confidence", 0) for i in c) / len(c),
        ),
    )
    return answers[best_cluster[0]]


def _compute_direct_confidence(responses: list[dict], best_answer: str) -> float | None:
    """Average confidence of responses matching best_answer (0–1 scale)."""
    confs = [
        r["confidence"]
        for r in responses
        if r.get("confidence") is not None
        and _simple_equivalence_check(r.get("answer", ""), best_answer)
    ]
    if not confs:
        confs = [r["confidence"] for r in responses if r.get("confidence") is not None]
    return sum(confs) / len(confs) if confs else None


def _confidence_weighted_share(
    responses: list[dict], semantic_ids: list[int], cluster_id: int
) -> float:
    """Summed confidence of responses in ``cluster_id`` over all responses (0–1).

    Share of responses in the cluster times their mean confidence: 0 when no
    response lands in it.
    """
    in_cluster = sum(
        responses[i]["confidence"]
        for i, sid in enumerate(semantic_ids)
        if sid == cluster_id and responses[i].get("confidence") is not None
    )
    return in_cluster / len(responses)


def _compute_direct_confidence_by_cluster(
    responses: list[dict], semantic_ids: list[int], winning_cluster_id: int
) -> float | None:
    """Average confidence of responses in winning cluster (0–1 scale)."""
    confs = [
        responses[i]["confidence"]
        for i, sid in enumerate(semantic_ids)
        if sid == winning_cluster_id and responses[i].get("confidence") is not None
    ]
    if not confs:
        confs = [r["confidence"] for r in responses if r.get("confidence") is not None]
    return sum(confs) / len(confs) if confs else None
