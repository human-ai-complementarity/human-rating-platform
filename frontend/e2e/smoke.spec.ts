import { expect, test, type Page, type Route } from '@playwright/test';
import { fileURLToPath } from 'node:url';

type ExperimentRecord = {
  id: number;
  name: string;
  internal_name: string | null;
  created_at: string;
  num_ratings_per_question: number;
  prolific_completion_url: string | null;
  question_count: number;
  rating_count: number;
  status: 'DRAFT' | 'LAUNCH' | 'FINISHED';
  archived_at: string | null;
};

type UploadRecord = {
  id: number;
  filename: string;
  uploaded_at: string;
  question_count: number;
  dataset_meta: Record<string, unknown> | null;
};

type ExperimentRoundRecord = {
  id: number;
  round_number: number;
  prolific_study_id: string;
  prolific_study_status: 'UNPUBLISHED' | 'ACTIVE' | 'AWAITING_REVIEW' | 'COMPLETED';
  places_requested: number;
  description: string;
  estimated_completion_time: number;
  reward: number;
  device_compatibility: string[];
  created_at: string;
  prolific_study_url: string;
};

type RecommendationRecord = {
  avg_time_per_question_seconds: number;
  remaining_rating_actions: number;
  total_hours_remaining: number;
  recommended_places: number;
  is_complete: boolean;
};

type RaterSessionRecord = {
  rater_id: number;
  session_start: string;
  session_end_time: string;
  experiment_name: string;
  completion_url: string | null;
  rater_session_token: string;
};

type RaterAnalyticsRecord = {
  prolific_id: string;
  study_id: string | null;
  session_start: string | null;
  session_end: string | null;
  is_active: boolean;
  num_ratings: number;
  total_response_time_seconds: number;
  avg_response_time_seconds: number;
  avg_confidence: number;
};

type AnalyticsRecord = {
  experiment_name: string;
  overview: {
    total_ratings: number;
    total_questions: number;
    total_raters: number;
    avg_response_time_seconds: number;
    avg_confidence: number;
  };
  questions: unknown[];
  raters: RaterAnalyticsRecord[];
};

type RaterQuestionRecord = {
  id: number;
  question_text: string;
  options: string | null;
  question_type: string;
  parent_question_text?: string | null;
};

type MockState = {
  experiments: ExperimentRecord[];
  uploads: Record<number, UploadRecord[]>;
  rounds: Record<number, ExperimentRoundRecord[]>;
  recommendations: Record<number, RecommendationRecord>;
  statsRequests: string[];
  recommendationRequests: string[];
  startRequests: string[];
  previewStartRequests: string[];
  nextQuestionSessionTokens: string[];
  submittedRatings: Record<string, unknown>[];
  sessionsByExperimentId: Record<number, RaterSessionRecord>;
  analyticsByExperimentId: Record<number, AnalyticsRecord>;
  statsByExperimentId: Record<number, Record<string, unknown>>;
  questionsBySessionToken: Record<string, RaterQuestionRecord>;
  nextExperimentId: number;
  nextUploadId: number;
  nextRoundId: number;
};

function buildExperiment(state: MockState, partial: Partial<ExperimentRecord> = {}): ExperimentRecord {
  return {
    id: state.nextExperimentId++,
    name: 'Smoke Test Experiment',
    internal_name: null,
    created_at: '2026-03-09T00:00:00Z',
    num_ratings_per_question: 3,
    prolific_completion_url: null,
    question_count: 0,
    rating_count: 0,
    status: 'DRAFT',
    archived_at: null,
    ...partial,
  };
}

function createMockState(): MockState {
  return {
    experiments: [],
    uploads: {},
    rounds: {},
    recommendations: {},
    statsRequests: [],
    recommendationRequests: [],
    startRequests: [],
    previewStartRequests: [],
    nextQuestionSessionTokens: [],
    submittedRatings: [],
    sessionsByExperimentId: {},
    analyticsByExperimentId: {},
    statsByExperimentId: {},
    questionsBySessionToken: {},
    nextExperimentId: 1,
    nextUploadId: 1,
    nextRoundId: 1,
  };
}

function extractExperimentId(url: URL): number {
  const match = url.pathname.match(/\/experiments\/(\d+)\//);
  if (!match) {
    throw new Error(`Missing experiment id in path: ${url.pathname}`);
  }
  return Number(match[1]);
}

function buildRound(state: MockState, round: Partial<ExperimentRoundRecord>): ExperimentRoundRecord {
  return {
    id: state.nextRoundId++,
    round_number: 0,
    prolific_study_id: `study-${state.nextRoundId}`,
    prolific_study_status: 'UNPUBLISHED',
    places_requested: 0,
    description: 'Mock study description',
    estimated_completion_time: 60,
    reward: 900,
    device_compatibility: ['desktop'],
    created_at: '2026-03-09T00:00:00Z',
    prolific_study_url: 'https://app.prolific.com/researcher/workspaces/studies/mock-study',
    ...round,
  };
}

async function fulfillJson(route: Route, status: number, body: unknown) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

async function installApiMocks(
  page: Page,
  state: MockState,
  options: {
    prolificEnabled?: boolean;
    currencyCode?: string | null;
    currencySymbol?: string | null;
    spendMinorUnits?: number;
  } = {}
) {
  const prolificEnabled = options.prolificEnabled ?? true;
  const currencyCode = options.currencyCode ?? null;
  const currencySymbol = options.currencySymbol ?? null;
  const spendMinorUnits = options.spendMinorUnits ?? 0;

  await page.context().route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const { pathname, search } = url;
    const method = request.method();

    if (pathname === '/api/v1/intercom/tokens/') {
      await route.fulfill({ status: 204, body: '' });
      return;
    }

    if (pathname === '/api/admin/auth/logout') {
      await fulfillJson(route, 200, { ok: true });
      return;
    }

    if (pathname === '/api/admin/platform-status') {
      await fulfillJson(route, 200, {
        prolific_enabled: prolificEnabled,
        currency_code: currencyCode,
        currency_symbol: currencySymbol,
      });
      return;
    }

    if (pathname === '/api/admin/experiments' && method === 'GET') {
      // Mirror the backend's archived filtering so the detail page's per-id
      // fetch (not the list) is what resolves an archived experiment:
      // `include_archived=true` returns both, `archived=true` returns only
      // archived, and the default hides archived rows.
      const includeArchived = url.searchParams.get('include_archived') === 'true';
      const archivedOnly = url.searchParams.get('archived') === 'true';
      const rows = includeArchived
        ? state.experiments
        : archivedOnly
          ? state.experiments.filter((item) => item.archived_at !== null)
          : state.experiments.filter((item) => item.archived_at === null);
      await fulfillJson(route, 200, rows);
      return;
    }

    if (pathname === '/api/admin/experiments' && method === 'POST') {
      const payload = request.postDataJSON() as { name: string; num_ratings_per_question: number };
      const experiment = buildExperiment(state, {
        name: payload.name,
        num_ratings_per_question: payload.num_ratings_per_question,
      });
      state.experiments = [experiment];
      state.uploads[experiment.id] = [];
      state.rounds[experiment.id] = [];
      state.recommendations[experiment.id] = {
        avg_time_per_question_seconds: 0,
        remaining_rating_actions: 0,
        total_hours_remaining: 0,
        recommended_places: 0,
        is_complete: false,
      };
      await fulfillJson(route, 200, experiment);
      return;
    }

    const experimentByIdMatch = pathname.match(/^\/api\/admin\/experiments\/(\d+)$/);
    if (experimentByIdMatch && method === 'GET') {
      const experimentId = Number(experimentByIdMatch[1]);
      const experiment = state.experiments.find((item) => item.id === experimentId);
      if (!experiment) {
        await fulfillJson(route, 404, { detail: 'Experiment not found' });
        return;
      }
      await fulfillJson(route, 200, experiment);
      return;
    }

    if (pathname.endsWith('/upload') && method === 'POST') {
      const experimentId = extractExperimentId(url);
      const upload = {
        id: state.nextUploadId++,
        filename: 'sample_questions.csv',
        uploaded_at: '2026-03-09T00:01:00Z',
        question_count: 2,
        dataset_meta: null,
      };
      state.uploads[experimentId] = [upload];
      const experiment = state.experiments.find((item) => item.id === experimentId);
      if (experiment) {
        experiment.question_count = 2;
      }
      await fulfillJson(route, 200, {
        message: 'Uploaded 2 questions',
        meta_applied: [],
        meta_conflicts: [],
      });
      return;
    }

    if (pathname.endsWith('/uploads') && method === 'GET') {
      const experimentId = extractExperimentId(url);
      await fulfillJson(route, 200, state.uploads[experimentId] || []);
      return;
    }

    if (pathname.endsWith('/stats') && method === 'GET') {
      const experimentId = extractExperimentId(url);
      state.statsRequests.push(search);
      const experiment = state.experiments.find((item) => item.id === experimentId);
      const override = state.statsByExperimentId[experimentId];
      await fulfillJson(route, 200, override ?? {
        experiment_name: experiment?.name ?? 'Unknown',
        total_questions: experiment?.question_count ?? 0,
        questions_complete: 0,
        total_ratings: 0,
        effective_ratings: 0,
        total_raters: 0,
        target_ratings_per_question: experiment?.num_ratings_per_question ?? 3,
      });
      return;
    }

    if (pathname.endsWith('/analytics') && method === 'GET') {
      const analytics = state.analyticsByExperimentId[extractExperimentId(url)];
      await fulfillJson(route, 200, analytics ?? {
        experiment_name: 'Smoke Test Experiment',
        overview: {
          total_ratings: 0,
          total_questions: 2,
          total_raters: 0,
          avg_response_time_seconds: 0,
          avg_confidence: 0,
        },
        questions: [],
        raters: [],
      });
      return;
    }

    if (pathname.endsWith('/prolific/recommend') && method === 'GET') {
      const experimentId = extractExperimentId(url);
      state.recommendationRequests.push(search);
      await fulfillJson(route, 200, state.recommendations[experimentId]);
      return;
    }

    if (pathname.endsWith('/prolific/rounds') && method === 'GET') {
      const experimentId = extractExperimentId(url);
      await fulfillJson(route, 200, state.rounds[experimentId] || []);
      return;
    }

    if (pathname.endsWith('/prolific/sync-spend') && method === 'POST') {
      await fulfillJson(route, 200, { spend_minor_units: spendMinorUnits });
      return;
    }

    if (pathname.endsWith('/prolific/pilot') && method === 'POST') {
      const experimentId = extractExperimentId(url);
      const payload = request.postDataJSON() as {
        description: string;
        estimated_completion_time: number;
        reward: number;
        pilot_places: number;
      };
      const pilot = buildRound(state, {
        round_number: 0,
        prolific_study_id: 'study-pilot-1',
        prolific_study_status: 'UNPUBLISHED',
        places_requested: payload.pilot_places,
        prolific_study_url: 'https://app.prolific.com/researcher/workspaces/studies/study-pilot-1',
      });
      state.rounds[experimentId] = [pilot];
      state.recommendations[experimentId] = {
        avg_time_per_question_seconds: 42,
        remaining_rating_actions: 320,
        total_hours_remaining: 3.7,
        recommended_places: 4,
        is_complete: false,
      };
      const experiment = state.experiments.find((item) => item.id === experimentId);
      if (experiment) {
        experiment.prolific_completion_url = 'https://app.prolific.com/submissions/complete?cc=TEST1234';
      }
      await fulfillJson(route, 200, pilot);
      return;
    }

    const publishMatch = pathname.match(/\/prolific\/rounds\/(\d+)\/publish$/);
    if (publishMatch && method === 'POST') {
      const experimentId = extractExperimentId(url);
      const roundId = Number(publishMatch[1]);
      const round = (state.rounds[experimentId] || []).find((item) => item.id === roundId);
      if (!round) {
        await fulfillJson(route, 404, { detail: 'Experiment round not found' });
        return;
      }
      round.prolific_study_status = 'ACTIVE';
      await fulfillJson(route, 200, { message: 'Study published on Prolific', status: 'ACTIVE' });
      return;
    }

    const closeMatch = pathname.match(/\/prolific\/rounds\/(\d+)\/close$/);
    if (closeMatch && method === 'POST') {
      const experimentId = extractExperimentId(url);
      const roundId = Number(closeMatch[1]);
      const round = (state.rounds[experimentId] || []).find((item) => item.id === roundId);
      if (!round) {
        await fulfillJson(route, 404, { detail: 'Experiment round not found' });
        return;
      }
      round.prolific_study_status = 'AWAITING_REVIEW';
      await fulfillJson(route, 200, { message: 'Round closed on Prolific', status: 'AWAITING_REVIEW' });
      return;
    }

    if (pathname.endsWith('/prolific/rounds') && method === 'POST') {
      const experimentId = extractExperimentId(url);
      const payload = request.postDataJSON() as { places: number };
      const nextRoundNumber = (state.rounds[experimentId] || []).length;
      const round = buildRound(state, {
        round_number: nextRoundNumber,
        prolific_study_id: `study-round-${nextRoundNumber}`,
        prolific_study_status: 'UNPUBLISHED',
        places_requested: payload.places,
        prolific_study_url: `https://app.prolific.com/researcher/workspaces/studies/study-round-${nextRoundNumber}`,
      });
      state.rounds[experimentId] = [...(state.rounds[experimentId] || []), round];
      await fulfillJson(route, 200, round);
      return;
    }

    if (pathname === '/api/raters/start' && method === 'POST') {
      state.startRequests.push(search);
      state.previewStartRequests.push(search);
      const experimentId = Number(url.searchParams.get('experiment_id') || '0');
      const experiment = state.experiments.find((item) => item.id === experimentId);
      const session =
        state.sessionsByExperimentId[experimentId] || {
          rater_id: 101,
          session_start: '2026-03-09T00:02:00Z',
          session_end_time: '2099-03-09T01:02:00Z',
          experiment_name: experiment?.name ?? 'Smoke Test Experiment',
          completion_url:
            experiment?.prolific_completion_url ??
            'https://app.prolific.com/submissions/complete?cc=TEST1234',
          rater_session_token: `token-exp-${experimentId || 'default'}`,
        };
      await fulfillJson(route, 200, {
        ...session,
      });
      return;
    }

    if (pathname === '/api/raters/next-question' && method === 'GET') {
      const sessionToken = request.headers()['x-rater-session'] || '';
      state.nextQuestionSessionTokens.push(sessionToken);
      await fulfillJson(
        route,
        200,
        state.questionsBySessionToken[sessionToken] || {
          id: 500,
          question_text: 'Is this workflow ready for release?',
          options: 'Yes|No',
          question_type: 'MC',
        }
      );
      return;
    }

    if (pathname === '/api/raters/submit' && method === 'POST') {
      state.submittedRatings.push(request.postDataJSON());
      await fulfillJson(route, 200, { id: 1, success: true });
      return;
    }

    if (pathname === '/api/raters/session-status' && method === 'GET') {
      await fulfillJson(route, 200, {
        is_active: true,
        time_remaining_seconds: 3600,
        questions_completed: 0,
      });
      return;
    }

    if (pathname === '/api/raters/end-session' && method === 'POST') {
      await fulfillJson(route, 200, { message: 'ok' });
      return;
    }

    throw new Error(`Unhandled API request: ${method} ${pathname}`);
  });
}

test.beforeEach(async ({ page }) => {
  page.on('dialog', (dialog) => dialog.accept());
});

test('create experiment and upload CSV shows the upload and success toast', async ({ page }) => {
  const state = createMockState();
  await installApiMocks(page, state);

  await page.goto('/admin');

  await page.getByTestId('experiment-name-input').fill('Hour Breakdown Smoke Test');
  await page.getByTestId('ratings-per-question-input').fill('3');
  await page.getByRole('button', { name: 'Create Experiment' }).click();

  await expect(page.getByRole('heading', { name: 'Hour Breakdown Smoke Test' })).toBeVisible();

  // Post-creation lands on Overview; upload lives on the Questions tab.
  await page.getByTestId('tab-questions').click();
  const csvPath = fileURLToPath(new URL('./fixtures/sample_questions.csv', import.meta.url));
  await page.getByTestId('upload-csv-input').setInputFiles(csvPath);
  await page.getByTestId('upload-csv-button').click();

  await expect(page.getByText('sample_questions.csv')).toBeVisible();
  await expect(page.getByText('2 questions', { exact: true })).toBeVisible();
  await expect(page.getByText('Uploaded 2 questions')).toBeVisible();

  // Pilot form lives on the Launch tab. When Prolific is enabled we
  // deliberately don't show a Prolific-mode banner (would be noise in prod).
  await page.getByTestId('tab-launch').click();
  await expect(page.getByTestId('prolific-mode-notice')).toHaveCount(0);
  await expect(page.getByTestId('run-pilot-button')).toBeVisible();
});

test('a slow upload shows progress and re-enables the form when it finishes', async ({ page }) => {
  const state = createMockState();
  await installApiMocks(page, state);

  // Hold the upload response open so the in-flight UI is observable. The
  // bytes are already sent by then, so this exercises the 'processing' phase.
  let releaseUpload: () => void = () => {};
  const uploadHeld = new Promise<void>((resolve) => {
    releaseUpload = resolve;
  });
  await page.route('**/experiments/*/upload', async (route) => {
    await uploadHeld;
    await route.fallback();
  });

  await page.goto('/admin');
  await page.getByTestId('experiment-name-input').fill('Slow Upload Test');
  await page.getByTestId('ratings-per-question-input').fill('3');
  await page.getByRole('button', { name: 'Create Experiment' }).click();
  await expect(page.getByRole('heading', { name: 'Slow Upload Test' })).toBeVisible();

  await page.getByTestId('tab-questions').click();
  const csvPath = fileURLToPath(new URL('./fixtures/sample_questions.csv', import.meta.url));
  await page.getByTestId('upload-csv-input').setInputFiles(csvPath);
  await page.getByTestId('upload-csv-button').click();

  // Which phase shows depends on when the bytes clear the wire, so assert the
  // panel is up and naming the file rather than pinning it to one phase.
  const progress = page.getByTestId('upload-progress');
  await expect(progress).toBeVisible();
  await expect(progress).toContainText('sample_questions.csv');
  await expect(page.getByTestId('upload-csv-button')).toBeDisabled();
  await expect(page.getByTestId('upload-csv-input')).toBeDisabled();

  releaseUpload();

  await expect(page.getByText('Uploaded 2 questions')).toBeVisible();
  // Snapshot, not a retrying assertion: the panel must already be gone the
  // moment the success toast paints. A retrying toHaveCount(0) would happily
  // pass while the two contradicted each other for a few hundred ms.
  expect(await progress.count()).toBe(0);
  await expect(page.getByTestId('upload-csv-input')).toBeEnabled();
});

test('run pilot, close it, and launch a round from an experiment with uploaded questions', async ({ page }) => {
  const state = createMockState();
  state.experiments = [
    buildExperiment(state, {
      id: 1,
      name: 'Hour Breakdown Smoke Test',
      num_ratings_per_question: 3,
      question_count: 2,
      prolific_completion_url: null,
    }),
  ];
  state.nextExperimentId = 2;
  state.uploads[1] = [
    {
      id: 1,
      filename: 'sample_questions.csv',
      uploaded_at: '2026-03-09T00:01:00Z',
      question_count: 2,
      dataset_meta: null,
    },
  ];
  state.rounds[1] = [];
  state.recommendations[1] = {
    avg_time_per_question_seconds: 0,
    remaining_rating_actions: 0,
    total_hours_remaining: 0,
    recommended_places: 0,
    is_complete: false,
  };

  await installApiMocks(page, state);
  await page.goto('/admin/experiments/1');

  // Pilot form and round controls all live on the Launch tab.
  await page.getByTestId('tab-launch').click();
  await page.getByTestId('pilot-description-input').fill('Pilot description for smoke coverage');
  await page.getByTestId('pilot-estimated-completion-time-input').fill('60');
  await page.getByTestId('pilot-reward-input').fill('900');
  await page.getByTestId('pilot-places-input').fill('5');
  await page.getByTestId('run-pilot-button').click();

  await expect(page.getByText('Pilot Round', { exact: true })).toBeVisible();
  await expect(page.getByTestId('publish-round-0')).toBeVisible();
  await expect(page.getByTestId('recommendation-panel')).toContainText('Recommendation for next round');
  await expect(page.getByTestId('completion-url-input')).toContainText(
    'https://app.prolific.com/submissions/complete?cc=TEST1234'
  );
  // Button is hidden (not just disabled) while the previous round is open,
  // matching the pre-pilot state — we don't render mockable buttons.
  await expect(page.getByTestId('launch-round-button')).toHaveCount(0);

  await page.getByTestId('publish-round-0').click();
  await expect(page.getByTestId('close-round-0')).toBeVisible();
  await expect(page.getByTestId('study-rounds-list').getByText('ACTIVE')).toBeVisible();

  await page.getByTestId('close-round-0').click();
  await expect(page.getByTestId('study-rounds-list').getByText('AWAITING_REVIEW')).toBeVisible();
  await expect(page.getByTestId('launch-round-button')).toBeVisible();

  await page.getByTestId('launch-round-button').click();
  await expect(page.getByTestId('study-rounds-list').getByText('Round 1')).toBeVisible();
  await expect(page.getByTestId('study-rounds-list').getByText('4 places', { exact: true })).toBeVisible();
  await expect(page.getByTestId('publish-round-1')).toBeVisible();

  const exportLink = page.getByTestId('export-link');
  await expect(exportLink).toHaveAttribute('href', /\/api\/admin\/experiments\/1\/export$/);
  // include-preview toggle lives on the Overview panel.
  await page.getByTestId('tab-overview').click();
  await page.getByTestId('include-preview-toggle').click();
  await expect(exportLink).toHaveAttribute('href', /include_preview=true/);
  await expect
    .poll(() => state.statsRequests.some((query) => query.includes('include_preview=true')))
    .toBeTruthy();
  await expect
    .poll(() => state.recommendationRequests.some((query) => query.includes('include_preview=true')))
    .toBeTruthy();
});

test('preview participant link opens /rate with preview mode and starts one preview session', async ({ page, context }) => {
  const state = createMockState();
  state.experiments = [
    buildExperiment(state, {
      id: 1,
      name: 'Preview Experiment',
      question_count: 2,
      prolific_completion_url: 'https://app.prolific.com/submissions/complete?cc=TEST1234',
    }),
  ];
  state.nextExperimentId = 2;
  state.uploads[1] = [
    {
      id: 1,
      filename: 'sample_questions.csv',
      uploaded_at: '2026-03-09T00:00:00Z',
      question_count: 2,
      dataset_meta: null,
    },
  ];
  state.rounds[1] = [];
  state.recommendations[1] = {
    avg_time_per_question_seconds: 0,
    remaining_rating_actions: 0,
    total_hours_remaining: 0,
    recommended_places: 0,
    is_complete: false,
  };

  await installApiMocks(page, state);
  await page.goto('/admin/experiments/1');

  // Preview button lives on the Launch tab.
  await page.getByTestId('tab-launch').click();
  const popupPromise = context.waitForEvent('page');
  await page.getByTestId('preview-participant-button').click();
  const popup = await popupPromise;
  await popup.waitForLoadState('networkidle');

  await expect(popup).toHaveURL(/preview=true/);
  await expect(popup.getByText('Preview mode')).toBeVisible();
  await expect(popup.getByText('Preview Experiment')).toBeVisible();
  await expect(popup.getByText('Is this workflow ready for release?')).toBeVisible();
  await expect.poll(() => state.previewStartRequests.length).toBe(1);
  await expect(state.previewStartRequests[0]).toContain('preview=true');
});

test('long-context question links document separately and shows only question in rater card', async ({ page, context }) => {
  const state = createMockState();
  state.experiments = [
    buildExperiment(state, {
      id: 1,
      name: 'Long Context Experiment',
      question_count: 1,
      prolific_completion_url: 'https://app.prolific.com/submissions/complete?cc=TEST1234',
    }),
  ];
  state.nextExperimentId = 2;
  state.uploads[1] = [];
  state.rounds[1] = [];
  state.recommendations[1] = {
    avg_time_per_question_seconds: 0,
    remaining_rating_actions: 0,
    total_hours_remaining: 0,
    recommended_places: 0,
    is_complete: false,
  };
  state.sessionsByExperimentId[1] = {
    rater_id: 301,
    session_start: '2026-03-09T00:05:00Z',
    session_end_time: '2099-03-09T01:05:00Z',
    experiment_name: 'Long Context Experiment',
    completion_url: 'https://app.prolific.com/submissions/complete?cc=TEST1234',
    rater_session_token: 'token-long-context',
  };
  state.questionsBySessionToken['token-long-context'] = {
    id: 503,
    question_text: 'Document line one\nDocument line two\n\n--- QUESTION ---\nWhich answer follows from the document?',
    options: 'A|B',
    question_type: 'MC',
  };

  await installApiMocks(page, state);
  await page.goto('/rate?experiment_id=1&PROLIFIC_PID=pid-1&STUDY_ID=study-1&SESSION_ID=session-1');

  const documentLink = page.getByRole('link', { name: 'Open document in new tab' });
  await expect(documentLink).toBeVisible();
  await expect(page.getByText('Which answer follows from the document?')).toBeVisible();
  await expect(page.getByText('Document line one')).toHaveCount(0);

  const documentPopupPromise = context.waitForEvent('page');
  await documentLink.click();
  const documentPopup = await documentPopupPromise;
  await documentPopup.waitForLoadState('domcontentloaded');

  await expect(documentPopup.getByRole('heading', { name: 'Document', exact: true })).toBeVisible();
  // The external question id must not leak into the popup a rater can read.
  await expect(documentPopup).toHaveTitle('Document');
  await expect(documentPopup.getByText('Document line one')).toBeVisible();
});

// Seeds a rater session serving exactly one question, for the parent-context
// rendering cases below.
function seedRaterWithQuestion(state: MockState, question: RaterQuestionRecord) {
  state.experiments = [
    buildExperiment(state, {
      id: 1,
      name: 'Parent Context Experiment',
      question_count: 1,
      prolific_completion_url: 'https://app.prolific.com/submissions/complete?cc=TEST1234',
    }),
  ];
  state.nextExperimentId = 2;
  state.uploads[1] = [];
  state.rounds[1] = [];
  state.recommendations[1] = {
    avg_time_per_question_seconds: 0,
    remaining_rating_actions: 0,
    total_hours_remaining: 0,
    recommended_places: 0,
    is_complete: false,
  };
  state.sessionsByExperimentId[1] = {
    rater_id: 302,
    session_start: '2026-03-09T00:05:00Z',
    session_end_time: '2099-03-09T01:05:00Z',
    experiment_name: 'Parent Context Experiment',
    completion_url: 'https://app.prolific.com/submissions/complete?cc=TEST1234',
    rater_session_token: 'token-parent-context',
  };
  state.questionsBySessionToken['token-parent-context'] = question;
}

const RATER_URL = '/rate?experiment_id=1&PROLIFIC_PID=pid-1&STUDY_ID=study-1&SESSION_ID=session-1';

test('a long parent question moves the document behind the link, not into the card', async ({
  page,
  context,
}) => {
  const state = createMockState();
  const document = `LONGBENCH DOCUMENT BODY ${'padding '.repeat(400)}`;
  expect(document.length).toBeGreaterThan(2000);

  seedRaterWithQuestion(state, {
    id: 504,
    question_text: 'Which answer follows from the document?',
    options: 'A|B',
    question_type: 'MC',
    parent_question_text: document,
  });

  await installApiMocks(page, state);
  await page.goto(RATER_URL);

  const documentLink = page.getByRole('link', { name: 'Open document in new tab' });
  await expect(documentLink).toBeVisible();
  await expect(page.getByText('Which answer follows from the document?')).toBeVisible();
  // The whole point: a 3KB document must not be dumped into the rating card.
  await expect(page.getByText('Context', { exact: true })).toHaveCount(0);
  await expect(page.getByText('LONGBENCH DOCUMENT BODY')).toHaveCount(0);

  const popupPromise = context.waitForEvent('page');
  await documentLink.click();
  const popup = await popupPromise;
  await popup.waitForLoadState('domcontentloaded');
  await expect(popup.getByText('LONGBENCH DOCUMENT BODY')).toBeVisible();
});

test('a short parent question stays inline in the context box', async ({ page }) => {
  const state = createMockState();
  const preamble = 'Customer review: arrived late but exceeded expectations.';

  seedRaterWithQuestion(state, {
    id: 505,
    question_text: 'Does the review express satisfaction?',
    options: 'Yes|No',
    question_type: 'MC',
    parent_question_text: preamble,
  });

  await installApiMocks(page, state);
  await page.goto(RATER_URL);

  await expect(page.getByText('Context', { exact: true })).toBeVisible();
  await expect(page.getByText(preamble)).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open document in new tab' })).toHaveCount(0);
});

test('an MC question with no options submits the typed free-text answer', async ({ page }) => {
  const state = createMockState();

  // Datasets whose upload omitted `question_type` arrive typed MC with empty
  // options. The card falls back to a textarea, so submit must read that.
  seedRaterWithQuestion(state, {
    id: 506,
    question_text: 'Summarize what the document recommends.',
    options: '',
    question_type: 'MC',
  });

  await installApiMocks(page, state);
  const dialogs: string[] = [];
  page.on('dialog', async (dialog) => {
    dialogs.push(dialog.message());
    await dialog.dismiss();
  });

  await page.goto(RATER_URL);

  const answer = page.getByPlaceholder('Type your answer here...');
  await expect(answer).toBeVisible();
  await answer.fill('It recommends staged rollout.');
  await page.getByRole('button', { name: 'Submit answer' }).click();

  await expect.poll(() => state.submittedRatings.length).toBe(1);
  expect(state.submittedRatings[0]).toMatchObject({ answer: 'It recommends staged rollout.' });
  expect(dialogs).toEqual([]);
});

test('rater ignores a stored session from another experiment and starts a fresh one', async ({ page }) => {
  const state = createMockState();
  state.experiments = [
    buildExperiment(state, {
      id: 1,
      name: 'Old Experiment',
      question_count: 1,
      prolific_completion_url: 'https://app.prolific.com/submissions/complete?cc=OLD1111',
    }),
    buildExperiment(state, {
      id: 2,
      name: 'Fresh Experiment',
      question_count: 1,
      prolific_completion_url: 'https://app.prolific.com/submissions/complete?cc=NEW2222',
    }),
  ];
  state.nextExperimentId = 3;
  state.uploads[1] = [];
  state.uploads[2] = [];
  state.rounds[1] = [];
  state.rounds[2] = [];
  state.recommendations[1] = {
    avg_time_per_question_seconds: 0,
    remaining_rating_actions: 0,
    total_hours_remaining: 0,
    recommended_places: 0,
    is_complete: false,
  };
  state.recommendations[2] = {
    avg_time_per_question_seconds: 0,
    remaining_rating_actions: 0,
    total_hours_remaining: 0,
    recommended_places: 0,
    is_complete: false,
  };
  state.sessionsByExperimentId[2] = {
    rater_id: 202,
    session_start: '2026-03-09T00:05:00Z',
    session_end_time: '2099-03-09T01:05:00Z',
    experiment_name: 'Fresh Experiment',
    completion_url: 'https://app.prolific.com/submissions/complete?cc=NEW2222',
    rater_session_token: 'token-exp-2',
  };
  state.questionsBySessionToken['token-exp-1'] = {
    id: 501,
    question_text: 'Old experiment question',
    options: 'Yes,No',
    question_type: 'MC',
  };
  state.questionsBySessionToken['token-exp-2'] = {
    id: 502,
    question_text: 'Fresh experiment question',
    options: 'Yes,No',
    question_type: 'MC',
  };

  await page.addInitScript(() => {
    window.sessionStorage.setItem(
      'hrp_rater_session',
      JSON.stringify({
        experimentId: '1',
        session: {
          rater_id: 101,
          session_start: '2026-03-09T00:00:00Z',
          session_end_time: '2099-03-09T01:00:00Z',
          experiment_name: 'Old Experiment',
          completion_url: 'https://app.prolific.com/submissions/complete?cc=OLD1111',
          rater_session_token: 'token-exp-1',
        },
      })
    );
  });

  await installApiMocks(page, state);
  await page.goto('/rate?experiment_id=2&PROLIFIC_PID=pid-2&STUDY_ID=study-2&SESSION_ID=session-2');

  await expect(page.getByRole('heading', { name: 'Fresh Experiment' })).toBeVisible();
  await expect(page.getByText('Fresh experiment question')).toBeVisible();
  await expect(page.getByText('Old experiment question')).toHaveCount(0);
  await expect.poll(() => state.startRequests.length).toBe(1);
  await expect(state.startRequests[0]).toContain('experiment_id=2');
  await expect.poll(() => state.nextQuestionSessionTokens[0]).toBe('token-exp-2');

  const persistedSession = await page.evaluate(() => {
    const stored = window.sessionStorage.getItem('hrp_rater_session');
    return stored ? JSON.parse(stored) : null;
  });
  expect(persistedSession.experimentId).toBe('2');
  expect(persistedSession.session.rater_session_token).toBe('token-exp-2');
});

test('disabled mode explains why pilot controls are unavailable', async ({ page }) => {
  const state = createMockState();
  state.experiments = [
    buildExperiment(state, {
      id: 1,
      name: 'Disabled Prolific Experiment',
      question_count: 2,
    }),
  ];
  state.nextExperimentId = 2;
  state.uploads[1] = [];
  state.rounds[1] = [];
  state.recommendations[1] = {
    avg_time_per_question_seconds: 0,
    remaining_rating_actions: 0,
    total_hours_remaining: 0,
    recommended_places: 0,
    is_complete: false,
  };

  await installApiMocks(page, state, { prolificEnabled: false });
  await page.goto('/admin/experiments/1');

  // Prolific-mode banner and preview button live on the Launch tab.
  await page.getByTestId('tab-launch').click();
  await expect(page.getByTestId('prolific-mode-badge')).toHaveText('Disabled');
  await expect(page.getByTestId('prolific-mode-notice')).toContainText('Prolific is disabled for this environment');
  await expect(page.getByTestId('prolific-mode-notice')).toContainText('Configure a Prolific API token');
  await expect(page.getByTestId('preview-participant-button')).toBeVisible();
  await expect(page.getByTestId('run-pilot-button')).toHaveCount(0);
});

test('completion progress stays under 100% while any question is short', async ({ page }) => {
  // Real CulturalBench numbers: 3671 of 3681 effective ratings is 99.7%, which
  // Math.round reported as a full 100% while 10 questions were still short.
  const state = createMockState();
  state.experiments = [
    buildExperiment(state, {
      id: 1,
      name: 'Nearly Complete Experiment',
      question_count: 1227,
      status: 'LAUNCH',
    }),
  ];
  state.nextExperimentId = 2;
  state.uploads[1] = [];
  state.rounds[1] = [];
  state.recommendations[1] = {
    avg_time_per_question_seconds: 0,
    remaining_rating_actions: 10,
    total_hours_remaining: 0,
    recommended_places: 0,
    is_complete: false,
  };
  state.statsByExperimentId[1] = {
    experiment_name: 'Nearly Complete Experiment',
    total_questions: 1227,
    questions_complete: 1217,
    total_ratings: 3865,
    effective_ratings: 3671,
    total_raters: 92,
    target_ratings_per_question: 3,
  };

  await installApiMocks(page, state);
  await page.goto('/admin/experiments/1');

  await expect(page.getByText('1217 of 1227 questions')).toBeVisible();
  await expect(page.getByText('99%', { exact: true })).toBeVisible();
  await expect(page.getByText('100%', { exact: true })).toHaveCount(0);
});

test('completion progress reaches 100% only when every question is at target', async ({ page }) => {
  const state = createMockState();
  state.experiments = [
    buildExperiment(state, {
      id: 1,
      name: 'Complete Experiment',
      question_count: 1227,
      status: 'LAUNCH',
    }),
  ];
  state.nextExperimentId = 2;
  state.uploads[1] = [];
  state.rounds[1] = [];
  state.recommendations[1] = {
    avg_time_per_question_seconds: 0,
    remaining_rating_actions: 0,
    total_hours_remaining: 0,
    recommended_places: 0,
    is_complete: true,
  };
  state.statsByExperimentId[1] = {
    experiment_name: 'Complete Experiment',
    total_questions: 1227,
    questions_complete: 1227,
    total_ratings: 3865,
    effective_ratings: 3681,
    total_raters: 92,
    target_ratings_per_question: 3,
  };

  await installApiMocks(page, state);
  await page.goto('/admin/experiments/1');

  await expect(page.getByText('100%', { exact: true })).toBeVisible();
});

test('a non-numeric experiment id shows a friendly not-found message', async ({ page }) => {
  const state = createMockState();
  await installApiMocks(page, state);

  // A typo'd / stale URL segment isn't a valid id. The page should short-circuit
  // to "Experiment not found" rather than leaking the backend's 422 JSON.
  await page.goto('/admin/experiments/abc');

  await expect(page.getByText('Experiment not found')).toBeVisible();
});

test('opening an archived experiment resolves via the per-id fetch', async ({ page }) => {
  const state = createMockState();
  state.experiments = [
    buildExperiment(state, {
      id: 1,
      name: 'Archived Experiment',
      question_count: 2,
      archived_at: '2026-03-10T00:00:00Z',
    }),
  ];
  state.nextExperimentId = 2;
  state.uploads[1] = [];
  state.rounds[1] = [];
  state.recommendations[1] = {
    avg_time_per_question_seconds: 0,
    remaining_rating_actions: 0,
    total_hours_remaining: 0,
    recommended_places: 0,
    is_complete: false,
  };

  await installApiMocks(page, state);
  await page.goto('/admin/experiments/1');

  // The list endpoint hides archived rows, so the old list-then-.find() lookup
  // would 404 here. Resolving the page proves the detail view now fetches the
  // experiment by id directly.
  await expect(page.getByRole('heading', { name: 'Archived Experiment' })).toBeVisible();
  await expect(page.getByText('Experiment not found')).toHaveCount(0);
});

test('spend card formats a zero-decimal currency (ISK) without decimals', async ({ page }) => {
  const state = createMockState();
  state.experiments = [
    buildExperiment(state, { id: 1, name: 'ISK Experiment', question_count: 2 }),
  ];
  state.nextExperimentId = 2;
  state.uploads[1] = [];
  state.rounds[1] = [];
  state.recommendations[1] = {
    avg_time_per_question_seconds: 0,
    remaining_rating_actions: 0,
    total_hours_remaining: 0,
    recommended_places: 0,
    is_complete: false,
  };

  // ISK has no minor unit, so 90000 must render as kr90000, not kr900.00. The
  // background sync hydrates the Spend card on the (default) Overview tab.
  await installApiMocks(page, state, {
    prolificEnabled: true,
    currencyCode: 'ISK',
    currencySymbol: 'kr',
    spendMinorUnits: 90000,
  });
  await page.goto('/admin/experiments/1');

  await expect(page.getByText('kr90000')).toBeVisible();
  await expect(page.getByText('kr900.00')).toHaveCount(0);
});

// Locale and timezone are pinned so the rendered timestamp is comparable.
test.describe('analytics raters tab', () => {
  test.use({ timezoneId: 'UTC', locale: 'en-GB' });

  test('renders session start times served in the backend timestamp format', async ({ page }) => {
    const state = createMockState();
    state.experiments = [
      buildExperiment(state, {
        id: 1,
        name: 'Analytics Smoke Test',
        internal_name: 'Analytics Internal Name',
        question_count: 2,
        rating_count: 3,
      }),
    ];
    state.nextExperimentId = 2;
    state.uploads[1] = [];
    state.rounds[1] = [];
    state.recommendations[1] = {
      avg_time_per_question_seconds: 0,
      remaining_rating_actions: 0,
      total_hours_remaining: 0,
      recommended_places: 0,
      is_complete: false,
    };
    state.analyticsByExperimentId[1] = {
      experiment_name: 'Analytics Smoke Test',
      overview: {
        total_ratings: 3,
        total_questions: 2,
        total_raters: 1,
        avg_response_time_seconds: 69.94,
        avg_confidence: 3.08,
      },
      questions: [],
      raters: [
        {
          prolific_id: '660d6a1f4a7f1337de235daa',
          study_id: '6a637562575f23b5aaf81ae4',
          // Microsecond precision and a UTC designator, as the API sends it.
          session_start: '2026-07-24T14:26:30.179021Z',
          session_end: null,
          is_active: true,
          num_ratings: 3,
          total_response_time_seconds: 209.82,
          avg_response_time_seconds: 69.94,
          avg_confidence: 3.08,
        },
      ],
    };

    await installApiMocks(page, state);
    await page.goto('/admin/experiments/1');

    await page.getByRole('button', { name: 'View analytics' }).click();
    // Analytics is its own route now, titled with the internal name.
    await expect(page).toHaveURL('/admin/experiments/1/analytics');
    await expect(page.getByRole('heading', { name: 'Analytics: Analytics Internal Name' })).toBeVisible();

    await page.getByRole('button', { name: 'Raters', exact: true }).click();
    await expect(page).toHaveURL('/admin/experiments/1/analytics/raters');

    const raterRow = page.getByRole('row').filter({ hasText: '660d6a1f4a7f1337de235daa' });
    await expect(raterRow).toContainText('24/07/2026, 14:26:30');
    await expect(page.getByText('Invalid Date')).toHaveCount(0);
  });

  test('deeplinks straight to a tab', async ({ page }) => {
    const state = createMockState();
    state.experiments = [
      buildExperiment(state, {
        id: 1,
        name: 'Analytics Smoke Test',
        internal_name: 'Analytics Internal Name',
        question_count: 2,
        rating_count: 3,
      }),
    ];
    state.nextExperimentId = 2;
    state.analyticsByExperimentId[1] = {
      experiment_name: 'Analytics Smoke Test',
      overview: {
        total_ratings: 3,
        total_questions: 2,
        total_raters: 1,
        avg_response_time_seconds: 69.94,
        avg_confidence: 3.08,
      },
      questions: [],
      raters: [
        {
          prolific_id: '660d6a1f4a7f1337de235daa',
          study_id: '6a637562575f23b5aaf81ae4',
          session_start: '2026-07-24T14:26:30.179021Z',
          session_end: null,
          is_active: true,
          num_ratings: 3,
          total_response_time_seconds: 209.82,
          avg_response_time_seconds: 69.94,
          avg_confidence: 3.08,
        },
      ],
    };

    await installApiMocks(page, state);
    await page.goto('/admin/experiments/1/analytics/raters');

    await expect(page.getByRole('heading', { name: 'Analytics: Analytics Internal Name' })).toBeVisible();
    await expect(page.getByRole('row').filter({ hasText: '660d6a1f4a7f1337de235daa' })).toBeVisible();

    // Unknown tab segments fall back to the overview URL.
    await page.goto('/admin/experiments/1/analytics/bogus');
    await expect(page).toHaveURL('/admin/experiments/1/analytics');
    await expect(page.getByText('Total Ratings')).toBeVisible();
  });
});
