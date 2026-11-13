import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.PLAYWRIGHT_TEST_BASE_URL || "http://localhost:5173";

// E2E tests create and publish data, so they only run against a local dev stack.
const host = new URL(baseURL).hostname;
const isLocal = host === "localhost" || host === "127.0.0.1" || host.endsWith(".localhost");
if (!isLocal || process.env.NODE_ENV === "production") {
  throw new Error(
    `E2E tests only run in development against a local stack (got ${baseURL}, NODE_ENV=${process.env.NODE_ENV ?? "unset"}).`
  );
}

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: "list",
  use: {
    baseURL,
    trace: "retain-on-failure",
    headless: true,
    viewport: { width: 1280, height: 800 },
  },
  // The dev server is started here. The backend must already be running on port 8000.
  webServer: {
    command: "npm run dev",
    url: baseURL,
    reuseExistingServer: true,
    timeout: 60_000,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
