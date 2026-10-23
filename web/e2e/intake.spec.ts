import { expect, test, type Page } from '@playwright/test';

// UI acceptance: the console as a person uses it. The conversation logic has
// its own tests (pytest, the scenario runner); these check that the page
// wires it up: calls start, turns land, the staff view reflects the call, and
// tabs don't share a call.

async function startCall(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: 'New call' }).click();
  await expect(page.getByText(/automated AI assistant/)).toBeVisible();
}

async function say(page: Page, text: string) {
  const input = page.getByPlaceholder(/as the caller/);
  await input.fill(text);
  await input.press('Enter');
}

const staffView = (page: Page) => page.locator('aside');

test('the greeting discloses AI and transcription, and the call starts identifying', async ({ page }) => {
  await startCall(page);
  await expect(page.getByText(/this call is transcribed/)).toBeVisible();
  await expect(staffView(page).getByText('identify', { exact: true })).toBeVisible();
});

test('a clear emergency gets the 911 script and stops booking', async ({ page }) => {
  await startCall(page);
  await say(page, 'I have crushing chest pain right now');
  await expect(page.getByText(/call 911/)).toBeVisible();
  await expect(staffView(page).getByText('emergency', { exact: true })).toBeVisible();
  await expect(staffView(page).getByText(/Booking stopped; 911 advised/)).toBeVisible();
});

test('the agent identifies a caller and the staff view shows them', async ({ page }) => {
  await startCall(page);
  await say(page, "Hi, I'm Maria Garcia, born March 14 1985");
  await expect(staffView(page).getByText('Maria Garcia', { exact: true })).toBeVisible();
  await expect(staffView(page).getByText('1985-03-14', { exact: true })).toBeVisible();
});

test('two tabs are two separate calls', async ({ browser }) => {
  const context = await browser.newContext();
  const first = await context.newPage();
  const second = await context.newPage();
  await startCall(first);
  await startCall(second);

  await say(first, 'I have crushing chest pain right now');
  await expect(staffView(first).getByText('emergency', { exact: true })).toBeVisible();

  // The other call is untouched: still identifying, no emergency, and none
  // of the first caller's words in its transcript.
  await expect(staffView(second).getByText('identify', { exact: true })).toBeVisible();
  await expect(second.getByText(/crushing chest pain/)).toHaveCount(0);
  await context.close();
});

test('demo switches are stored on the call', async ({ page }) => {
  await startCall(page);
  const conflict = page.getByLabel('Slot taken at booking');
  await conflict.check(); // flips immediately, before the server answers
  // Still checked after the 1.5s poll, so the server stored it on this call.
  await page.waitForTimeout(2000);
  await expect(conflict).toBeChecked();
});

test('typing before a call has started asks for a new call', async ({ page }) => {
  await page.goto('/');
  await say(page, 'hello?');
  await expect(page.getByText('Press New call first.')).toBeVisible();
});

test('the console is password protected when a demo password is set', async ({ browser, baseURL }, testInfo) => {
  test.skip(!testInfo.config.metadata.passwordProtected, 'no DEMO_PASSWORD configured');
  const anonymous = await browser.newContext({ httpCredentials: undefined });
  const response = await anonymous.request.get(baseURL!);
  expect(response.status()).toBe(401);
  await anonymous.close();
});
