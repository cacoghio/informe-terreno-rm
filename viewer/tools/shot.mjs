// Captura de pantalla de revision visual:  node tools/shot.mjs "<query>" salida.png [ancho] [alto] [--gpu]
import { chromium } from '../node_modules/@playwright/test/index.mjs';

const [query, out, w = '1440', h = '900', ...flags] = process.argv.slice(2);
const args = flags.includes('--gpu') ? ['--use-angle=d3d11'] : ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'];
const browser = await chromium.launch({ args });
const page = await browser.newPage({ viewport: { width: +w, height: +h } });
const logs = [];
page.on('console', (m) => logs.push(`[${m.type()}] ${m.text()}`));
page.on('pageerror', (e) => logs.push('[pageerror] ' + e.message));
const t0 = Date.now();
await page.goto(`http://127.0.0.1:8765/index.html?${query}`);
await page.waitForFunction(() => document.body.dataset.ready === 'true' || document.body.dataset.error, null, { timeout: 90000 });
const t1 = Date.now();
await page.evaluate(() => window.__viewer?.renderNow());
await page.waitForTimeout(500);
await page.screenshot({ path: out });
console.log(`listo en ${t1 - t0} ms; captura ${Date.now() - t1} ms`);
console.log(logs.join('\n') || '(sin mensajes de consola)');
console.log('error:', await page.evaluate(() => document.body.dataset.error || '(ninguno)'));
await browser.close();
