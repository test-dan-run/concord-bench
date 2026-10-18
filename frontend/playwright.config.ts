import { defineConfig, devices } from "@playwright/test";

// Smoke test per screen against a replayed fixture set (tests/e2e_server.py). Needs `npm run build`
// first (the API serves frontend/dist). Run: `npm run build && npx playwright test`.
// With E2E_BASE_URL set, an already running server is used (e.g. browsers in the Playwright image:
// `just e2e` starts tests/e2e_server.py on the host and runs Playwright in mcr.microsoft.com/playwright).
const PORT = 8611;
const external = process.env.E2E_BASE_URL;

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: external ?? `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    launchOptions: { args: ["--autoplay-policy=no-user-gesture-required"] },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } } }],
  webServer: external ? undefined : {
    command: `cd .. && uv run python tests/e2e_server.py --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}/api/config`,
    timeout: 240_000,
    reuseExistingServer: false,
    stdout: "pipe",
  },
});
