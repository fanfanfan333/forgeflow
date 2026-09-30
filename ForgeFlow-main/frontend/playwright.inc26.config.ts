import { defineConfig, devices } from '@playwright/test'

// QA overlay for INC26 T06. Not product code: identical to playwright.config.ts
// except it pins `channel: 'chromium'` so the run uses the FULL bundled Chromium
// (chromium-1228/chrome-win64) in new-headless mode. The default headless path
// wants `chrome-headless-shell`, which is not present on this offline box, and
// downloading it is network-blocked. No behavioural difference to the SUT.
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 4,
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:4173',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'], channel: 'chromium' } },
  ],
  webServer: {
    command: 'npm run build && npm run preview -- --port 4173 --strictPort',
    url: 'http://localhost:4173',
    timeout: 120_000,
    reuseExistingServer: true,
  },
})
