// API client for the human rating platform backend.
//
// All routes are relative to /api — the base URL is resolved once at startup
// from VITE_API_HOST. Empty = same-origin /api (local dev via Vite proxy).
// Non-empty = cross-origin {origin}/api (e.g. Render deployment).

import type {
  Analytics,
  ApiKey,
  ApiKeyCreated,
  AssistanceStep,
  ExperimentRound,
  ExperimentRoundUpdate,
  Dataset,
  Experiment,
  ExperimentCreate,
  ExperimentGroup,
  ExperimentStats,
  ExperimentStatus,
  PilotStudyCreate,
  PlatformStatus,
  Question,
  RatingSubmit,
  RecommendationResponse,
  Session,
  Upload,
  UploadResponse,
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
    finishExperiment: (id: number) => `/admin/experiments/${id}/finish`,
    duplicateExperiment: (id: number) => `/admin/experiments/${id}/duplicate`,
    archiveExperiment: (id: number) => `/admin/experiments/${id}/archive`,
    unarchiveExperiment: (id: number) => `/admin/experiments/${id}/unarchive`,
    upload: (id: number) => `/admin/experiments/${id}/upload`,
    uploads: (id: number) => `/admin/experiments/${id}/uploads`,
    stats: (id: number) => `/admin/experiments/${id}/stats`,
    analytics: (id: number) => `/admin/experiments/${id}/analytics`,
    export: (id: number) => `/admin/experiments/${id}/export`,
    authLogin: '/admin/auth/login',
    authLogout: '/admin/auth/logout',
    platformStatus: '/admin/platform-status',
    datasets: '/admin/datasets',
    experimentGroups: '/admin/experiment-groups',
    apiKeys: '/admin/api-keys',
    apiKeyRegenerate: (id: number) => `/admin/api-keys/${id}/regenerate`,
    apiKeyRevoke: (id: number) => `/admin/api-keys/${id}/revoke`,
    prolificPilot: (id: number) => `/admin/experiments/${id}/prolific/pilot`,
    prolificRecommend: (id: number) => `/admin/experiments/${id}/prolific/recommend`,
    prolificRounds: (id: number) => `/admin/experiments/${id}/prolific/rounds`,
    prolificSyncSpend: (id: number) => `/admin/experiments/${id}/prolific/sync-spend`,
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

function httpErrorMessage(status: number, statusText: string, body: string, url: string): string {
  if (body && looksLikeHtml(body)) {
    return buildRoutingHint(url);
  }

  const detail = extractDetail(body);
  if (detail) {
    return detail;
  }
  const fallback = body.trim() || `${status} ${statusText}`;
  return `Request failed (${status}) for ${url}: ${fallback}`;
}

async function throwHttpError(response: Response, url: string): Promise<never> {
  const body = await readText(response);
  throw new Error(httpErrorMessage(response.status, response.statusText, body, url));
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

function parseJsonBody<T>(body: string, contentType: string, url: string): T {
  if (!contentType.includes(JSON_CONTENT_TYPE)) {
    if (looksLikeHtml(body)) {
      throw new Error(buildRoutingHint(url));
    }

    throw new Error(
      `Expected JSON from ${url}, but received content-type '${contentType || 'unknown'}'.`
    );
  }

  try {
    return JSON.parse(body) as T;
  } catch {
    throw new Error(`Invalid JSON returned from ${url}. Check API routing and response format.`);
  }
}

async function parseJson<T>(response: Response, url: string): Promise<T> {
  const contentType = (response.headers.get('content-type') || '').toLowerCase();
  return parseJsonBody<T>(await readText(response), contentType, url);
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

// ── Uploads with progress ────────────────────────────────────────────────────
// fetch() cannot report request-body progress, so file uploads drop to
// XMLHttpRequest, whose `upload.progress` event gives byte-level feedback. The
// status/parse handling below mirrors requestJson() so callers see identical
// error messages either way.

export type UploadProgress = {
  /** Request-body bytes sent so far. */
  loaded: number;
  /** Total bytes to send; null when the browser can't compute it. */
  total: number | null;
};

/** Thrown when the caller aborts an upload — distinct from a real failure. */
export class UploadAbortedError extends Error {
  constructor() {
    super('Upload canceled');
    this.name = 'UploadAbortedError';
  }
}

type UploadOptions = {
  onProgress?: (progress: UploadProgress) => void;
  signal?: AbortSignal;
};

function uploadFormData<T>(
  path: string,
  formData: FormData,
  { onProgress, signal }: UploadOptions = {}
): Promise<T> {
  const url = buildUrl(path);

  return new Promise<T>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new UploadAbortedError());
      return;
    }

    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.withCredentials = true;

    const abort = () => xhr.abort();
    signal?.addEventListener('abort', abort);
    const cleanup = () => signal?.removeEventListener('abort', abort);

    if (onProgress) {
      const report = (event: ProgressEvent) => {
        onProgress({
          loaded: event.loaded,
          total: event.lengthComputable ? event.total : null,
        });
      };
      xhr.upload.addEventListener('progress', report);
      // `upload.load` fires once the whole body is on the wire, with
      // loaded === total. Listening for it explicitly means the caller learns
      // the transfer finished even if the last `progress` event never lands.
      xhr.upload.addEventListener('load', report);
    }

    xhr.addEventListener('abort', () => {
      cleanup();
      reject(new UploadAbortedError());
    });

    xhr.addEventListener('error', () => {
      cleanup();
      reject(new Error(`Network error while uploading to ${url}. Check your connection.`));
    });

    xhr.addEventListener('load', () => {
      cleanup();
      const body = xhr.responseText || '';

      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new Error(httpErrorMessage(xhr.status, xhr.statusText, body, url)));
        return;
      }

      const contentType = (xhr.getResponseHeader('content-type') || '').toLowerCase();
      try {
        resolve(parseJsonBody<T>(body, contentType, url));
      } catch (err) {
        reject(err instanceof Error ? err : new Error(String(err)));
      }
    });

    xhr.send(formData);
  });
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

  async listExperiments({
    archived = false,
    includeArchived = false,
    status,
    search,
  }: {
    archived?: boolean;
    // Return both active and archived rows in one call, for client-side
    // filtering. Overrides `archived` when set.
    includeArchived?: boolean;
    status?: ExperimentStatus;
    search?: string;
  } = {}): Promise<Experiment[]> {
    return requestJson<Experiment[]>(routes.admin.experiments, {
      query: {
        ...(includeArchived ? { include_archived: 'true' } : archived ? { archived: 'true' } : {}),
        ...(status ? { status } : {}),
        ...(search ? { search } : {}),
      },
    });
  },

  // Single-experiment fetch. Resolves by id regardless of archived state, so
  // the detail page can open an archived experiment (the list hides those).
  async listDatasets(): Promise<Dataset[]> {
    return requestJson<Dataset[]>(routes.admin.datasets);
  },

  async createDataset(data: { name: string; waves?: string[] }): Promise<Dataset> {
    return requestJson<Dataset>(routes.admin.datasets, {
      method: 'POST',
      json: data,
    });
  },

  async listExperimentGroups(query: { dataset_id?: number; wave?: string } = {}): Promise<ExperimentGroup[]> {
    return requestJson<ExperimentGroup[]>(routes.admin.experimentGroups, {
      query: {
        ...(query.dataset_id != null ? { dataset_id: query.dataset_id } : {}),
        ...(query.wave ? { wave: query.wave } : {}),
      },
    });
  },

  async createExperimentGroup(data: {
    name: string;
    dataset_id: number;
    wave?: string | null;
  }): Promise<ExperimentGroup> {
    return requestJson<ExperimentGroup>(routes.admin.experimentGroups, {
      method: 'POST',
      json: data,
    });
  },

  async getExperiment(experimentId: number): Promise<Experiment> {
    return requestJson<Experiment>(routes.admin.experiment(experimentId));
  },

  // Reports transfer progress via `options.onProgress`. Note that progress
  // reaching 100% means the bytes are sent, not that the upload is done — the
  // server still has to parse the file and insert rows, which for a large
  // dataset is the longer half of the wait.
  async uploadQuestions(
    experimentId: number,
    file: File,
    options: UploadOptions = {}
  ): Promise<UploadResponse> {
    const formData = new FormData();
    formData.append('file', file);

    return uploadFormData<UploadResponse>(routes.admin.upload(experimentId), formData, options);
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

  async updateExperiment(
    experimentId: number,
    data: {
      assistance_method: string;
      assistance_params?: Record<string, unknown> | null;
      // Public rater-facing name. Undefined means "leave unchanged"; the backend
      // rejects an empty value (the public name is required).
      name?: string;
      // Private researcher-facing label. Undefined means "leave unchanged"; ""
      // clears it and the title falls back to the public name.
      internal_name?: string;
      // Dataset metadata edits. Each field uses null/undefined to mean
      // "leave unchanged" and "" to clear. Mirrors ExperimentUpdate on the backend.
      description?: string;
      system_prompt?: string;
      human_prompt_prefix?: string;
      human_prompt_suffix?: string;
      prolific_pool?: string;
    },
  ): Promise<Experiment> {
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

  async finishExperiment(experimentId: number): Promise<Experiment> {
    return requestJson<Experiment>(routes.admin.finishExperiment(experimentId), {
      method: 'POST',
    });
  },

  async duplicateExperiment(experimentId: number): Promise<Experiment> {
    return requestJson<Experiment>(routes.admin.duplicateExperiment(experimentId), {
      method: 'POST',
    });
  },

  async archiveExperiment(experimentId: number): Promise<Experiment> {
    return requestJson<Experiment>(routes.admin.archiveExperiment(experimentId), {
      method: 'POST',
    });
  },

  async unarchiveExperiment(experimentId: number): Promise<Experiment> {
    return requestJson<Experiment>(routes.admin.unarchiveExperiment(experimentId), {
      method: 'POST',
    });
  },

  async getPlatformStatus(): Promise<PlatformStatus> {
    return requestJson<PlatformStatus>(routes.admin.platformStatus);
  },

  // ── API keys (bearer credentials for the /api/v1 programmatic API) ─────────

  async listApiKeys(): Promise<ApiKey[]> {
    return requestJson<ApiKey[]>(routes.admin.apiKeys);
  },

  // Returns the full plaintext key — surface it to the user once, then discard.
  async createApiKey(name: string): Promise<ApiKeyCreated> {
    return requestJson<ApiKeyCreated>(routes.admin.apiKeys, {
      method: 'POST',
      json: { name },
    });
  },

  // Rotates the secret under the same key id/name; returns the new plaintext.
  async regenerateApiKey(id: number): Promise<ApiKeyCreated> {
    return requestJson<ApiKeyCreated>(routes.admin.apiKeyRegenerate(id), {
      method: 'POST',
    });
  },

  async revokeApiKey(id: number): Promise<ApiKey> {
    return requestJson<ApiKey>(routes.admin.apiKeyRevoke(id), {
      method: 'POST',
    });
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

  // Refreshes each round's Prolific cost and returns the experiment's total
  // spend (minor units). Fired from the detail view to hydrate the spend card.
  async syncExperimentSpend(experimentId: number): Promise<{ spend_minor_units: number }> {
    return requestJson<{ spend_minor_units: number }>(
      routes.admin.prolificSyncSpend(experimentId),
      { method: 'POST' },
    );
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

  async discardExperimentRound(experimentId: number, roundId: number): Promise<MessageResponse> {
    return requestJson<MessageResponse>(routes.admin.prolificRound(experimentId, roundId), {
      method: 'DELETE',
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
