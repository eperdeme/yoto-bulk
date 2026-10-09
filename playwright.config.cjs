const { defineConfig } = require("@playwright/test");

const port = process.env.UI_TEST_PORT || "8012";
const baseURL = `http://127.0.0.1:${port}`;

module.exports = defineConfig({
  testDir: "./tests/ui",
  timeout: 15000,
  expect: { timeout: 5000 },
  workers: 1,
  use: { baseURL, trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [
    { name: "desktop", use: { viewport: { width: 1280, height: 900 } } },
    { name: "mobile", use: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
  webServer: {
    command: "node tests/ui/server.cjs",
    url: baseURL,
    reuseExistingServer: false,
    env: { UI_TEST_PORT: port },
    timeout: 15000,
  },
});