import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";
import path from "node:path";

// Starts its own MOCK_ONLY backend (port 8012, separate embedded Postgres) and the production build under `vite preview`
// (port 4174, proxying /v1 to that backend), so it never touches a developer's running stack.
const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../..");
const API_PORT = Number(process.env.E2E_API_PORT ?? 8012);
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 4174);

export default defineConfig({
  testDir: path.join(repo, "tests/e2e"),
  outputDir: path.join(here, "test-results"),
  fullyParallel: true,
  // One worker by default: the suite shares one backend, and the development laptop throttles under sustained load.
  workers: Number(process.env.E2E_WORKERS ?? 1),
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"], ["html", { outputFolder: path.join(here, "playwright-report"), open: "never" }]],
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
  },
  webServer: [
    {
      command: `uv run uvicorn delegate.app:create_app --factory --app-dir services/api --host 127.0.0.1 --port ${API_PORT}`,
      cwd: repo,
      url: `http://127.0.0.1:${API_PORT}/readyz`,
      timeout: 180_000,
      reuseExistingServer: !process.env.CI,
      env: {
        DELEGATE_PROVIDER_MODE: "mock",
        DELEGATE_PGDATA: process.env.E2E_PGDATA ?? "/tmp/delegate-e2e-pg",
        DELEGATE_SESSIONS_PER_IP_PER_HOUR: "100000",
        DELEGATE_WEB_DIST: "/nonexistent-delegate-e2e",
      },
    },
    {
      command: `npm run build && npx vite preview --host 127.0.0.1 --port ${WEB_PORT} --strictPort`,
      cwd: here,
      url: `http://127.0.0.1:${WEB_PORT}`,
      timeout: 120_000,
      reuseExistingServer: !process.env.CI,
      env: { DELEGATE_API_URL: `http://127.0.0.1:${API_PORT}` },
    },
  ],
});
