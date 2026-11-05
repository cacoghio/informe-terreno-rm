// Spike Task 12 (paso 1): ¿renderiza WebGL el Chromium headless en esta maquina y a que ritmo se capturan fotogramas?
//   node tools/spike_webgl.mjs [default|swiftshader|d3d11] [frames]
import { chromium } from '../node_modules/@playwright/test/index.mjs';

const variants = {
  default: [],
  swiftshader: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
  d3d11: ['--use-angle=d3d11'],
};
const name = process.argv[2] || 'default';
const frames = +(process.argv[3] || 30);
const browser = await chromium.launch({ args: variants[name] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const t0 = Date.now();
await page.goto('http://127.0.0.1:8765/index.html?site=sample/out/conclusivo&fecha=2026-12-21&hora=08:00');
await page.waitForFunction(() => document.body.dataset.ready === 'true');
const renderer = await page.evaluate(() => {
  const gl = document.querySelector('canvas').getContext('webgl2');
  const e = gl.getExtension('WEBGL_debug_renderer_info');
  return e ? gl.getParameter(e.UNMASKED_RENDERER_WEBGL) : 'n/d';
});
const tLoad = Date.now() - t0;
const t1 = Date.now();
let bytes = 0;
for (let i = 0; i < frames; i++) {
  await page.evaluate((k) => {
    const a = window.__viewer.api;
    a.setSun(null, 480 + k * 10);
    a.setExaggeration(1.5 + (k % 6) * 0.5);
    a.setCameraSpherical({ target: new a.THREE.Vector3(0, a.groundY(), 0), bearingDeg: 200 + k * 2, elevationDeg: 30, dist: a.R * 3 });
    a.renderNow();
  }, i);
  bytes += (await page.screenshot({ type: 'png' })).length;
}
const per = (Date.now() - t1) / frames;
console.log(JSON.stringify({ variant: name, renderer, load_ms: tLoad, frames, ms_per_frame: Math.round(per), kb_per_frame: Math.round(bytes / frames / 1024) }));
await browser.close();
