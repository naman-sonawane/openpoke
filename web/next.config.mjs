import envPackage from '@next/env';
import { dirname, resolve } from 'path';
import { fileURLToPath } from 'url';

// Ensure the Next.js runtime loads environment variables declared in the repo root .env
const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..');
const isDevelopment = process.env.NODE_ENV !== 'production';

const { loadEnvConfig } = envPackage;
loadEnvConfig?.(repoRoot, isDevelopment);

/** @type {import('next').NextConfig} */
const nextConfig = {
  // Middleware runs in the edge runtime, which only sees env passed through
  // here. Referenced only by middleware.ts, so it never reaches the browser.
  env: { DEMO_PASSWORD: process.env.DEMO_PASSWORD || '' },
};

export default nextConfig;
