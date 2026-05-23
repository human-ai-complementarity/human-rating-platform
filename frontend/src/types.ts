export interface Experiment {
  id: number;
  name: string;
  created_at: string;
  num_ratings_per_question: number;
  prolific_completion_url: string | null;
  question_count: number;
  rating_count: number;
  assistance_method: string;
  assistance_params: Record<string, unknown> | null;
}

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
}

export interface Session {
  rater_id: number;
  session_start: string;
  session_end_time: string;
  experiment_name: string;
  completion_url: string | null;
  rater_session_token: string;
  assistance_method: string;
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
  num_ratings_per_question: number;
  prolific_completion_url: string;
  prolific?: ProlificStudyConfig;
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
  created_at: string;
  prolific_study_url: string;
}

export interface ExperimentRoundUpdate {
  description?: string;
  estimated_completion_time?: number;
  reward?: number;
  places?: number;
  device_compatibility?: string[];
}

export interface PilotStudyCreate {
  description: string;
  estimated_completion_time: number;
  reward: number;
  pilot_places: number;
  device_compatibility: string[];
}

export interface RecommendationResponse {
  avg_time_per_question_seconds: number;
  remaining_rating_actions: number;
  total_hours_remaining: number;
  recommended_places: number;
  is_complete: boolean;
}
