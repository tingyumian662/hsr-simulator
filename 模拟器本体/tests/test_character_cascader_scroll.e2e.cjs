const assert = require('node:assert/strict');
const test = require('node:test');

const playwrightModule = process.env.HSR_PLAYWRIGHT_PATH || 'playwright';
const { chromium } = require(playwrightModule);
const appUrl = process.env.HSR_TEST_URL || 'http://127.0.0.1:8000';

async function scrollMetrics(locator) {
  return locator.evaluate(element => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
    scrollTop: element.scrollTop,
  }));
}

async function assertLastOptionVisible(list, option) {
  const listBox = await list.boundingBox();
  const optionBox = await option.boundingBox();
  assert.ok(optionBox.y >= listBox.y);
  assert.ok(optionBox.y + optionBox.height <= listBox.y + listBox.height);
}

test('mouse wheel reaches the last path and character options', async () => {
  const launchOptions = { headless: true };
  if(process.env.HSR_EDGE_PATH) launchOptions.executablePath = process.env.HSR_EDGE_PATH;
  const browser = await chromium.launch(launchOptions);
  try {
    const page = await browser.newPage({ viewport: { width: 516, height: 616 } });
    await page.goto(appUrl, { waitUntil: 'networkidle' });
    await page.locator('#char-trigger0').click();

    const paths = page.locator('#char-paths0');
    const pathBefore = await scrollMetrics(paths);
    assert.ok(pathBefore.scrollHeight > pathBefore.clientHeight);
    await paths.hover();
    await page.mouse.wheel(0, 1000);
    await page.waitForTimeout(50);
    const pathAfter = await scrollMetrics(paths);
    assert.ok(pathAfter.scrollTop > 0);
    await assertLastOptionVisible(paths, paths.locator('.char-path-option').last());

    await page.locator('#char-paths0 [data-path="虚无"]').click();
    const characters = page.locator('#char-options0');
    const characterBefore = await scrollMetrics(characters);
    assert.ok(characterBefore.scrollHeight > characterBefore.clientHeight);
    await characters.hover();
    await page.mouse.wheel(0, 1600);
    await page.waitForTimeout(50);
    const characterAfter = await scrollMetrics(characters);
    assert.ok(characterAfter.scrollTop > 0);
    await assertLastOptionVisible(characters, characters.locator('.char-option').last());
  } finally {
    await browser.close();
  }
});
