import { defineConfig } from './node_modules/@playwright/test/index.mjs';

// El visor se sirve con un servidor estatico (file:// esta bloqueado). Raiz = poc/viewer.
export default defineConfig({
  testDir: '../tests',
  testMatch: ['viewer.spec.mjs'],
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  outputDir: './test-results',
  use: {
    baseURL: 'http://127.0.0.1:8765',
    viewport: { width: 1440, height: 900 },
    launchOptions: { args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] },
  },
  webServer: {
    command: 'python -m http.server 8765 --bind 127.0.0.1',
    cwd: '.',
    url: 'http://127.0.0.1:8765/index.html',
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
