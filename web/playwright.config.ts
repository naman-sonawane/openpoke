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

// UI acceptance tests. They run against the already-running stack
// (python -m server.server and npm run dev); start both first.
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 30_000 },
  fullyParallel: true,
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://localhost:3000',
    httpCredentials: password ? { username: 'demo', password } : undefined,
  },
  metadata: { passwordProtected: Boolean(password) },
});
