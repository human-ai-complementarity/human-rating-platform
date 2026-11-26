import { expect, test } from '@playwright/test';

// The e2e auth bypass (frontend/src/auth.tsx) scripts Clerk's sign-in and
// sign-up responses by address. These are the addresses it recognises; any
// other address signs in as a wrong password.
const UNKNOWN = 'unknown@example.com';
const GOOGLE_ONLY = 'google-only@example.com';
const EXISTING = 'existing@example.com';
const PASSWORD = 'correct-horse-battery';

test.beforeEach(async ({ page }) => {
  await page.goto('/e2e/login');
  await expect(page.getByRole('heading', { name: 'Human Rating Platform' })).toBeVisible();
});

async function submitSignIn(page: import('@playwright/test').Page, email: string) {
  await page.getByPlaceholder('Email').fill(email);
  await page.getByPlaceholder('Password').fill(PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
}

test('signing up with an existing email moves to sign-in with the email kept', async ({ page }) => {
  await page.getByRole('button', { name: 'Create an Account' }).click();
  await expect(page.getByRole('heading', { name: 'Create an account' })).toBeVisible();
  await page.getByPlaceholder('Email').fill(EXISTING);
  await page.getByPlaceholder('Password').fill(PASSWORD);
  await page.getByRole('button', { name: 'Sign up' }).click();

  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  await expect(page.getByPlaceholder('Email')).toHaveValue(EXISTING);
  await expect(page.getByPlaceholder('Password')).toHaveValue('');
  await expect(page.getByText('An account with this email already exists')).toBeVisible();
  // The message offers Google, so the button must be on this screen, not a Back click away.
  await expect(page.getByRole('button', { name: 'Continue with Google' })).toBeVisible();
});

test('signing in tells an unknown email, a Google-only account, and a wrong password apart', async ({
  page,
}) => {
  await page.getByRole('button', { name: 'Sign in with Email' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();

  await submitSignIn(page, UNKNOWN);
  await expect(page.getByText('No account with this email yet')).toBeVisible();

  await submitSignIn(page, GOOGLE_ONLY);
  await expect(page.getByText('This account signs in with Google')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Continue with Google' })).toBeVisible();

  await submitSignIn(page, 'someone@example.com');
  await expect(page.getByText('Invalid email or password.')).toBeVisible();
});
