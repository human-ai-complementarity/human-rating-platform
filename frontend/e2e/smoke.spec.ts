import { expect, test, type Page, type Route } from '@playwright/test';
import { fileURLToPath } from 'node:url';

type ExperimentRecord = {
  id: number;
  name: string;
  created_at: string;
  num_ratings_per_question: number;
  prolific_completion_url: string | null;
  question_count: number;
  rating_count: number;
};

type UploadRecord = {
  id: number;
  filename: string;
  uploaded_at: string;
  question_count: number;
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

type RaterQuestionRecord = {
  id: number;
  question_id: string;
  question_text: string;
  options: string | null;
  question_type: string;
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
  sessionsByExperimentId: Record<number, RaterSessionRecord>;
  questionsBySessionToken: Record<string, RaterQuestionRecord>;
  nextExperimentId: number;
  nextUploadId: number;
  nextRoundId: number;
};

function buildExperiment(state: MockState, partial: Partial<ExperimentRecord> = {}): ExperimentRecord {
  return {
    id: state.nextExperimentId++,
    name: 'Smoke Test Experiment',
    created_at: '2026-03-09T00:00:00Z',
    num_ratings_per_question: 3,
    prolific_completion_url: null,
    question_count: 0,
    rating_count: 0,
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
    sessionsByExperimentId: {},
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
  options: { prolificEnabled?: boolean } = {}
) {
  const prolificEnabled = options.prolificEnabled ?? true;

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
        currency_code: null,
        currency_symbol: null,
      });
      return;
    }

    if (pathname === '/api/admin/experiments' && method === 'GET') {
      await fulfillJson(route, 200, state.experiments);
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

    if (pathname.endsWith('/upload') && method === 'POST') {
      const experimentId = extractExperimentId(url);
      const upload = {
        id: state.nextUploadId++,
        filename: 'sample_questions.csv',
        uploaded_at: '2026-03-09T00:01:00Z',
        question_count: 2,
      };
      state.uploads[experimentId] = [upload];
      const experiment = state.experiments.find((item) => item.id === experimentId);
      if (experiment) {
        experiment.question_count = 2;
      }
      await fulfillJson(route, 200, { message: 'Uploaded 2 questions' });
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
      await fulfillJson(route, 200, {
        experiment_name: experiment?.name ?? 'Unknown',
        total_questions: experiment?.question_count ?? 0,
        questions_complete: 0,
        total_ratings: 0,
        total_raters: 0,
        target_ratings_per_question: experiment?.num_ratings_per_question ?? 3,
      });
      return;
    }

    if (pathname.endsWith('/analytics') && method === 'GET') {
      await fulfillJson(route, 200, {
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
          question_id: 'q-1',
          question_text: 'Is this workflow ready for release?',
          options: 'Yes|No',
          question_type: 'MC',
        }
      );
      return;
    }

    if (pathname === '/api/raters/submit' && method === 'POST') {
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

test('create experiment, upload CSV, run pilot, close it, and launch a round', async ({ page }) => {
  const state = createMockState();
  await installApiMocks(page, state);

  await page.goto('/admin');

  await page.getByTestId('experiment-name-input').fill('Hour Breakdown Smoke Test');
  await page.getByTestId('ratings-per-question-input').fill('3');
  await page.getByRole('button', { name: 'Create Experiment' }).click();

  await expect(page.getByRole('heading', { name: 'Hour Breakdown Smoke Test' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Prolific Workflow' })).toBeVisible();
  await expect(page.getByTestId('prolific-mode-badge')).toHaveText('Real Mode');
  await expect(page.getByTestId('prolific-mode-notice')).toContainText('Real Prolific mode is enabled');
  await expect(page.getByTestId('run-pilot-button')).toBeVisible();

  const csvPath = fileURLToPath(new URL('../../sample_questions.csv', import.meta.url));
  await page.getByTestId('upload-csv-input').setInputFiles(csvPath);
  await page.getByTestId('upload-csv-button').click();

  await expect(page.getByText('sample_questions.csv')).toBeVisible();
  await expect(page.getByText('2 questions', { exact: true })).toBeVisible();
  await expect(page.getByText('Uploaded 2 questions')).toBeVisible();

  await page.getByTestId('pilot-description-input').fill('Pilot description for smoke coverage');
  await page.getByTestId('pilot-estimated-completion-time-input').fill('60');
  await page.getByTestId('pilot-reward-input').fill('900');
  await page.getByTestId('pilot-places-input').fill('5');
  await page.getByTestId('run-pilot-button').click();

  await expect(page.getByText('Pilot Round', { exact: true })).toBeVisible();
  await expect(page.getByTestId('publish-round-0')).toBeVisible();
  await expect(page.getByTestId('recommendation-panel')).toContainText('Recommendation for next round');
  await expect(page.getByTestId('completion-url-input')).toHaveValue(
    'https://app.prolific.com/submissions/complete?cc=TEST1234'
  );
  await expect(page.getByTestId('launch-round-button')).toBeDisabled();

  await page.getByTestId('publish-round-0').click();
  await expect(page.getByTestId('close-round-0')).toBeVisible();
  await expect(page.getByTestId('study-rounds-list').getByText('ACTIVE')).toBeVisible();

  await page.getByTestId('close-round-0').click();
  await expect(page.getByTestId('study-rounds-list').getByText('AWAITING_REVIEW')).toBeVisible();
  await expect(page.getByTestId('launch-round-button')).toBeEnabled();

  await page.getByTestId('launch-round-button').click();
  await expect(page.getByTestId('study-rounds-list').getByText('Round 1')).toBeVisible();
  await expect(page.getByTestId('study-rounds-list').getByText('4 places', { exact: true })).toBeVisible();
  await expect(page.getByTestId('publish-round-1')).toBeVisible();

  const exportLink = page.getByTestId('export-link');
  await expect(exportLink).toHaveAttribute('href', /\/api\/admin\/experiments\/1\/export$/);
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
    question_id: 'long-q',
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

  await expect(documentPopup.getByRole('heading', { name: 'Document for Question long-q' })).toBeVisible();
  await expect(documentPopup.getByText('Document line one')).toBeVisible();
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
    question_id: 'old-q',
    question_text: 'Old experiment question',
    options: 'Yes,No',
    question_type: 'MC',
  };
  state.questionsBySessionToken['token-exp-2'] = {
    id: 502,
    question_id: 'fresh-q',
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

  await expect(page.getByTestId('prolific-mode-badge')).toHaveText('Disabled');
  await expect(page.getByTestId('prolific-mode-notice')).toContainText('Prolific is disabled for this environment');
  await expect(page.getByTestId('prolific-mode-notice')).toContainText('Configure a Prolific API token');
  await expect(page.getByTestId('preview-participant-button')).toBeVisible();
  await expect(page.getByTestId('run-pilot-button')).toHaveCount(0);
});
