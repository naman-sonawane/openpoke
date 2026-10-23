import { defineConfig } from '@playwright/test';
import { readFileSync } from 'fs';
import { join } from 'path';

// The console may sit behind DEMO_PASSWORD (web/.env.local); log in with it.
function demoPassword(): string | undefined {
  if (process.env.DEMO_PASSWORD) return process.env.DEMO_PASSWORD;
  try {
    const env = readFileSync(join(__dirname, '.env.local'), 'utf8');
    return env.match(/^DEMO_PASSWORD=(.+)$/m)?.[1]?.trim();
  } catch {
    return undefined;
  }
}

const password = demoPassword();

// A visible (--headed) run is for watching: slow each action down so it can be
// followed. Set before workers start, so they inherit it.
if (process.argv.includes('--headed') && !process.env.E2E_SLOWMO) process.env.E2E_SLOWMO = '600';

// UI acceptance tests. They run against the already-running stack
// (python -m server.server and npm run dev); start both first.
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 30_000 },
  fullyParallel: true,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://localhost:3000',
    httpCredentials: password ? { username: 'demo', password } : undefined,
    launchOptions: { slowMo: Number(process.env.E2E_SLOWMO || 0) },
    trace: 'on',
  },
  metadata: { passwordProtected: Boolean(password) },
});
