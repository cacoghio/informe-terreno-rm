// Captura determinista del recorrido y codificacion con ffmpeg (Task 12).
//   node tools/capture.mjs <url-del-visor> <salida.mp4> [seconds=45] [fps=12] [ancho=1440] [alto=900]
// Cada fotograma: window.__tour.seek(t) -> captura PNG -> stdin de ffmpeg. Imprime una linea JSON con las medidas.
import { chromium } from '../node_modules/@playwright/test/index.mjs';
import { spawn } from 'node:child_process';

const [url, out, seconds = '45', fps = '12', width = '1440', height = '900'] = process.argv.slice(2);
if (!url || !out) { console.error('uso: capture.mjs <url> <out.mp4> [seconds] [fps] [w] [h]'); process.exit(2); }
const S = +seconds, F = +fps;
const GL = process.env.TERRENO_GL || 'software'; // 'software' (SwiftShader) | 'd3d11' (GPU)
const args = GL === 'd3d11' ? ['--use-angle=d3d11'] : ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'];

const sep = url.includes('?') ? '&' : '?';
const tourUrl = `${url}${sep}tour=1&seconds=${S}`;

const t0 = Date.now();
const browser = await chromium.launch({ args });
const page = await browser.newPage({ viewport: { width: +width, height: +height }, deviceScaleFactor: 1 });
const problems = [];
page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
page.on('console', (m) => { if (m.type() === 'error') problems.push('console: ' + m.text()); });
await page.goto(tourUrl);
await page.waitForFunction(() => window.__tour || document.body.dataset.error, null, { timeout: 90000 });
const err = await page.evaluate(() => document.body.dataset.error || '');
if (err) { console.error('visor: ' + err); await browser.close(); process.exit(3); }
await page.evaluate(() => document.fonts.ready);
const tLoad = Date.now() - t0;

const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(F), '-c:v', 'png', '-i', '-',
  '-c:v', 'libx264', '-preset', 'medium', '-crf', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', out], { stdio: ['pipe', 'inherit', 'inherit'] });
const ffDone = new Promise((res, rej) => { ff.on('close', (c) => (c === 0 ? res() : rej(new Error('ffmpeg salio con ' + c)))); ff.on('error', rej); });

const n = Math.round(S * F);
const t1 = Date.now();
for (let i = 0; i < n; i++) {
  await page.evaluate((t) => window.__tour.seek(t), (i / n) * S);
  const png = await page.screenshot({ type: 'png' });
  if (!ff.stdin.write(png)) await new Promise((r) => ff.stdin.once('drain', r));
  if (process.env.TERRENO_PROGRESS && i % 60 === 59) console.error(`fotograma ${i + 1}/${n}  ${Math.round((Date.now() - t1) / (i + 1))} ms/fotograma (acumulado)`);
}
ff.stdin.end();
await ffDone;
await browser.close();
console.log(JSON.stringify({ out, seconds: S, fps: F, frames: n, gl: GL, load_ms: tLoad, capture_ms: Date.now() - t1, total_ms: Date.now() - t0, problems }));
