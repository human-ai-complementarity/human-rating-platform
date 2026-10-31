import { expect, test, type Page, type Route } from '@playwright/test';

type KeyRecord = {
  id: number;
  name: string;
  masked_key: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  created_by: string | null;
  is_active: boolean;
  scope: 'read' | 'admin';
};

async function fulfillJson(route: Route, status: number, body: unknown) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

/**
 * In-memory mock of the admin api-keys endpoints. Mirrors the backend closely
 * enough to exercise the page: create returns a one-time plaintext, regenerate
 * mints a new plaintext under the same id, revoke flips is_active.
 */
async function installApiKeyMocks(page: Page) {
  const state: {
    keys: KeyRecord[];
    nextId: number;
    nextSecret: number;
    createBodies: Record<string, unknown>[];
  } = {
    keys: [],
    nextId: 1,
    nextSecret: 1,
    createBodies: [],
  };

  await page.context().route('**/api/**', async (route) => {
    const request = route.request();
    const { pathname } = new URL(request.url());
    const method = request.method();

    if (pathname === '/api/admin/auth/logout') {
      await fulfillJson(route, 200, { ok: true });
      return;
    }
    if (pathname === '/api/admin/platform-status') {
      await fulfillJson(route, 200, { prolific_enabled: true, currency_code: null, currency_symbol: null });
      return;
    }

    if (pathname === '/api/admin/api-keys' && method === 'GET') {
      await fulfillJson(route, 200, state.keys);
      return;
    }

    if (pathname === '/api/admin/api-keys' && method === 'POST') {
      const body = JSON.parse(request.postData() || '{}') as {
        name: string;
        scope?: KeyRecord['scope'];
      };
      state.createBodies.push(body);
      const id = state.nextId++;
      const secret = `hrp_secret${state.nextSecret++}`;
      const record: KeyRecord = {
        id,
        name: body.name,
        masked_key: `hrp_abcd1234••••••`,
        created_at: new Date().toISOString(),
        last_used_at: null,
        revoked_at: null,
        created_by: 'dev@local',
        is_active: true,
        scope: body.scope ?? 'read',
      };
      state.keys.unshift(record);
      await fulfillJson(route, 200, { ...record, plaintext_key: secret });
      return;
    }

    const regen = pathname.match(/^\/api\/admin\/api-keys\/(\d+)\/regenerate$/);
    if (regen && method === 'POST') {
      const id = Number(regen[1]);
      const record = state.keys.find((k) => k.id === id)!;
      record.is_active = true;
      record.revoked_at = null;
      const secret = `hrp_secret${state.nextSecret++}`;
      await fulfillJson(route, 200, { ...record, plaintext_key: secret });
      return;
    }

    const revoke = pathname.match(/^\/api\/admin\/api-keys\/(\d+)\/revoke$/);
    if (revoke && method === 'POST') {
      const id = Number(revoke[1]);
      const record = state.keys.find((k) => k.id === id)!;
      record.is_active = false;
      record.revoked_at = new Date().toISOString();
      await fulfillJson(route, 200, record);
      return;
    }

    await fulfillJson(route, 200, {});
  });
  return state;
}

test('create, reveal, regenerate, and revoke an API key', async ({ page }) => {
  const state = await installApiKeyMocks(page);

  await page.goto('/admin/api-keys');
  await expect(page.getByRole('heading', { name: 'API Keys' })).toBeVisible();

  // Create.
  await page.getByTestId('api-key-name-input').fill('inference-pipeline');
  await page.getByTestId('api-key-create-button').click();

  // One-time reveal shows the full secret.
  const reveal = page.getByTestId('api-key-reveal');
  await expect(reveal).toBeVisible();
  await expect(page.getByTestId('api-key-plaintext')).toHaveText('hrp_secret1');

  // The key appears in the list, read-only unless asked otherwise.
  const row = page.getByTestId('api-key-row');
  await expect(row).toHaveCount(1);
  await expect(row).toContainText('inference-pipeline');
  await expect(row).toContainText('Active');
  expect(state.createBodies).toEqual([{ name: 'inference-pipeline', scope: 'read' }]);
  await expect(page.getByTestId('api-key-admin-scope')).toHaveCount(0);

  // Dismiss the reveal.
  await page.getByRole('button', { name: 'Done' }).click();
  await expect(reveal).toHaveCount(0);

  // Regenerate → confirm (scoped to the dialog; the row has a like-named
  // button) → new secret revealed.
  await page.getByTestId('api-key-regenerate').click();
  await page.getByRole('dialog', { name: 'Regenerate key' }).getByRole('button', { name: 'Regenerate' }).click();
  await expect(page.getByTestId('api-key-plaintext')).toHaveText('hrp_secret2');
  await page.getByRole('button', { name: 'Done' }).click();

  // Revoke → confirm → row flips to Revoked and the revoke button disappears.
  await page.getByTestId('api-key-revoke').click();
  await page.getByRole('dialog', { name: 'Revoke key' }).getByRole('button', { name: 'Revoke' }).click();
  await expect(page.getByTestId('api-key-row')).toContainText('Revoked');
  await expect(page.getByTestId('api-key-revoke')).toHaveCount(0);
});

test('an admin key is chosen deliberately, warned about, and badged', async ({ page }) => {
  const state = await installApiKeyMocks(page);

  await page.goto('/admin/api-keys');
  await expect(page.getByTestId('api-key-scope-select')).toHaveValue('read');
  await expect(page.getByText('An admin key can create, change and delete')).toHaveCount(0);

  await page.getByTestId('api-key-scope-select').selectOption('admin');
  await expect(page.getByText('An admin key can create, change and delete')).toBeVisible();

  await page.getByTestId('api-key-name-input').fill('study-cli');
  await page.getByTestId('api-key-create-button').click();
  await expect(page.getByTestId('api-key-plaintext')).toHaveText('hrp_secret1');
  await page.getByRole('button', { name: 'Done' }).click();

  expect(state.createBodies).toEqual([{ name: 'study-cli', scope: 'admin' }]);
  await expect(page.getByTestId('api-key-row')).toContainText('study-cli');
  await expect(page.getByTestId('api-key-admin-scope')).toHaveCount(1);
  // The form goes back to read-only for the next key.
  await expect(page.getByTestId('api-key-scope-select')).toHaveValue('read');
});
