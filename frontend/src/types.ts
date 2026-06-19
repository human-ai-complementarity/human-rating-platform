export interface Experiment {
  id: number;
  name: string;
  internal_name: string | null;
  created_at: string;
  num_ratings_per_question: number;
  prolific_completion_url: string | null;
  question_count: number;
  rating_count: number;
  assistance_method: string;
  assistance_params: Record<string, unknown> | null;
  description: string | null;
  system_prompt: string | null;
  human_prompt_prefix: string | null;
  human_prompt_suffix: string | null;
  prolific_pool: string | null;
}

// Keys an upload may declare as dataset-level metadata (CSV `#META:` line or
// Parquet `dataset_meta` schema key). Kept in sync with DATASET_META_FIELDS in
// backend/services/admin/uploads.py.
export const DATASET_META_FIELDS = [
  'description',
  'system_prompt',
  'human_prompt_prefix',
  'human_prompt_suffix',
  'prolific_pool',
] as const;
export type DatasetMetaField = (typeof DATASET_META_FIELDS)[number];
export type DatasetMeta = Partial<Record<DatasetMetaField, string>>;

export type StudyLabel =
  | 'annotation'
  | 'survey'
  | 'decision_making_task'
  | 'writing_task'
  | 'interview'
  | 'other';

export type Screener = 'ai_taskers' | 'fact_checkers' | 'approval_rate';

export interface Question {
  id: number;
  question_id: string;
  question_text: string;
  options: string | null;
  question_type: string;
  parent_question_text?: string | null;
}

export interface ExperimentStats {
  experiment_name: string;
  total_questions: number;
  questions_complete: number;
  total_ratings: number;
  total_raters: number;
  target_ratings_per_question: number;
}

export interface Upload {
  id: number;
  filename: string;
  uploaded_at: string;
  question_count: number;
  dataset_meta: DatasetMeta | null;
}

// Response shape for POST /api/admin/experiments/{id}/upload.
// `meta_applied` lists fields the experiment picked up from this upload's
// dataset metadata. `meta_conflicts` lists fields whose declared value
// disagreed with the experiment's existing value — the existing value wins.
export interface UploadResponse {
  message: string;
  meta_applied: DatasetMetaField[];
  meta_conflicts: DatasetMetaField[];
}

export interface Session {
  rater_id: number;
  session_start: string;
  session_end_time: string;
  experiment_name: string;
  // Pre-rendered HTML (via the same Prolific-markdown converter the external
  // study description uses). Render with dangerouslySetInnerHTML on the splash.
  experiment_description_html: string | null;
  // Per-question framing rendered above (prefix) and below (suffix) the
  // question text. Constant for the session.
  human_prompt_prefix: string | null;
  human_prompt_suffix: string | null;
  completion_url: string | null;
  rater_session_token: string;
  assistance_method: string;
  assistance_instructions: string | null;
}

export interface RatingSubmit {
  question_id: number;
  answer: string;
  confidence: number;
  time_started: string;
  assistance_session_id?: number;
}

// ── Assistance ────────────────────────────────────────────────────────────────

export type SubtaskType = 'binary' | 'multiple_choice' | 'free_text' | 'rating_scale';

export interface Subtask {
  index: number;
  question: string;
  my_answer?: string;
  confidence?: number;
  type: SubtaskType;
  options: string[] | null;
}

export type AssistanceStepType = 'none' | 'display' | 'ask_input' | 'complete' | 'skip';

export interface AssistanceStep {
  session_id: number;
  type: AssistanceStepType;
  is_terminal: boolean;
  payload: {
    kind?: 'top_n' | string;
    top_n?: number;
    candidates?: Array<{
      rank: number;
      answer: string;
      confidence?: number;
      rationale?: string;
    }>;
    has_options?: boolean;
    subtasks?: Subtask[];
    iteration?: number;
    max_rounds?: number;
    confidence_threshold?: number;
    history?: Array<{ subtasks: Subtask[]; answers: Record<string, { answer: string; confidence?: number }> }>;
    synthesis?: { answer: string; reasoning: string } | null;
  };
}

export interface Analytics {
  experiment_name: string;
  overview: {
    total_ratings: number;
    total_questions: number;
    total_raters: number;
    avg_response_time_seconds: number;
    min_response_time_seconds?: number;
    max_response_time_seconds?: number;
    avg_confidence: number;
  };
  questions: QuestionAnalytics[];
  raters: RaterAnalytics[];
}

export interface QuestionAnalytics {
  question_id: string;
  question_text: string;
  num_ratings: number;
  avg_response_time_seconds: number;
  avg_confidence: number;
  answer_distribution: Record<string, number>;
}

export interface RaterAnalytics {
  prolific_id: string;
  study_id: string | null;
  session_start: string | null;
  num_ratings: number;
  total_response_time_seconds: number;
  avg_response_time_seconds: number;
  avg_confidence: number;
}

export interface ProlificStudyConfig {
  description: string;
  estimated_completion_time: number;
  reward: number;
  total_available_places: number;
  device_compatibility: string[];
}

export interface PlatformStatus {
  prolific_enabled: boolean;
  currency_code: string | null;
  currency_symbol: string | null;
}

export interface ExperimentCreate {
  name: string;
  internal_name?: string | null;
  num_ratings_per_question: number;
  prolific_completion_url: string;
  prolific?: ProlificStudyConfig;
  assistance_method?: string;
  assistance_params?: Record<string, unknown>;
}

export interface ExperimentRound {
  id: number;
  round_number: number;
  prolific_study_id: string;
  prolific_study_status: string;
  places_requested: number;
  description: string;
  estimated_completion_time: number;
  reward: number;
  device_compatibility: string[];
  study_label: StudyLabel | null;
  screeners: Screener[];
  created_at: string;
  prolific_study_url: string;
}

export interface ExperimentRoundUpdate {
  description?: string;
  estimated_completion_time?: number;
  reward?: number;
  places?: number;
  device_compatibility?: string[];
  study_label?: StudyLabel;
  screeners?: Screener[];
}

export interface PilotStudyCreate {
  description: string;
  estimated_completion_time: number;
  reward: number;
  pilot_places: number;
  device_compatibility: string[];
  study_label: StudyLabel;
  screeners: Screener[];
}

export interface RecommendationResponse {
  avg_time_per_question_seconds: number;
  remaining_rating_actions: number;
  total_hours_remaining: number;
  recommended_places: number;
  is_complete: boolean;
}
