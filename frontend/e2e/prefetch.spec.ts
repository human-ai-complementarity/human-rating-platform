import { expect, test, type Page, type Route } from '@playwright/test';

const url = '/rate?experiment_id=1&PROLIFIC_PID=pid&STUDY_ID=study&SESSION_ID=session';
const question = (id: number) => ({ id, question_text: `Question ${id}`, question_type: 'MC', options: 'Yes|No', is_markdown: false, assignment_id: id, assignment_generation: 1 });
const item = (id: number, activated = false) => ({ assignment_id: id, generation: 1, activated, question: question(id) });

async function mockQueue(page: Page, method = 'top_n', intro = false) {
  const state = {
    revision: 1, items: [item(1), item(2)], starts: [] as number[], prepares: [] as number[],
    submissions: [] as Record<string, unknown>[], actions: [] as string[],
    activationGate: null as Promise<void> | null, loseSubmit: false,
    replaceOnActivate: false, skipInitial: false,
    startFailures: [] as number[], sessionEndTime: '2099-01-01T00:00:00Z', graceSeconds: 300,
    assistanceGate: null as Promise<void> | null,
    conflictActivation: false, advanced: false, loseAdvance: false, advanceCalls: 0,
    advanceBodies: [] as Record<string, unknown>[], turnPending: false, conflictSubmit: false,
  };
  const json = (route: Route, value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/raters/start')) return json(route, {
      rater_id: 1, session_start: '2026-09-29T00:00:00Z', session_end_time: state.sessionEndTime, session_grace_seconds: state.graceSeconds,
      experiment_name: 'Prefetch study', experiment_description_html: intro ? '<p>Read these study instructions.</p>' : null,
      assistance_method: method, assistance_instructions: null, queue_enabled: false, rater_session_token: 'token', completion_url: null,
    });
    if (path.endsWith('/raters/session-status')) return json(route, { is_active: true, questions_completed: state.submissions.length, time_remaining_seconds: 3600, grace_seconds_remaining: 3900 });
    if (path.endsWith('/raters/next-question')) {
      state.actions.push('next-question');
      if (state.activationGate) await state.activationGate;
      if (state.conflictActivation) { state.conflictActivation = false; state.items.shift(); }
      if (state.replaceOnActivate) { state.items[0] = item(3); state.replaceOnActivate = false; }
      if (!state.items.length) return json(route, null);
      state.items[0].activated = true;
      return json(route, state.items[0].question);
    }
    if (path.endsWith('/raters/queue') && route.request().postDataJSON().action === 'skip') {
      const body = route.request().postDataJSON();
      state.actions.push('skip');
      state.items = state.items.filter(item => item.assignment_id !== body.assignment_id);
      return json(route, {});
    }
    if (path.endsWith('/raters/queue') || path.endsWith('/assistance/prepare')) {
      state.prepares.push(1);
      return json(route, { detail: 'Browser must not schedule work' }, 400);
    }
    if (path.endsWith('/assistance/start')) {
      const id = route.request().postDataJSON().question_id;
      state.starts.push(id);
      if (state.skipInitial && id === 1) return json(route, { session_id: id, turn: 1, type: 'skip', is_terminal: true, payload: {} });
      const failure = state.startFailures.shift();
      if (failure === -1) return route.abort('failed');
      if (failure) return json(route, { detail: 'Assistance is still preparing; retry the same question' }, failure);
      if (state.assistanceGate) await state.assistanceGate;
      if (state.advanced) return json(route, { session_id: id, turn: 2, type: 'complete', is_terminal: true, payload: { synthesis: { answer: 'Yes' } } });
      expect(state.items.find(value => value.assignment_id === id)?.activated).toBe(true);
      return json(route, { session_id: id, turn: 1, type: method === 'top_n' ? 'display' : 'ask_input', is_terminal: method === 'top_n',
        payload: method === 'top_n' ? { kind: 'top_n', candidates: [{ answer: 'Yes', rationale: 'Prepared guidance', rank: 1 }] }
          : { iteration: 1, max_rounds: 2, confidence_threshold: 75, subtasks: [{ index: 1, question: 'Check the evidence', type: 'free_text', confidence: 20 }] },
      });
    }
    if (path.endsWith('/assistance/advance')) {
      state.advanceBodies.push(route.request().postDataJSON());
      if (state.turnPending) return json(route, { detail: 'Assistance is still processing this turn. Retry shortly.' }, 409);
      if (state.advanced) return json(route, { session_id: 1, turn: 2, type: 'complete', is_terminal: true, payload: { synthesis: { answer: 'Yes' } } });
      state.advanceCalls += 1;
      state.advanced = true;
      if (state.loseAdvance) { state.loseAdvance = false; return route.abort('failed'); }
      return json(route, { session_id: 1, turn: 2, type: 'complete', is_terminal: true, payload: { synthesis: { answer: 'Yes' } } });
    }
    if (path.endsWith('/raters/submit')) {
      const body = route.request().postDataJSON();
      state.submissions.push(body);
      if (state.conflictSubmit) {
        state.items = state.items.filter(item => item.assignment_id !== body.assignment_id);
        state.revision += 1;
        return json(route, { detail: 'A different answer was already submitted' }, 409);
      }
      if (state.items[0]?.assignment_id === body.assignment_id) {
        state.items.shift();
        state.revision += 1;
      }
      if (state.loseSubmit) { state.loseSubmit = false; return route.abort('failed'); }
      return json(route, { id: body.assignment_id, success: true });
    }
    return json(route, {});
  });
  return state;
}

test('one next-question request precedes display without browser scheduling', async ({ page }) => {
  const state = await mockQueue(page);
  let release!: () => void;
  state.activationGate = new Promise<void>(resolve => { release = resolve; });
  await page.goto(url);
  await expect.poll(() => state.actions).toContain('next-question');
  await expect(page.getByText('Question 1', { exact: true })).toHaveCount(0);
  expect(state.starts).toEqual([]);
  release();
  await expect(page.getByText('Prepared guidance')).toBeVisible();
  expect(state.prepares).toEqual([]);
  await expect(page.getByText('Question 2', { exact: true })).toHaveCount(0);
  expect(state.starts).toEqual([1]);
  await page.getByRole('button', { name: 'Yes', exact: true }).click();
  await page.getByRole('button', { name: /submit/i }).click();
  await expect(page.getByText('Question 2', { exact: true })).toBeVisible();
  await expect.poll(() => state.starts).toEqual([1, 2]);
});

test('lost submission response retries the exact answer before advancing', async ({ page }) => {
  const state = await mockQueue(page);
  state.loseSubmit = true;
  await page.goto(url);
  await expect(page.getByText('Prepared guidance')).toBeVisible();
  await page.getByRole('button', { name: 'Yes', exact: true }).click();
  await page.getByRole('button', { name: /submit/i }).click();
  await expect(page.getByRole('button', { name: 'Retry saving answer' })).toBeVisible();
  await expect(page.getByText('Question 1', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'No', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Retry saving answer' }).click();
  await expect(page.getByText('Question 2', { exact: true })).toBeVisible();
  expect(state.submissions).toHaveLength(2);
  expect(state.submissions[0]).toEqual(state.submissions[1]);
});

test('Human-as-a-Tool still waits for real input', async ({ page }) => {
  const state = await mockQueue(page, 'human_as_a_tool');
  await page.goto(url);
  await expect(page.getByText('Check the evidence')).toBeVisible();
  expect(state.prepares).toEqual([]);
  await page.getByPlaceholder('Your answer...').fill('Evidence checked');
  await page.getByRole('button', { name: /send|continue|submit/i }).last().click();
  await expect(page.getByText('Analysis complete')).toBeVisible();
  expect(state.starts).toEqual([1]);
});

test('no preparation or reservation runs before intro acknowledgment', async ({ page }) => {
  const state = await mockQueue(page, 'top_n', true);
  await page.goto(url);
  await expect(page.getByText('Read these study instructions.')).toBeVisible();
  expect(state.actions).toEqual([]);
  expect(state.prepares).toEqual([]);
  await page.reload();
  await expect(page.getByText('Read these study instructions.')).toBeVisible();
  expect(state.actions).toEqual([]);
  await page.getByRole('button', { name: /begin|start|continue/i }).click();
  await expect(page.getByText('Prepared guidance')).toBeVisible();
  expect(state.prepares).toEqual([]);
});


test('the browser displays the server-selected question after another tab advances', async ({ page }) => {
  const state = await mockQueue(page);
  state.conflictActivation = true;
  await page.goto(url);
  await expect(page.getByText('Question 2', { exact: true })).toBeVisible();
  await expect(page.getByText('Question 1', { exact: true })).toHaveCount(0);
  expect(state.starts).toEqual([2]);
});

test('an uncertain assistance advance reconciles the saved step without advancing twice', async ({ page }) => {
  const state = await mockQueue(page, 'human_as_a_tool');
  state.loseAdvance = true;
  await page.goto(url);
  await page.getByPlaceholder('Your answer...').fill('Evidence checked');
  await page.getByRole('button', { name: 'Submit answers', exact: true }).click();
  await page.getByRole('button', { name: 'Retry assistance' }).click();
  await expect(page.getByText('Analysis complete')).toBeVisible();
  expect(state.advanceCalls).toBe(1);
  expect(state.starts).toEqual([1]);
  expect(state.advanceBodies).toHaveLength(2);
  expect(state.advanceBodies[0]).toEqual(state.advanceBodies[1]);
});


test('a pending advance keeps retrying the same revision and frozen answers', async ({ page }) => {
  const state = await mockQueue(page, 'human_as_a_tool');
  state.turnPending = true;
  await page.goto(url);
  await page.getByPlaceholder('Your answer...').fill('Evidence checked');
  await page.getByRole('button', { name: 'Submit answers', exact: true }).click();
  await page.getByRole('button', { name: 'Retry assistance' }).click();
  await expect(page.getByText('Assistance is still processing this turn. Retry shortly.')).toBeVisible();
  expect(state.advanceCalls).toBe(0);
  state.turnPending = false;
  state.advanced = true;
  await page.getByRole('button', { name: 'Retry assistance' }).click();
  await expect(page.getByText('Analysis complete')).toBeVisible();
  expect(state.advanceBodies).toHaveLength(3);
  expect(state.advanceBodies.every(body => JSON.stringify(body) === JSON.stringify(state.advanceBodies[0]))).toBe(true);
  expect(state.advanceBodies[0].turn).toBe(1);
});

test('submission conflict offers explicit recovery to the authoritative question', async ({ page }) => {
  const state = await mockQueue(page);
  state.conflictSubmit = true;
  await page.goto(url);
  await expect(page.getByText('Prepared guidance')).toBeVisible();
  await page.getByRole('button', { name: 'Yes', exact: true }).click();
  await page.getByRole('button', { name: /submit/i }).click();
  await expect(page.getByText('Question 1', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Continue from saved progress' }).click();
  await expect(page.getByText('Question 2', { exact: true })).toBeVisible();
  expect(state.submissions).toHaveLength(1);
});


test('early arrival waits for assistance without scheduling successors', async ({ page }) => {
  const state = await mockQueue(page);
  state.items = [item(1), item(2), item(3), item(4)];
  await page.goto(url);
  await expect(page.getByText('Prepared guidance')).toBeVisible();
  expect(state.prepares).toEqual([]);
  let release!: () => void;
  state.assistanceGate = new Promise<void>(resolve => { release = resolve; });
  await page.getByRole('button', { name: 'Yes', exact: true }).click();
  await page.getByRole('button', { name: /submit/i }).click();
  await expect(page.getByText('Question 2', { exact: true })).toBeVisible();
  await expect(page.getByText('Preparing guidance, please wait…')).toBeVisible();
  await expect(page.getByText('Prepared guidance')).toHaveCount(0);
  expect(state.starts).toEqual([1, 2]);
  expect(state.prepares).toEqual([]);
  release();
  await expect(page.getByText('Prepared guidance')).toBeVisible();
  expect(state.starts).toEqual([1, 2]);
  expect(state.prepares).toEqual([]);
});


test('serving-time replacement displays and demands only the returned question', async ({ page }) => {
  const state = await mockQueue(page);
  await page.goto(url);
  await expect(page.getByText('Prepared guidance')).toBeVisible();
  expect(state.prepares).toEqual([]);
  state.replaceOnActivate = true;
  await page.getByRole('button', { name: 'Yes', exact: true }).click();
  await page.getByRole('button', { name: /submit/i }).click();
  await expect(page.getByText('Question 3', { exact: true })).toBeVisible();
  await expect(page.getByText('Question 2', { exact: true })).toHaveCount(0);
  await expect.poll(() => state.starts).toEqual([1, 3]);
});

test('transient start failures retry the same question without leaving loading', async ({ page }) => {
  await page.clock.install();
  const state = await mockQueue(page);
  state.startFailures = [503, -1, 502, 504];
  await page.goto(url);
  await expect.poll(() => state.starts.length).toBe(1);
  for (const [index, delay] of [1000, 2000, 4000, 5000].entries()) {
    await expect(page.getByText('Preparing guidance, please wait…')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Retry assistance' })).toHaveCount(0);
    await page.clock.runFor(delay);
    await expect.poll(() => state.starts.length).toBe(index + 2);
  }
  await expect(page.getByText('Prepared guidance')).toBeVisible();
  expect(state.starts).toEqual([1, 1, 1, 1, 1]);
  expect(state.prepares).toEqual([]);
});

test('automatic start retries stop after six requests and allow explicit retry', async ({ page }) => {
  await page.clock.install();
  const state = await mockQueue(page);
  state.startFailures = Array(6).fill(503);
  await page.goto(url);
  await expect.poll(() => state.starts.length).toBe(1);
  for (const [index, delay] of [1000, 2000, 4000, 5000, 5000].entries()) {
    await page.clock.runFor(delay);
    await expect.poll(() => state.starts.length).toBe(index + 2);
  }
  await expect(page.getByRole('button', { name: 'Retry assistance' })).toBeVisible();
  await page.clock.runFor(30000);
  expect(state.starts).toHaveLength(6);
  await page.getByRole('button', { name: 'Retry assistance' }).click();
  await expect(page.getByText('Prepared guidance')).toBeVisible();
  expect(state.starts).toEqual(Array(7).fill(1));
});

test('assignment conflicts do not automatically retry assistance', async ({ page }) => {
  await page.clock.install();
  const state = await mockQueue(page);
  state.startFailures = [409];
  await page.goto(url);
  await expect(page.getByRole('button', { name: 'Retry assistance' })).toBeVisible();
  await page.clock.runFor(30000);
  expect(state.starts).toEqual([1]);
});

test('session expiry cancels scheduled assistance retries', async ({ page }) => {
  await page.clock.install();
  const state = await mockQueue(page);
  state.sessionEndTime = new Date(Date.now() + 1000).toISOString();
  state.graceSeconds = 0;
  state.startFailures = Array(6).fill(503);
  await page.goto(url);
  await expect.poll(() => state.starts.length).toBe(1);
  await page.clock.runFor(1500);
  await expect(page.getByText('Your session has ended.')).toBeVisible();
  const requestsAtExpiry = state.starts.length;
  await page.clock.runFor(30000);
  expect(state.starts).toHaveLength(requestsAtExpiry);
  await expect(page.getByRole('button', { name: 'Retry assistance' })).toHaveCount(0);
});


test('failed-step skip uses the served assignment even for a restored legacy session', async ({ page }) => {
  const state = await mockQueue(page);
  state.skipInitial = true;
  await page.goto(url);
  await expect(page.getByText('Question 2', { exact: true })).toBeVisible();
  await expect(page.getByText('Prepared guidance')).toBeVisible();
  expect(state.actions).toEqual(['next-question', 'skip', 'next-question']);
  expect(state.prepares).toEqual([]);
});
