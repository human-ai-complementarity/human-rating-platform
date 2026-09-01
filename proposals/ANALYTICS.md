# Analytics Roadmap

This document defines the staged approach to analytics — what we capture per rating, what makes it into the CSV, and how data quality is enforced.

The platform's primary job is to capture everything researchers might want and surface it in the export. Individual researchers run their own analysis offline. The exception — and where this platform can add real value — is **method-specific metrics**: reliance, take rate, calibration against AI confidence, etc. These are tied to method internals and benefit from being defined alongside the methods themselves.

---

# What we have today (v0)

### Capture

- Per-rating (`Rating`): `answer`, `confidence` (1–5), `time_started`, `time_submitted`, `assistance_session_id`
- Per-rater (`Rater`): `prolific_id`, `study_id`, `session_id`, `session_start`, `session_end`, `is_active`, `is_preview`
- Per-question (`Question`): `gt_answer`, `options`, `question_type`, `extra_data` (untyped JSON blob)
- Per-assistance-session (`AssistanceSession`, FK'd from `Rating`): `method_name`, `params`, `step_type`, `state`, `payload`, `is_complete`, `created_at`, `updated_at`

### Researcher-facing surfaces

- `GET /admin/experiments/{id}/stats` — fast counts: `total_questions`, `total_ratings`, `total_raters`, `questions_complete`
- `GET /admin/experiments/{id}/analytics` — averages of response time and confidence, grouped by question and by rater. Computed on demand, no caching.
- `GET /admin/experiments/{id}/export` — streaming CSV at the **per-rating grain** with columns: `rating_id, question_id, question_text, gt_answer, rater_prolific_id, rater_study_id, rater_session_id, answer, confidence, time_started, time_submitted, response_time_seconds`
- All three accept `include_preview` (default false)
- Frontend `Analytics.tsx`: three tables (Overview, Per-Question, Per-Rater), preview toggle, refresh button. No charts, no drill-down.

### Prolific round management (relevant to data-quality v3)

- `Round` model + `/admin/experiments/{id}/prolific/recommend` computes avg time per question from existing ratings and recommends hours for the next round
- No quality gates: doesn't account for rater reliability before scaling

### Limitations

- `AssistanceSession.state` and `payload` are captured but not in the export — method-specific data is locked in the database
- No focus / paste / answer-change / first-interaction telemetry — can't detect AI agents or fast-clickers beyond raw timing in offline analysis
- No `is_attention_check` on `Question`; no `flagged` on `Rater`
- The CSV doesn't include enough context for researchers to compute attention-check pass rates, reliance, or any method-specific metric

---

# v1 – Capture (pre-pilot)

The point of v1 is to make the CSV export complete enough that researchers can do whatever analysis they want offline.

### 1) Per-rating telemetry columns

Add to `Rating` (all nullable / default-zero so old rows still work):

- `first_interaction_at: datetime` — when the rater first clicked or typed
- `answer_change_count: int` — how many times the answer was changed before submit
- `tab_blur_count: int` — number of times the tab lost focus during this question
- `tab_blur_seconds: float` — total blurred time during this question
- `paste_count: int` — number of paste events into the answer field
- `client_metadata: text` — JSON for user agent, viewport, anything else we want later

Frontend changes in `QuestionCard.tsx` and `RaterView.tsx`:

- `first_interaction_at` set on first `onChange` of the answer input
- `answer_change_count` incremented on each value change (debounced for free-text)
- Tab blur tracked via `visibilitychange` listener scoped to the current question
- `paste_count` via `onPaste` listener

Backend: extend `RatingSubmit` schema and the `Rating` insert in `backend/services/rater/operations.py`. One Alembic migration. Add the new columns to the `EXPORT_COLUMNS` list in `backend/services/admin/exports.py` so the CSV carries them.

### 2) Assistance-session data in the export

`AssistanceSession.state` and `payload` are populated during data collection but never leave the database. Extend the CSV export to include per-rating columns for assistance method, params, and the relevant pieces of state/payload (flattened or as a JSON column).

This unblocks every reliance metric researchers might want without the platform having to compute any of them.

### 3) Confidence scale reconciliation

The live code uses 1–5 (`schemas.py`: `confidence: int = Field(ge=1, le=5)`). Prior data collection used a 0–100 slider with anchors at 0/50/100. Pick one before the pilot — irreversible after data collection starts.

**Goal:** every analysis researchers currently want is reachable from the CSV.

---

# v2 – Quality gates (post-pilot)

These are platform features, not analytics. They exist so that excluded raters don't pollute downstream analysis and so that data collection can react to quality problems mid-study.

### 1) Attention checks

- Add `is_attention_check: bool = False` to `Question`
- CSV upload accepts an `is_attention_check` column
- The flag flows through to the export so researchers can compute pass rates themselves
- Optional: surface per-rater `attention_check_pass_rate` on the existing rater table for at-a-glance review

Question content can be drawn from a standard list of attention-check items (e.g., "Please select Strongly disagree", "What is 3+5", "Please choose 25 on the slider").

### 2) Rater flagging

- Add `flagged: bool = False`, `flag_reason: text` to `Rater`
- Admin UI: flag button on each row of the rater table with a free-text reason
- Export includes `flagged` and `flag_reason` columns; researchers decide whether to filter
- Endpoint filter "include flagged raters" toggle, parallel to existing `includePreview`

### 3) Per-rater drill-down

Click a rater in the table → page showing every rating they submitted with question text, answer, gt, correctness (computed in the view), confidence, response time, blur events, paste count. This is what we'd actually use to decide whether to manually flag someone — the only piece of in-platform analytics that pays its own way.

**Goal:** make it cheap to identify and exclude bad raters mid-study, without the researcher having to re-export and re-grep on every refresh.

---

# v3 – Method-specific metrics

This is where being an in-house platform actually matters. We can compute metrics that depend on method internals — things only meaningful in the context of a specific assistance method — rather than punting all of it to offline CSV processing.

This work should be **driven by the methods subteam**, not by platform. Each method's implementer is best positioned to decide what's worth recording and surfacing for their method. Platform's job is to provide the primitives (assistance-session data in the export, hooks for method-specific dashboards) and a consistent place to surface the results.

A starting point for discussion:

### Reliance metrics

The exact set depends on the method:

**HumanAsATool** (single suggestion):
- **Take rate** — % of questions where the rater changed their answer to match the AI suggestion
- **Override rate** — % of questions where the rater stuck with their original answer despite a divergent suggestion
- **Outcome by reliance** — was reliance correlated with accuracy?

**Top-N** (N candidates, none privileged):
- **Hit rate** — % of questions where the rater's final answer was one of the N candidates
- **Selected rank distribution** — when the rater picks from the set, which position do they pick? (Catches anchoring on candidate 1.)
- **Off-list rate** — % of questions where the rater's answer is none of the candidates
- **Outcome by selection** — accuracy conditional on selected rank vs. off-list

If metrics are computed on demand, requires persisting what was shown to the rater — probably `AssistanceSession.presented_candidates` plus `presented_order` if randomized. Independent of compute strategy, persisting candidates is also useful for retrospective analysis of N-sweep experiments.

### Other future work

- **AI-agent detection signals** — variance of typing rhythm, mouse-movement entropy, time-to-first-interaction. Build the model offline against pilot data first; only ship a UI signal once we know what's predictive.
- **Auto-rerun after exclusion** — when a rater is filtered out, automatically request additional Prolific rating hours to backfill `num_ratings_per_question`. Slots into the existing `calculate_recommendation` logic in `backend/services/admin/rounds.py`, which already computes hours-needed from outstanding actions; v3 just teaches it to subtract excluded raters' contributions before recommending.
- **Cross-experiment dashboard** — rater performance across multiple studies; catches repeat low-quality raters.