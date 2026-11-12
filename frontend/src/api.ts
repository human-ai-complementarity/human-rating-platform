// API client for the human rating platform backend.
//
// All routes are relative to /api — the base URL is resolved once at startup
// from VITE_API_HOST. Empty = same-origin /api (local dev via Vite proxy).
// Non-empty = cross-origin {origin}/api (e.g. Render deployment).

import type {
  Analytics,
  ApiKey,
  ApiKeyCreated,
  ApiKeyScope,
  AssistanceStep,
  ConsentResponse,
  StudyFlag,
  ExperimentRound,
  TermsPreview,
  TermsStatus,
  ExperimentRoundUpdate,
  Dataset,
  DatasetCardFields,
  Experiment,
  ExperimentCreate,
  ExperimentDuplicate,
  ExperimentGroup,
  Tag,
  ExperimentStats,
  ExperimentStatus,
  LaunchPreview,
  PilotCheckRefusal,
  PilotCheckRun,
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
  // Seconds until even the grace window closes. While the session is live this
  // is time_remaining_seconds plus the grace period; afterwards it counts down
  // alone, and the question already on screen may still be submitted.
  grace_seconds_remaining: number;
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
  signal?: AbortSignal;
};

// ── Constants ────────────────────────────────────────────────────────────────

const API_PREFIX = '/api';
const JSON_CONTENT_TYPE = 'application/json';
// The largest page the admin experiments list will serve (`limit` max).
const EXPERIMENT_PAGE_SIZE = 1000;

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
    exportDocuments: (id: number) => `/admin/experiments/${id}/export/documents`,
    authLogin: '/admin/auth/login',
    authLogout: '/admin/auth/logout',
    platformStatus: '/admin/platform-status',
    datasets: '/admin/datasets',
    experimentGroups: '/admin/experiment-groups',
    tags: '/admin/tags',
    apiKeys: '/admin/api-keys',
    apiKeyRegenerate: (id: number) => `/admin/api-keys/${id}/regenerate`,
    apiKeyRevoke: (id: number) => `/admin/api-keys/${id}/revoke`,
    launch: (id: number) => `/admin/experiments/${id}/launch`,
    pilotCheck: (id: number) => `/admin/experiments/${id}/pilot-check`,
    launchPreview: (id: number) => `/admin/experiments/${id}/launch/preview`,
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
    terms: '/admin/terms',
    experimentTermsPreview: (experimentId: number) =>
      `/admin/experiments/${experimentId}/terms/preview`,
  },
  rater: {
    start: '/raters/start',
    consent: '/raters/consent',
    nextQuestion: '/raters/next-question',
    question: (questionId: number) => `/raters/questions/${questionId}`,
    submit: '/raters/submit',
    sessionStatus: '/raters/session-status',
    endSession: '/raters/end-session',
    assistanceStart: '/raters/assistance/start',
    queue: '/raters/queue',
    assistanceObservation: '/raters/assistance/observation',
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

// A question image's URL as the browser should fetch it. The backend hands out
// either a signed storage URL (absolute, used as is) or one of its own paths
// under /api, which has to go to the API host like every other request. The
// result is always absolute, so it also works from a blob: document (the
// "open document in new tab" popup), where relative URLs do not resolve.
export function resolveMediaUrl(url: string): string {
  if (!url.startsWith(`${API_PREFIX}/`)) {
    return url;
  }
  const rebased = `${API_BASE}${url.slice(API_PREFIX.length)}`;
  return new URL(rebased, window.location.origin).href;
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

function httpErrorMessage(
  status: number,
  statusText: string,
  body: string,
  detail: unknown,
  url: string
): string {
  if (body && looksLikeHtml(body)) {
    return buildRoutingHint(url);
  }

  const message = describeDetail(detail);
  if (message) {
    return message;
  }
  const fallback = body.trim() || `${status} ${statusText}`;
  return `Request failed (${status}) for ${url}: ${fallback}`;
}

/**
 * A non-2xx response. `message` is what the UI shows; `status` and `detail`
 * let a caller recognise a structured refusal, such as the pilot check's 409
 * (docs/pilot-check.md), and act on it instead of only displaying it.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

// FastAPI's `detail`, when the body is JSON that carries one.
function parseDetail(body: string): unknown {
  const trimmed = body.trim();
  if (!trimmed || (trimmed[0] !== '{' && trimmed[0] !== '[')) return undefined;
  try {
    return (JSON.parse(trimmed) as { detail?: unknown }).detail;
  } catch {
    return undefined;
  }
}

/** The launch gate's refusal, when `err` is one. */
export function pilotCheckRefusal(err: unknown): PilotCheckRefusal | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const detail = err.detail as Partial<PilotCheckRefusal> | undefined;
  if (!detail || typeof detail !== 'object' || !detail.pilot_check) return null;
  if (detail.code !== 'pilot_check') return null;
  return detail as PilotCheckRefusal;
}

async function throwHttpError(response: Response, url: string): Promise<never> {
  const body = await readText(response);
  const detail = parseDetail(body);
  throw new ApiError(
    response.status,
    httpErrorMessage(response.status, response.statusText, body, detail, url),
    detail
  );
}

// FastAPI's `detail` comes in three shapes: a string from HTTPException, a
// list of `{loc, msg, ...}` from request validation (Pydantic), and an object
// with a `message` for a structured refusal such as the pilot check's 409.
// Unwrap each so users see the sentence instead of escaped JSON.
function describeDetail(detail: unknown): string | null {
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) return formatValidationErrors(detail);
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === 'string' && message.trim()) return message.trim();
  }
  return null;
}

// One "<field>: <msg>" per error, joined. `input` is deliberately never shown:
// it echoes the rejected value, and for a missing field it is the whole
// request body.
function formatValidationErrors(errors: unknown[]): string | null {
  const lines = errors.flatMap((error) => {
    if (!error || typeof error !== 'object') return [];
    const { loc, msg } = error as { loc?: unknown; msg?: unknown };
    if (typeof msg !== 'string' || !msg.trim()) return [];
    const field = Array.isArray(loc) ? formatErrorLocation(loc) : '';
    return [field ? `${field}: ${msg.trim()}` : msg.trim()];
  });
  return lines.length > 0 ? lines.join('; ') : null;
}

// ["body", "tags", 0] -> "tags[0]". The leading segment names where the value
// came from (body, query, path, ...), not a field, so it is dropped.
const ERROR_LOCATION_SOURCES = new Set(['body', 'query', 'path', 'header', 'cookie']);
function formatErrorLocation(loc: unknown[]): string {
  const parts = typeof loc[0] === 'string' && ERROR_LOCATION_SOURCES.has(loc[0]) ? loc.slice(1) : loc;
  return parts.reduce<string>((path, part) => {
    if (typeof part === 'number') return `${path}[${part}]`;
    return path ? `${path}.${String(part)}` : String(part);
  }, '');
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

  const init: RequestInit = { method, credentials: 'include', signal: options.signal };

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
        reject(
          new Error(httpErrorMessage(xhr.status, xhr.statusText, body, parseDetail(body), url))
        );
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
    // Callers search and total spend over the full set, and the server pages
    // (100 newest by default), so walk every page instead of taking the first.
    const all: Experiment[] = [];
    for (;;) {
      const page = await requestJson<Experiment[]>(routes.admin.experiments, {
        query: {
          skip: all.length,
          limit: EXPERIMENT_PAGE_SIZE,
          ...(includeArchived ? { include_archived: 'true' } : archived ? { archived: 'true' } : {}),
          ...(status ? { status } : {}),
          ...(search ? { search } : {}),
        },
      });
      all.push(...page);
      if (page.length < EXPERIMENT_PAGE_SIZE) return all;
    }
  },

  // Single-experiment fetch. Resolves by id regardless of archived state, so
  // the detail page can open an archived experiment (the list hides those).
  async listDatasets(): Promise<Dataset[]> {
    return requestJson<Dataset[]>(routes.admin.datasets);
  },

  async createDataset(
    data: { name: string; waves?: string[] } & Partial<DatasetCardFields>,
  ): Promise<Dataset> {
    return requestJson<Dataset>(routes.admin.datasets, {
      method: 'POST',
      json: data,
    });
  },

  async getDataset(datasetId: number): Promise<Dataset> {
    return requestJson<Dataset>(`${routes.admin.datasets}/${datasetId}`);
  },

  // Partial: omitted keys are left alone, an explicit null clears the field.
  // `waves` replaces the set; the backend rejects dropping one a group uses.
  async updateDatasetCard(
    datasetId: number,
    data: Partial<DatasetCardFields> & { name?: string; waves?: string[] },
  ): Promise<Dataset> {
    return requestJson<Dataset>(`${routes.admin.datasets}/${datasetId}`, {
      method: 'PATCH',
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

  async listTags(): Promise<Tag[]> {
    return requestJson<Tag[]>(routes.admin.tags);
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
      // Rater terms. Undefined means "leave unchanged"; locked after publish.
      study_flags?: StudyFlag[];
      content_warning_details?: string;
      is_markdown?: boolean;
      render_math?: boolean;
      prolific_pool?: string;
      // Minutes per rater. Undefined means "leave unchanged"; locked once the
      // experiment leaves DRAFT.
      session_duration_minutes?: number;
      // Undefined means "leave unchanged"; null ungroups. Locked once the
      // experiment leaves DRAFT.
      group_id?: number | null;
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

  async duplicateExperiment(experimentId: number): Promise<ExperimentDuplicate> {
    return requestJson<ExperimentDuplicate>(routes.admin.duplicateExperiment(experimentId), {
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

  // ── API keys (bearer credentials for /api/v1, and /api/admin when admin) ───

  async listApiKeys(): Promise<ApiKey[]> {
    return requestJson<ApiKey[]>(routes.admin.apiKeys);
  },

  // Returns the full plaintext key — surface it to the user once, then discard.
  async createApiKey(name: string, scope: ApiKeyScope = 'read'): Promise<ApiKeyCreated> {
    return requestJson<ApiKeyCreated>(routes.admin.apiKeys, {
      method: 'POST',
      json: { name, scope },
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

  // One-click: builds the pilot draft from the dataset card instead of the
  // form. Only a complete card (economics included) qualifies, so a dataset's
  // first study usually goes through the pilot form. Publishing is still separate.
  // It excludes the dataset's other experiments; getLaunchPreview lists them.
  async launchFromCard(
    experimentId: number,
    places?: number,
    { acknowledgePilotCheck = false }: { acknowledgePilotCheck?: boolean } = {}
  ): Promise<ExperimentRound> {
    return requestJson<ExperimentRound>(routes.admin.launch(experimentId), {
      method: 'POST',
      json: {
        ...(places != null ? { places } : {}),
        ...(acknowledgePilotCheck ? { acknowledge_pilot_check: true } : {}),
      },
    });
  },

  // ── Pilot check (docs/pilot-check.md) ─────────────────────────────────

  // Starts a run; it finishes in the background. Poll getPilotCheck.
  async runPilotCheck(experimentId: number): Promise<PilotCheckRun> {
    return requestJson<PilotCheckRun>(routes.admin.pilotCheck(experimentId), { method: 'POST' });
  },

  // The experiment's latest run, or null when none has been started.
  async getPilotCheck(experimentId: number): Promise<PilotCheckRun | null> {
    return requestJson<PilotCheckRun | null>(routes.admin.pilotCheck(experimentId));
  },

  async getLaunchPreview(experimentId: number): Promise<LaunchPreview> {
    return requestJson<LaunchPreview>(routes.admin.launchPreview(experimentId));
  },

  async getRecommendation(
    experimentId: number,
    { includePreview = false }: { includePreview?: boolean } = {}
  ): Promise<RecommendationResponse> {
    return requestJson<RecommendationResponse>(routes.admin.prolificRecommend(experimentId), {
      query: { ...(includePreview ? { include_preview: 'true' } : {}) },
    });
  },

  async runExperimentRound(
    experimentId: number,
    places: number,
    { acknowledgePilotCheck = false }: { acknowledgePilotCheck?: boolean } = {}
  ): Promise<ExperimentRound> {
    return requestJson<ExperimentRound>(routes.admin.prolificRounds(experimentId), {
      method: 'POST',
      json: { places, ...(acknowledgePilotCheck ? { acknowledge_pilot_check: true } : {}) },
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

  getDocumentsExportUrl(experimentId: number): string {
    return buildUrl(routes.admin.exportDocuments(experimentId));
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

  // Deep-link target for the analytics question table: loads one specific
  // question instead of whatever the selector would serve next. Preview
  // sessions only — and that rejection is also a 403, so unlike
  // getNextQuestion we don't blanket-map 403 to "Session expired". requestJson
  // surfaces FastAPI's `detail` verbatim, which keeps a real expiry matching
  // the caller's check while the preview-only rejection reads as itself.
  async getQuestion(sessionToken: string, questionId: number): Promise<Question> {
    return requestJson<Question>(routes.rater.question(questionId), {
      headers: { 'X-Rater-Session': sessionToken },
    });
  },

  async submitRating(sessionToken: string, data: RatingSubmit): Promise<SubmitRatingResponse> {
    return requestJson<SubmitRatingResponse>(routes.rater.submit, {
      method: 'POST',
      headers: { 'X-Rater-Session': sessionToken },
      json: data,
    });
  },

  async getTermsStatus(): Promise<TermsStatus> {
    return requestJson<TermsStatus>(routes.admin.terms);
  },

  // `selection` previews settings as chosen in the form, saved or not; the
  // backend ignores it once the experiment has pinned its versions.
  async getTermsPreview(
    experimentId: number,
    selection?: { study_flags: StudyFlag[]; content_warning_details: string },
  ): Promise<TermsPreview> {
    const params = new URLSearchParams();
    if (selection) {
      // The sentinel lets an empty selection (no flags) differ from "use
      // what is saved", which repeated query fields alone cannot express.
      params.set('selection', 'true');
      for (const flag of selection.study_flags) params.append('study_flags', flag);
      params.set('content_warning_details', selection.content_warning_details);
    }
    const query = selection ? `?${params.toString()}` : '';
    return requestJson<TermsPreview>(`${routes.admin.experimentTermsPreview(experimentId)}${query}`);
  },

  async recordConsent(sessionToken: string): Promise<ConsentResponse> {
    return requestJson<ConsentResponse>(routes.rater.consent, {
      method: 'POST',
      headers: { 'X-Rater-Session': sessionToken },
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

  async skipQuestion(sessionToken: string, assignmentId: number, generation: number): Promise<void> {
    await requestJson(routes.rater.queue, {
      method: 'POST', headers: { 'X-Rater-Session': sessionToken },
      json: { action: 'skip', assignment_id: assignmentId, generation },
    });
  },

  async observeAssistance(sessionToken: string, sessionId: number, waitMs: number): Promise<void> {
    await requestJson(routes.rater.assistanceObservation, {
      method: 'POST', headers: { 'X-Rater-Session': sessionToken },
      json: { session_id: sessionId, wait_ms: Math.min(600_000, Math.max(0, waitMs)) },
    });
  },

  async startAssistance(sessionToken: string, questionId: number, signal?: AbortSignal): Promise<AssistanceStep> {
    return requestJson<AssistanceStep>(routes.rater.assistanceStart, {
      method: 'POST',
      signal,
      headers: { 'X-Rater-Session': sessionToken },
      json: { question_id: questionId },
    });
  },

  async advanceAssistance(
    sessionToken: string,
    step: Pick<AssistanceStep, 'session_id' | 'turn'>,
    answers: Record<number, { answer: string; confidence: number }>
  ): Promise<AssistanceStep> {
    return requestJson<AssistanceStep>(routes.rater.assistanceAdvance, {
      method: 'POST',
      headers: { 'X-Rater-Session': sessionToken },
      json: { session_id: step.session_id, human_input: JSON.stringify(answers), turn: step.turn },
    });
  },
};
