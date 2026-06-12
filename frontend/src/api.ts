// API client for the human rating platform backend.
//
// All routes are relative to /api — the base URL is resolved once at startup
// from VITE_API_HOST. Empty = same-origin /api (local dev via Vite proxy).
// Non-empty = cross-origin {origin}/api (e.g. Render deployment).

import type {
  Analytics,
  AssistanceStep,
  ExperimentRound,
  ExperimentRoundUpdate,
  Experiment,
  ExperimentCreate,
  ExperimentStats,
  PilotStudyCreate,
  PlatformStatus,
  Question,
  RatingSubmit,
  RecommendationResponse,
  Session,
  Upload,
} from './types';

// ── Response types ───────────────────────────────────────────────────────────
// API-layer response shapes. These live here (not in types.ts) because they're
// wire formats specific to request/response handling, not domain types shared
// with components.

type MessageResponse = { message: string };
type SubmitRatingResponse = { id: number; success: boolean };
type SessionStatusResponse = {
  is_active: boolean;
  time_remaining_seconds: number;
  questions_completed: number;
};

// ── Request types ────────────────────────────────────────────────────────────

type QueryValue = string | number | boolean | null | undefined;
type QueryParams = Record<string, QueryValue>;
type HttpMethod = 'GET' | 'POST' | 'DELETE' | 'PATCH';

type RequestOptions = {
  method?: HttpMethod;
  query?: QueryParams;
  json?: unknown; // mutually exclusive with formData
  formData?: FormData; // mutually exclusive with json
  headers?: Record<string, string>;
};

// ── Constants ────────────────────────────────────────────────────────────────

const API_PREFIX = '/api';
const JSON_CONTENT_TYPE = 'application/json';

// ── Routes ───────────────────────────────────────────────────────────────────
// Paths are relative to the API mount point (/api). buildUrl() prepends the
// resolved base, so these never include /api themselves.

const routes = {
  admin: {
    experiments: '/admin/experiments',
    experiment: (id: number) => `/admin/experiments/${id}`,
    upload: (id: number) => `/admin/experiments/${id}/upload`,
    uploads: (id: number) => `/admin/experiments/${id}/uploads`,
    stats: (id: number) => `/admin/experiments/${id}/stats`,
    analytics: (id: number) => `/admin/experiments/${id}/analytics`,
    export: (id: number) => `/admin/experiments/${id}/export`,
    authLogin: '/admin/auth/login',
    authLogout: '/admin/auth/logout',
    platformStatus: '/admin/platform-status',
    prolificPilot: (id: number) => `/admin/experiments/${id}/prolific/pilot`,
    prolificRecommend: (id: number) => `/admin/experiments/${id}/prolific/recommend`,
    prolificRounds: (id: number) => `/admin/experiments/${id}/prolific/rounds`,
    prolificRound: (experimentId: number, roundId: number) =>
      `/admin/experiments/${experimentId}/prolific/rounds/${roundId}`,
    prolificRoundPublish: (experimentId: number, roundId: number) =>
      `/admin/experiments/${experimentId}/prolific/rounds/${roundId}/publish`,
    prolificRoundClose: (experimentId: number, roundId: number) =>
      `/admin/experiments/${experimentId}/prolific/rounds/${roundId}/close`,
  },
  rater: {
    start: '/raters/start',
    nextQuestion: '/raters/next-question',
    submit: '/raters/submit',
    sessionStatus: '/raters/session-status',
    endSession: '/raters/end-session',
    assistanceStart: '/raters/assistance/start',
    assistanceAdvance: '/raters/assistance/advance',
  },
} as const;

// ── URL resolution ───────────────────────────────────────────────────────────
// Resolves the API base URL once at module load. Strict validation prevents
// silent misconfiguration — the most common deployment failure is a bad host
// that causes the SPA to serve index.html for API routes, producing cryptic
// HTML-parse errors instead of actionable feedback.

function resolveApiBase(rawHost: string): string {
  const host = rawHost.trim();
  if (!host) {
    return API_PREFIX;
  }

  let parsed: URL;
  try {
    parsed = new URL(host);
  } catch {
    throw new Error(
      `Invalid VITE_API_HOST '${rawHost}'. Expected an origin like 'https://api.example.com'.`
    );
  }

  if (parsed.pathname !== '/' || parsed.search || parsed.hash) {
    throw new Error(
      `Invalid VITE_API_HOST '${rawHost}'. Use origin only (no path/query/hash), e.g. 'https://api.example.com'.`
    );
  }

  return `${parsed.origin}${API_PREFIX}`;
}

const API_BASE = resolveApiBase(import.meta.env.VITE_API_HOST || '');

// ── URL building ─────────────────────────────────────────────────────────────

function ensureLeadingSlash(path: string): string {
  return path.startsWith('/') ? path : `/${path}`;
}

function buildQueryString(params: QueryParams = {}): string {
  const searchParams = new URLSearchParams();

  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined) {
      searchParams.set(key, String(value));
    }
  }

  return searchParams.toString();
}

function buildUrl(path: string, query?: QueryParams): string {
  const normalized = ensureLeadingSlash(path);
  const qs = buildQueryString(query);
  return qs ? `${API_BASE}${normalized}?${qs}` : `${API_BASE}${normalized}`;
}

// ── Error handling ───────────────────────────────────────────────────────────
// The most common failure mode in deployment is routing misconfiguration: the
// SPA serves index.html for unknown paths, so API calls get HTML back instead
// of JSON. We detect this explicitly and surface an actionable hint rather than
// letting the caller see a cryptic JSON.parse error.

function looksLikeHtml(payload: string): boolean {
  const normalized = payload.trim().toLowerCase();
  return normalized.startsWith('<!doctype html') || normalized.startsWith('<html');
}

function buildRoutingHint(url: string): string {
  return (
    `Expected JSON from ${url}, but received HTML. ` +
    'This usually means API routing is misconfigured. ' +
    'Check VITE_API_HOST and ensure backend routes are mounted under /api.'
  );
}

// Best-effort body read for error diagnostics. Intentionally swallows failures —
// the response body is context for a better error message, not load-bearing.
async function readText(response: Response): Promise<string> {
  try {
    return await response.text();
  } catch {
    return '';
  }
}

async function throwHttpError(response: Response, url: string): Promise<never> {
  const body = await readText(response);

  if (body && looksLikeHtml(body)) {
    throw new Error(buildRoutingHint(url));
  }

  const detail = extractDetail(body);
  if (detail) {
    throw new Error(detail);
  }
  const fallback = body.trim() || `${response.status} ${response.statusText}`;
  throw new Error(`Request failed (${response.status}) for ${url}: ${fallback}`);
}

// FastAPI returns `{"detail": "..."}` for HTTPException; unwrap so users see
// the message directly instead of escaped JSON.
function extractDetail(body: string): string | null {
  const trimmed = body.trim();
  if (!trimmed || (trimmed[0] !== '{' && trimmed[0] !== '[')) return null;
  try {
    const parsed = JSON.parse(trimmed);
    if (parsed && typeof parsed.detail === 'string' && parsed.detail.trim()) {
      return parsed.detail.trim();
    }
  } catch {
    // not JSON — fall through to raw body fallback in caller
  }
  return null;
}

async function parseJson<T>(response: Response, url: string): Promise<T> {
  const contentType = (response.headers.get('content-type') || '').toLowerCase();

  if (!contentType.includes(JSON_CONTENT_TYPE)) {
    const body = await readText(response);
    if (looksLikeHtml(body)) {
      throw new Error(buildRoutingHint(url));
    }

    throw new Error(
      `Expected JSON from ${url}, but received content-type '${contentType || 'unknown'}'.`
    );
  }

  try {
    return (await response.json()) as T;
  } catch {
    throw new Error(`Invalid JSON returned from ${url}. Check API routing and response format.`);
  }
}

// ── Request pipeline ─────────────────────────────────────────────────────────
// request()     — raw fetch wrapper: builds URL, sets method/body/headers.
// requestJson() — request() + status check + JSON parse. Most public methods
//                 use this; the few that need custom status handling (e.g.
//                 getNextQuestion) drop to request() directly.

async function request(
  path: string,
  options: RequestOptions = {}
): Promise<{ url: string; response: Response }> {
  const { method = 'GET', query, json, formData, headers } = options;

  // Runtime guard: TypeScript can't enforce mutual exclusion on two optional
  // fields, so we catch it here.
  if (json !== undefined && formData !== undefined) {
    throw new Error('Invalid request options: provide either json or formData, not both.');
  }

  const init: RequestInit = { method, credentials: 'include' };

  if (formData !== undefined) {
    init.body = formData;
  } else if (json !== undefined) {
    init.headers = { ...(headers || {}), 'Content-Type': JSON_CONTENT_TYPE };
    init.body = JSON.stringify(json);
  } else if (headers) {
    init.headers = headers;
  }

  const url = buildUrl(path, query);
  const response = await fetch(url, init);
  return { url, response };
}

async function requestJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { url, response } = await request(path, options);

  if (!response.ok) {
    await throwHttpError(response, url);
  }

  return parseJson<T>(response, url);
}

// ── Public API ───────────────────────────────────────────────────────────────

export const api = {
  // ── Admin ────────────────────────────────────────────────────────────────

  async adminLogin(token: string): Promise<{ ok: boolean } | MessageResponse> {
    return requestJson<{ ok: boolean } | MessageResponse>(routes.admin.authLogin, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    });
  },

  async adminLogout(): Promise<{ ok: boolean } | MessageResponse> {
    return requestJson<{ ok: boolean } | MessageResponse>(routes.admin.authLogout, {
      method: 'POST',
    });
  },

  async createExperiment(data: ExperimentCreate): Promise<Experiment> {
    return requestJson<Experiment>(routes.admin.experiments, {
      method: 'POST',
      json: data,
    });
  },

  async listExperiments(): Promise<Experiment[]> {
    return requestJson<Experiment[]>(routes.admin.experiments);
  },

  async uploadQuestions(experimentId: number, file: File): Promise<MessageResponse> {
    const formData = new FormData();
    formData.append('file', file);

    return requestJson<MessageResponse>(routes.admin.upload(experimentId), {
      method: 'POST',
      formData,
    });
  },

  async getExperimentStats(
    experimentId: number,
    { includePreview = false }: { includePreview?: boolean } = {}
  ): Promise<ExperimentStats> {
    return requestJson<ExperimentStats>(routes.admin.stats(experimentId), {
      query: { ...(includePreview ? { include_preview: 'true' } : {}) },
    });
  },

  async getExperimentAnalytics(
    experimentId: number,
    { includePreview = false }: { includePreview?: boolean } = {}
  ): Promise<Analytics> {
    return requestJson<Analytics>(routes.admin.analytics(experimentId), {
      query: { ...(includePreview ? { include_preview: 'true' } : {}) },
    });
  },

  async listUploads(experimentId: number): Promise<Upload[]> {
    return requestJson<Upload[]>(routes.admin.uploads(experimentId));
  },

  async updateExperiment(experimentId: number, data: { assistance_method: string; assistance_params?: Record<string, unknown> | null }): Promise<Experiment> {
    return requestJson<Experiment>(routes.admin.experiment(experimentId), {
      method: 'PATCH',
      json: data,
    });
  },

  async deleteExperiment(experimentId: number): Promise<MessageResponse> {
    return requestJson<MessageResponse>(routes.admin.experiment(experimentId), {
      method: 'DELETE',
    });
  },

  async getPlatformStatus(): Promise<PlatformStatus> {
    return requestJson<PlatformStatus>(routes.admin.platformStatus);
  },

  async runPilotStudy(experimentId: number, data: PilotStudyCreate): Promise<ExperimentRound> {
    return requestJson<ExperimentRound>(routes.admin.prolificPilot(experimentId), {
      method: 'POST',
      json: data,
    });
  },

  async getRecommendation(
    experimentId: number,
    { includePreview = false }: { includePreview?: boolean } = {}
  ): Promise<RecommendationResponse> {
    return requestJson<RecommendationResponse>(routes.admin.prolificRecommend(experimentId), {
      query: { ...(includePreview ? { include_preview: 'true' } : {}) },
    });
  },

  async runExperimentRound(experimentId: number, places: number): Promise<ExperimentRound> {
    return requestJson<ExperimentRound>(routes.admin.prolificRounds(experimentId), {
      method: 'POST',
      json: { places },
    });
  },

  async listExperimentRounds(experimentId: number): Promise<ExperimentRound[]> {
    return requestJson<ExperimentRound[]>(routes.admin.prolificRounds(experimentId));
  },

  async editExperimentRound(
    experimentId: number,
    roundId: number,
    fields: ExperimentRoundUpdate,
  ): Promise<ExperimentRound> {
    return requestJson<ExperimentRound>(routes.admin.prolificRound(experimentId, roundId), {
      method: 'PATCH',
      json: fields,
    });
  },

  async publishExperimentRound(experimentId: number, roundId: number): Promise<MessageResponse> {
    return requestJson<MessageResponse>(routes.admin.prolificRoundPublish(experimentId, roundId), {
      method: 'POST',
    });
  },

  async closeExperimentRound(experimentId: number, roundId: number): Promise<MessageResponse> {
    return requestJson<MessageResponse>(routes.admin.prolificRoundClose(experimentId, roundId), {
      method: 'POST',
    });
  },

  // Returns a URL string for direct browser download (not a fetch).
  getExportUrl(
    experimentId: number,
    { includePreview = false }: { includePreview?: boolean } = {}
  ): string {
    return buildUrl(routes.admin.export(experimentId), {
      ...(includePreview ? { include_preview: 'true' } : {}),
    });
  },

  // ── Rater ────────────────────────────────────────────────────────────────

  // Query params follow Prolific platform conventions: PROLIFIC_PID, STUDY_ID,
  // SESSION_ID are passed through from the study URL Prolific redirects to.
  async startSession(
    experimentId: string,
    prolificId: string,
    studyId: string | null,
    sessionId: string | null,
    preview: boolean = false
  ): Promise<Session> {
    return requestJson<Session>(routes.rater.start, {
      method: 'POST',
      query: {
        experiment_id: experimentId,
        PROLIFIC_PID: prolificId,
        STUDY_ID: studyId,
        SESSION_ID: sessionId,
        ...(preview ? { preview: 'true' } : {}),
      },
    });
  },

  // Drops to request() instead of requestJson() because the backend returns
  // 403 for expired sessions — we need to check status before JSON parsing.
  async getNextQuestion(sessionToken: string): Promise<Question | null> {
    const { url, response } = await request(routes.rater.nextQuestion, {
      headers: { 'X-Rater-Session': sessionToken },
    });

    if (response.status === 403) {
      throw new Error('Session expired');
    }

    if (!response.ok) {
      await throwHttpError(response, url);
    }

    return parseJson<Question | null>(response, url);
  },

  async submitRating(sessionToken: string, data: RatingSubmit): Promise<SubmitRatingResponse> {
    return requestJson<SubmitRatingResponse>(routes.rater.submit, {
      method: 'POST',
      headers: { 'X-Rater-Session': sessionToken },
      json: data,
    });
  },

  async getSessionStatus(sessionToken: string): Promise<SessionStatusResponse> {
    return requestJson<SessionStatusResponse>(routes.rater.sessionStatus, {
      headers: { 'X-Rater-Session': sessionToken },
    });
  },

  async endSession(sessionToken: string): Promise<MessageResponse> {
    return requestJson<MessageResponse>(routes.rater.endSession, {
      method: 'POST',
      headers: { 'X-Rater-Session': sessionToken },
    });
  },

  async startAssistance(sessionToken: string, questionId: number): Promise<AssistanceStep> {
    return requestJson<AssistanceStep>(routes.rater.assistanceStart, {
      method: 'POST',
      headers: { 'X-Rater-Session': sessionToken },
      json: { question_id: questionId },
    });
  },

  async advanceAssistance(
    sessionToken: string,
    sessionId: number,
    answers: Record<number, { answer: string; confidence: number }>
  ): Promise<AssistanceStep> {
    return requestJson<AssistanceStep>(routes.rater.assistanceAdvance, {
      method: 'POST',
      headers: { 'X-Rater-Session': sessionToken },
      json: { session_id: sessionId, human_input: JSON.stringify(answers) },
    });
  },
};
