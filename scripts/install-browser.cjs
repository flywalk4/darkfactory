// Pinned Playwright 1.64.1: install Chromium and its headless shell (no video).
// chromium.launch() in headless mode, the default for tests and scripts, runs the headless shell: without it every
// browser check failed with "Executable doesn't exist at …/chromium_headless_shell-…".
const fs = require('node:fs');
const test = require('@playwright/test');
const { registry } = require(require.resolve('playwright-core/lib/coreBundle', {
  paths: [require.resolve('@playwright/test')],
})).registry;

async function main() {
  await registry.install([registry.findExecutable('chromium'), registry.findExecutable('chromium-headless-shell')]);
  fs.accessSync(test.chromium.executablePath(), fs.constants.X_OK);
  fs.symlinkSync(test.chromium.executablePath(), '/usr/local/bin/darkfactory-chromium');
  // The image is only usable if the default launch works: headless shell plus system libraries.
  const browser = await test.chromium.launch();
  await browser.close();
}
main().catch(error => { console.error(error); process.exit(1); });
