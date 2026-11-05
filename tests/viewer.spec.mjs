// Pruebas del visor 3D (Task 11). Se sirven muestras sinteticas de viewer/sample/out (make_sample.py).
//   cd poc/viewer && npx playwright test
import { test, expect } from '../viewer/node_modules/@playwright/test/index.mjs';

const OK = '/index.html?site=sample/out/conclusivo&fecha=2026-06-21&hora=15:30';
const INCONC = '/index.html?site=sample/out/inconclusivo&fecha=2026-06-21&hora=15:30';

async function open(page, url) {
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  page.on('requestfailed', (r) => errors.push('requestfailed: ' + r.url()));
  page.on('response', (r) => { if (r.status() >= 400) errors.push(`http ${r.status()}: ${r.url()}`); });
  await page.goto(url);
  await page.waitForFunction(() => document.body.dataset.ready === 'true' || document.body.dataset.error, null, { timeout: 60_000 });
  return errors;
}

test('test_loads_without_console_errors', async ({ page }) => {
  const errors = await open(page, OK);
  expect(await page.evaluate(() => document.body.dataset.error || '')).toBe('');
  await expect(page.locator('canvas')).toBeVisible();
  // el terreno se dibujo de verdad: la malla tiene geometria y el predio tiene vertices
  const info = await page.evaluate(() => window.__viewer.info());
  expect(info.vertices).toBeGreaterThan(1000);
  expect(info.lotPoints).toBeGreaterThan(3);
  expect(errors, errors.join('\n')).toEqual([]);
});

test('test_exaggeration_label_visible', async ({ page }) => {
  await open(page, OK);
  const label = page.locator('#exag-chip');
  await expect(label).toBeVisible();
  await expect(label).toHaveText(/Exageración ×1,5/);
  // el rotulo sigue al deslizador y es permanente
  await page.locator('#exag').fill('3');
  await expect(label).toHaveText(/Exageración ×3,0/);
  expect(await page.evaluate(() => window.__viewer.state.exaggeration)).toBeCloseTo(3.0, 5);
  const box = await label.boundingBox();
  expect(box.width).toBeGreaterThan(80);
  expect(await page.evaluate(() => window.__viewer.info().meshScaleY)).toBeCloseTo(3.0 / 1.5, 4); // GLB horneado a 1,5
});

test('test_source_panel_shows_level_and_30m', async ({ page }) => {
  await open(page, OK);
  const panel = page.locator('#panel-fuente');
  await expect(panel).toBeVisible();
  await expect(panel.getByRole('heading', { name: 'Fuente y límites' })).toBeVisible();
  await expect(panel).toContainText('Copernicus GLO-30');
  await expect(panel).toContainText(/30 m/);
  await expect(panel.locator('#nivel-letra')).toHaveText('B');
  await expect(panel).toContainText('Nivel de confianza');
  await expect(panel.locator('#n-pixeles')).toHaveText('176');
  await expect(panel).toContainText('Evaluación preliminar basada en datos públicos');
  await expect(panel).toContainText('produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014');
  await expect(panel).toContainText('do not incur any liability for any use of the Copernicus WorldDEM-30');
  // el panel esta fijo: sigue en su sitio tras mover la rueda sobre el lienzo
  const before = await panel.boundingBox();
  await page.mouse.move(500, 400);
  await page.mouse.wheel(0, -400);
  const after = await panel.boundingBox();
  expect(after).toEqual(before);
});

test('test_layer_toggle_changes_state', async ({ page }) => {
  await open(page, OK);
  const pend = page.getByRole('checkbox', { name: /Pendiente por clases/ });
  await expect(pend).toBeEnabled();
  await expect(pend).not.toBeChecked();
  await page.evaluate(() => window.__viewer.renderNow());
  const before = await page.locator('canvas').screenshot();
  await pend.check();
  await expect(pend).toBeChecked();
  expect(await page.evaluate(() => window.__viewer.state.layers.pendiente)).toBe(true);
  await page.evaluate(() => window.__viewer.renderNow());
  const after = await page.locator('canvas').screenshot();
  expect(Buffer.compare(before, after)).not.toBe(0);
  await pend.uncheck();
  expect(await page.evaluate(() => window.__viewer.state.layers.pendiente)).toBe(false);
  // las cuatro capas existen en la muestra conclusiva
  for (const n of ['Orientación', 'Curvas de nivel indicativas', 'Sombreado']) {
    await expect(page.getByRole('checkbox', { name: new RegExp(n) })).toBeEnabled();
  }
});

test('test_url_has_no_personal_data', async ({ page }) => {
  await open(page, OK);
  await page.getByRole('checkbox', { name: /Curvas de nivel indicativas/ }).check();
  await page.locator('#exag').fill('2.5');
  const shared = await page.locator('#share-url').inputValue();
  const u = new URL(shared);
  const allowed = new Set(['site', 'exag', 'capas', 'fecha', 'hora', 'vista']);
  for (const [k, v] of u.searchParams) {
    expect(allowed.has(k), `parametro inesperado ${k}`).toBe(true);
    expect(v).not.toMatch(/@|https?:|\.\.|\\/);
  }
  expect(u.searchParams.get('exag')).toBe('2.5');
  expect(u.searchParams.get('capas')).toBe('curvas');
  // ni coordenadas ni nombres ni rol del predio en la URL
  expect(shared).not.toMatch(/-?\d{2}\.\d{3,}/);
  const pageUrl = new URL(page.url());
  for (const k of pageUrl.searchParams.keys()) expect(allowed.has(k)).toBe(true);
  // el enlace es de solo lectura: abrirlo reproduce el estado
  await page.goto(shared);
  await page.waitForFunction(() => document.body.dataset.ready === 'true');
  expect(await page.evaluate(() => window.__viewer.state.layers.curvas)).toBe(true);
  expect(await page.evaluate(() => window.__viewer.state.exaggeration)).toBeCloseTo(2.5, 5);
});

test('test_site_param_rejects_foreign_origins', async ({ page }) => {
  await page.goto('/index.html?site=https://example.com/x');
  await page.waitForFunction(() => document.body.dataset.error);
  expect(await page.evaluate(() => document.body.dataset.error)).toMatch(/ruta relativa/i);
});

test('test_sun_slider_moves_light', async ({ page }) => {
  await open(page, OK);
  const a = await page.evaluate(() => window.__viewer.sunNow());
  await page.locator('#hora').fill('600'); // 10:00
  const b = await page.evaluate(() => window.__viewer.sunNow());
  // 10:00 y 15:30 son simetricos respecto del mediodia solar: misma altura, azimut distinto (NE vs NO)
  expect(Math.abs(b.azimuthDeg - a.azimuthDeg)).toBeGreaterThan(20);
  await expect(page.locator('#sol-lectura')).toContainText('10:00');
  // 21-jun, -33,4 de latitud: a mediodia solar el sol esta al norte, ~33 grados
  await page.locator('#hora').fill('780');
  const noon = await page.evaluate(() => window.__viewer.sunNow());
  expect(noon.altitudeDeg).toBeGreaterThan(28);
  expect(noon.altitudeDeg).toBeLessThan(36);
  expect(noon.altitudeDeg).toBeGreaterThan(b.altitudeDeg + 5);
  expect(Math.min(noon.azimuthDeg, 360 - noon.azimuthDeg)).toBeLessThan(15); // al norte
});

test('test_inconclusive_site_disables_slope_toggle', async ({ page }) => {
  const errors = await open(page, INCONC);
  const pend = page.getByRole('checkbox', { name: /Pendiente por clases/ });
  await expect(pend).toBeDisabled();
  await expect(page.locator('#capa-pendiente-nota')).toContainText(/no concluyente/i);
  await expect(page.getByRole('checkbox', { name: /Orientación/ })).toBeDisabled(); // capa totalmente transparente
  await expect(page.getByRole('checkbox', { name: /Curvas de nivel indicativas/ })).toBeEnabled();
  await expect(page.locator('#nivel-letra')).toHaveText('C');
  await expect(page.locator('#panel-fuente')).toContainText(/revisión humana obligatoria/i);
  // la unica falla permitida es el 404 esperado de la capa de pendiente ausente
  expect(errors.filter((e) => !/pendiente\.png|status of 404/.test(e)), errors.join('\n')).toEqual([]);
});
