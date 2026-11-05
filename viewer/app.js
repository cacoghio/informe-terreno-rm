// Visor 3D de terreno (Task 11). Estatico: three.js + SunCalc vendorizados en ./vendor, sin bundler ni CDN.
// Entrada (contrato con build_glb): terreno.glb, predio.geojson, layers/*.png, manifest.json dentro de ?site=<ruta relativa>.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { sunAt, sunPath, localToDate, fmtTime, parseTime, compassName } from './sun.js';
import * as T from './texts.js';

const LAYERS = ['pendiente', 'orientacion', 'curvas', 'sombreado'];
const LAYER_ORDER = ['sombreado', 'orientacion', 'pendiente', 'curvas']; // de abajo hacia arriba
const SHARE_KEYS = ['site', 'exag', 'capas', 'fecha', 'hora', 'vista'];
const EXAG_MIN = 1, EXAG_MAX = 5;
const STAKE = '#f2500f'; // color del predio (baliza de topografo)

const $ = (id) => document.getElementById(id);
const fmtExag = (v) => v.toFixed(1).replace('.', ',');
const nf0 = new Intl.NumberFormat('es-CL', { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat('es-CL', { maximumFractionDigits: 1, minimumFractionDigits: 1 });

class ViewerError extends Error {}

// ------------------------------------------------------------------------------------------ parametros / entrada
function readParams() {
  const q = new URLSearchParams(location.search);
  const site = q.get('site') || 'sample/out/conclusivo';
  if (!/^[\p{L}\p{N}_\-./]+$/u.test(site) || site.includes('..') || site.includes('//')) {
    throw new ViewerError('El parámetro site debe ser una ruta relativa dentro del servidor (del mismo servidor; sin esquema, sin «..»).');
  }
  const exag = parseFloat(q.get('exag'));
  const capas = (q.get('capas') || '').split(',').filter((c) => LAYERS.includes(c));
  const fecha = /^\d{4}-\d{2}-\d{2}$/.test(q.get('fecha') || '') ? q.get('fecha') : null;
  const hora = parseTime(q.get('hora'));
  const vista = ['panorama', 'predio'].includes(q.get('vista')) ? q.get('vista') : 'panorama';
  return { site: site.replace(/\/$/, ''), exag: Number.isFinite(exag) ? exag : null, capas, fecha, hora, vista, tour: q.has('tour') };
}

async function fetchJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new ViewerError(`No se pudo leer ${url} (HTTP ${r.status}).`);
  return r.json();
}

function loadImage(url) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => resolve(null);
    img.src = url;
  });
}

/** Una capa PNG es utilizable si existe y no es totalmente transparente. */
function imageHasContent(img) {
  const s = Math.min(1, 512 / Math.max(img.width, img.height));
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(img.width * s));
  c.height = Math.max(1, Math.round(img.height * s));
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, c.width, c.height);
  const d = ctx.getImageData(0, 0, c.width, c.height).data;
  for (let i = 3; i < d.length; i += 4) if (d[i] > 0) return true;
  return false;
}

function collectExtras(root) {
  const out = {};
  root.traverse((o) => Object.assign(out, o.userData || {}));
  return out;
}

function lotRingFromGeoJSON(gj) {
  let g = gj;
  if (g.type === 'FeatureCollection') g = g.features[0];
  if (g.type === 'Feature') g = g.geometry;
  if (g.type === 'MultiPolygon') g = { type: 'Polygon', coordinates: g.coordinates[0] };
  if (g.type !== 'Polygon') throw new ViewerError('predio.geojson debe contener un Polygon en metros locales.');
  const ring = g.coordinates[0].map(([x, y]) => [x, y]);
  if (ring.length > 1 && ring[0][0] === ring[ring.length - 1][0] && ring[0][1] === ring[ring.length - 1][1]) ring.pop();
  if (ring.length < 3) throw new ViewerError('predio.geojson tiene menos de 3 vértices.');
  return ring; // [x este, y norte]
}

// ------------------------------------------------------------------------------------------ principal
async function main() {
  const params = readParams();
  const base = params.site;
  const [manifest, lotGeo] = await Promise.all([fetchJson(`${base}/manifest.json`), fetchJson(`${base}/predio.geojson`)]);
  const gltf = await new GLTFLoader().loadAsync(`${base}/terreno.glb`);
  const extras = collectExtras(gltf.scene);
  for (const k of ['exaggeration', 'bbox_local_m', 'elevation_min', 'elevation_max']) {
    if (extras[k] === undefined) throw new ViewerError(`El GLB no declara extras.${k}: no se puede rotular la exageración vertical.`);
  }
  const E0 = extras.exaggeration;
  const [xmin, ymin, xmax, ymax] = extras.bbox_local_m;
  const lotRing = lotRingFromGeoJSON(lotGeo);

  const mesh0 = (() => { let m = null; gltf.scene.traverse((o) => { if (!m && o.isMesh) m = o; }); return m; })();
  if (!mesh0) throw new ViewerError('El GLB no contiene una malla.');

  const layerState = {};
  await Promise.all(LAYERS.map(async (n) => {
    const img = await loadImage(`${base}/layers/${n}.png`);
    layerState[n] = !img ? { status: 'missing' } : imageHasContent(img) ? { status: 'ok', img } : { status: 'empty' };
  }));

  const terr = manifest.results.terrain;
  const conf = manifest.confidence;
  const baseDs = manifest.inputs.datasets.find((d) => d.role === 'base') || manifest.inputs.datasets[0];
  const [lon, lat] = manifest.inputs.centroid_lonlat;

  const state = {
    exaggeration: Math.min(EXAG_MAX, Math.max(EXAG_MIN, params.exag ?? E0)),
    layers: Object.fromEntries(LAYERS.map((n) => [n, false])),
    fecha: params.fecha || new Date().toLocaleDateString('sv-SE', { timeZone: 'America/Santiago' }),
    minutes: params.hora ?? 16 * 60 + 30,
    view: params.vista,
  };

  // ---------------------------------------------------------------------------------------- escena
  const stage = $('stage');
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  renderer.setClearColor(0x000000, 0);
  stage.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(32, 1, 5, 80000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = false;
  controls.maxPolarAngle = THREE.MathUtils.degToRad(84);
  controls.screenSpacePanning = false;

  const dx = xmax - xmin, dy = ymax - ymin;
  const R = Math.hypot(dx, dy) / 2; // radio del panorama
  const group = new THREE.Group(); // todo lo que depende de la altura se escala en y con la exageracion
  scene.add(group);

  // malla del terreno (copia del GLB con colores hipsometricos)
  const geo = mesh0.geometry.clone();
  if (!geo.attributes.uv) throw new ViewerError('El GLB no trae UV.');
  geo.computeVertexNormals();
  geo.computeBoundingBox();
  const bakedMaxY = geo.boundingBox.max.y;
  const elevOf = (yBaked) => yBaked / E0 + extras.elevation_min;
  paintHypsometric(geo, (y) => (elevOf(y) - extras.elevation_min) / Math.max(1e-6, extras.elevation_max - extras.elevation_min));
  const terrain = new THREE.Mesh(geo, new THREE.MeshLambertMaterial({ vertexColors: true }));
  terrain.castShadow = terrain.receiveShadow = true;
  group.add(terrain);
  group.add(buildSkirt(geo, -Math.max(20, bakedMaxY * 0.22)));

  // capa superpuesta (capas PNG + relleno del predio)
  const overlayCanvas = document.createElement('canvas');
  overlayCanvas.width = 2048;
  overlayCanvas.height = Math.max(256, Math.round(2048 * (dy / dx)));
  const overlayTex = new THREE.CanvasTexture(overlayCanvas);
  overlayTex.flipY = false;
  overlayTex.colorSpace = THREE.SRGBColorSpace;
  overlayTex.anisotropy = renderer.capabilities.getMaxAnisotropy();
  const overlay = new THREE.Mesh(geo, new THREE.MeshLambertMaterial({
    map: overlayTex, transparent: true, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2,
  }));
  overlay.receiveShadow = true;
  overlay.renderOrder = 1;
  group.add(overlay);

  // predio: cinta pegada al terreno + cortina translucida
  group.updateMatrixWorld(true);
  const ray = new THREE.Raycaster();
  const heightAt = (x, z) => {
    ray.set(new THREE.Vector3(x, 1e5, z), new THREE.Vector3(0, -1, 0));
    const h = ray.intersectObject(terrain, false)[0];
    return h ? h.point.y : null;
  };
  const lot = buildLot(lotRing, heightAt, Math.hypot(...bboxOf(lotRing)), R);
  group.add(lot.ribbon, lot.curtain);
  const lotCenter = lot.center; // [x, z]
  const lotBaseY = heightAt(lotCenter[0], lotCenter[1]) ?? 0;

  // luz
  const hemi = new THREE.HemisphereLight(0xdfe9f2, 0x8e8f86, 0.9);
  const sun = new THREE.DirectionalLight(0xfff1d8, 2.6);
  sun.castShadow = true;
  sun.shadow.mapSize.set(4096, 4096);
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = 1.5;
  scene.add(hemi, sun, sun.target);

  // trayectoria solar
  const sunGroup = new THREE.Group();
  scene.add(sunGroup);
  const sunBall = new THREE.Mesh(new THREE.SphereGeometry(1, 24, 16), new THREE.MeshBasicMaterial({ color: 0xffc94d }));
  sunGroup.add(sunBall);
  const sunHalo = new THREE.Sprite(new THREE.SpriteMaterial({ map: haloTexture(), transparent: true, depthWrite: false }));
  sunGroup.add(sunHalo);
  const arcLine = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: 0xe0a02e, transparent: true, opacity: 0.85 }));
  const rayLine = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineDashedMaterial({ color: 0xe0a02e, dashSize: 1, gapSize: 1, transparent: true, opacity: 0.55 }));
  const ticks = new THREE.Group();
  sunGroup.add(arcLine, rayLine, ticks);
  const centre = new THREE.Vector3((xmin + xmax) / 2, 0, -(ymin + ymax) / 2);
  const arcR = R * 0.92;

  // ---------------------------------------------------------------------------------------- estado -> escena
  let dirty = true;
  const requestRender = () => { dirty = true; };
  const sunInfo = { altitudeDeg: 0, azimuthDeg: 0 };

  function applyExaggeration() {
    const k = state.exaggeration / E0;
    group.scale.y = k;
    group.updateMatrixWorld(true);
    $('exag-chip').textContent = `Exageración ×${fmtExag(state.exaggeration)}`;
    $('exag-out').textContent = `×${fmtExag(state.exaggeration)}`;
    if ($('exag').value !== String(state.exaggeration)) $('exag').value = String(state.exaggeration);
    updateSun(); // la altura de la trayectoria depende del centro vertical
    requestRender();
  }

  function groundY() { return lotBaseY * group.scale.y; }

  function updateSun() {
    const date = localToDate(state.fecha, state.minutes);
    const s = sunAt(date, lat, lon);
    sunInfo.altitudeDeg = s.altitudeDeg;
    sunInfo.azimuthDeg = s.azimuthDeg;
    const dir = new THREE.Vector3(...s.dir);
    const up = s.altitudeDeg > 0;
    const c = new THREE.Vector3(centre.x, groundY(), centre.z);

    sun.position.copy(c).addScaledVector(dir, R * 3);
    sun.target.position.copy(c);
    sun.target.updateMatrixWorld();
    const cam = sun.shadow.camera;
    const ext = R * 1.15;
    cam.left = -ext; cam.right = ext; cam.top = ext; cam.bottom = -ext;
    cam.near = R * 3 - R * 1.6 - bakedMaxY * state.exaggeration / E0; cam.near = Math.max(1, cam.near);
    cam.far = R * 3 + R * 1.6 + bakedMaxY * state.exaggeration / E0;
    cam.updateProjectionMatrix();

    const lift = Math.max(0, Math.min(1, s.altitudeDeg / 12)); // atardecer: la luz se apaga suave
    sun.visible = up;
    sun.intensity = 3.4 * lift * (0.55 + 0.45 * Math.min(1, s.altitudeDeg / 35));
    sun.color.set(s.altitudeDeg < 14 ? 0xffd9a8 : 0xfff3dc);
    hemi.intensity = up ? 0.42 + 0.3 * lift : 0.5;
    hemi.color.set(up ? 0xdfe9f2 : 0x8da0b8);
    document.body.dataset.sunUp = up ? '1' : '0';
    document.body.classList.toggle('night', !up);

    sunBall.position.copy(c).addScaledVector(dir, arcR);
    sunBall.scale.setScalar(R * 0.022);
    sunHalo.position.copy(sunBall.position);
    sunHalo.scale.setScalar(R * 0.16);
    sunBall.visible = sunHalo.visible = up;
    const pts = [c.clone(), sunBall.position.clone()];
    rayLine.geometry.setFromPoints(pts);
    rayLine.computeLineDistances();
    rayLine.material.dashSize = R * 0.025; rayLine.material.gapSize = R * 0.02;
    rayLine.visible = up;

    // arco del dia + marcas horarias
    const path = sunPath(state.fecha, lat, lon, 10);
    arcLine.geometry.setFromPoints(path.map((p) => c.clone().addScaledVector(new THREE.Vector3(...p.dir), arcR)));
    while (ticks.children.length) { const t = ticks.children.pop(); t.material.map?.dispose(); t.material.dispose(); }
    for (const p of path) {
      if (p.minutes % 180 !== 0 || p.altitudeDeg < 3) continue;
      const spr = labelSprite(`${p.minutes / 60}h`);
      spr.position.copy(c).addScaledVector(new THREE.Vector3(...p.dir), arcR * 1.0);
      spr.scale.set(R * 0.11, R * 0.055, 1);
      ticks.add(spr);
    }
    $('sol-lectura').textContent = up
      ? `${fmtTime(state.minutes)} · altura ${Math.round(s.altitudeDeg)}° · desde el ${compassName(s.azimuthDeg)}`
      : `${fmtTime(state.minutes)} · sol bajo el horizonte`;
    $('hora').value = String(state.minutes);
    if ($('fecha').value !== state.fecha) $('fecha').value = state.fecha;
    requestRender();
  }

  // capas ------------------------------------------------------------------------------------
  const lotPx = (x, y) => [((x - xmin) / dx) * overlayCanvas.width, (1 - (y - ymin) / dy) * overlayCanvas.height];
  function composeOverlay() {
    const ctx = overlayCanvas.getContext('2d');
    ctx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    for (const n of LAYER_ORDER) {
      if (state.layers[n] && layerState[n].status === 'ok') ctx.drawImage(layerState[n].img, 0, 0, overlayCanvas.width, overlayCanvas.height);
    }
    ctx.beginPath();
    lotRing.forEach(([x, y], i) => { const [px, py] = lotPx(x, y); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
    ctx.closePath();
    ctx.fillStyle = 'rgba(242, 80, 15, 0.26)';
    ctx.fill();
    overlayTex.needsUpdate = true;
    requestRender();
  }

  function setLayer(name, on) {
    if (!LAYERS.includes(name)) return;
    const ok = layerState[name].status === 'ok';
    if (state.layers[name] === (!!on && ok)) return;
    state.layers[name] = !!on && ok;
    const cb = $(`capa-${name}`);
    if (cb.checked !== state.layers[name]) cb.checked = state.layers[name];
    composeOverlay();
    renderLegend();
    syncUrl();
  }

  function renderLegend() {
    const box = $('leyenda');
    const L = LEGEND_JSON;
    const parts = [];
    if (state.layers.pendiente) {
      parts.push(`<div class="key"><b>Pendiente</b>${L.pendiente.clases.map((c, i) => `<span><i style="background:${L.pendiente.colores[i]}"></i>${c}</span>`).join('')}</div>`);
    }
    if (state.layers.orientacion) {
      parts.push(`<div class="key"><b>Orientación (hacia donde baja)</b>${L.orientacion.sectores.map((c, i) => `<span><i style="background:${L.orientacion.colores[i]}"></i>${c}</span>`).join('')}</div>`);
    }
    if (state.layers.curvas) {
      parts.push(`<div class="key"><b>Curvas cada ${terr.contour_interval_m ?? L.curvas.equidistancia_m} m</b><span class="wide">Indicativas, no para diseño.</span></div>`);
    }
    if (state.layers.sombreado) parts.push('<div class="key"><b>Sombreado</b><span class="wide">Relieve iluminado desde el noroeste.</span></div>');
    box.innerHTML = parts.join('');
    box.hidden = parts.length === 0;
  }

  // camara -------------------------------------------------------------------------------------
  const TARGET_PANO = () => new THREE.Vector3(centre.x, groundY(), centre.z);
  const TARGET_LOT = () => new THREE.Vector3(lotCenter[0], groundY(), lotCenter[1]);
  const lotSpan = Math.max(...bboxOf(lotRing));
  function setCameraSpherical({ target, bearingDeg, elevationDeg, dist }) {
    // bearingDeg: direccion desde la que se mira (rumbo de la camara respecto del objetivo, desde el norte, horario)
    const b = THREE.MathUtils.degToRad(bearingDeg), e = THREE.MathUtils.degToRad(elevationDeg);
    controls.target.copy(target);
    camera.position.set(target.x + dist * Math.cos(e) * Math.sin(b), target.y + dist * Math.sin(e), target.z - dist * Math.cos(e) * Math.cos(b));
    controls.update();
    requestRender();
  }
  const VIEWS = {
    panorama: () => ({ target: TARGET_PANO(), bearingDeg: 205, elevationDeg: 30, dist: R * 4.7 }),
    predio: () => ({ target: TARGET_LOT(), bearingDeg: 200, elevationDeg: 27, dist: Math.max(300, lotSpan * 3.8) }),
  };
  let tween = null;
  function goView(name, { animate = true } = {}) {
    state.view = name;
    document.querySelectorAll('[data-view]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.view === name)));
    const v = VIEWS[name]();
    if (!animate) { setCameraSpherical(v); syncUrl(); return; }
    const from = { t: controls.target.clone(), p: camera.position.clone() };
    const to = { t: v.target, p: sphericalPos(v) };
    const t0 = performance.now();
    tween = (now) => {
      const k = Math.min(1, (now - t0) / 1100);
      const e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      controls.target.lerpVectors(from.t, to.t, e);
      camera.position.lerpVectors(from.p, to.p, e);
      controls.update();
      requestRender();
      if (k >= 1) tween = null;
    };
    syncUrl();
  }
  function sphericalPos({ target, bearingDeg, elevationDeg, dist }) {
    const b = THREE.MathUtils.degToRad(bearingDeg), e = THREE.MathUtils.degToRad(elevationDeg);
    return new THREE.Vector3(target.x + dist * Math.cos(e) * Math.sin(b), target.y + dist * Math.sin(e), target.z - dist * Math.cos(e) * Math.cos(b));
  }

  // layout: la escena se centra en el espacio libre entre los paneles
  function layout() {
    document.documentElement.style.setProperty('--title-h', `${$('titulo').offsetHeight}px`);
    const w = stage.clientWidth, h = stage.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    let shift = 0, cx = w / 2;
    if (w >= 960) {
      const left = Math.max($('controles').getBoundingClientRect().right, $('titulo').getBoundingClientRect().right);
      const right = $('panel-fuente').getBoundingClientRect().left;
      cx = (left + right) / 2;
      shift = cx - w / 2;
    }
    camera.setViewOffset(w, h, -shift, 0, w, h);
    camera.updateProjectionMatrix();
    document.documentElement.style.setProperty('--cx', `${cx}px`);
    requestRender();
  }

  // compas
  function updateCompass() {
    const az = controls.getAzimuthalAngle(); // 0 = camara en +z mirando al norte
    $('compass-needle').style.transform = `rotate(${(az * 180) / Math.PI}deg)`;
  }

  // URL de solo lectura ------------------------------------------------------------------------
  function shareUrl() {
    const q = new URLSearchParams();
    q.set('site', base);
    q.set('exag', String(state.exaggeration));
    const on = LAYERS.filter((n) => state.layers[n]);
    if (on.length) q.set('capas', on.join(','));
    q.set('fecha', state.fecha);
    q.set('hora', fmtTime(state.minutes));
    q.set('vista', state.view);
    for (const k of q.keys()) if (!SHARE_KEYS.includes(k)) q.delete(k);
    return `${location.origin}${location.pathname}?${q.toString().replace(/%2C/g, ',').replace(/%2F/g, '/').replace(/%3A/g, ':')}`;
  }
  function syncUrl() {
    const u = shareUrl();
    $('share-url').value = u;
    if (!params.tour) history.replaceState(null, '', u);
  }

  // ---------------------------------------------------------------------------------------- UI
  const LEGEND_JSON = await fetchJson('legend.json');
  fillPanels();
  wireLayerToggles();
  $('exag').min = EXAG_MIN; $('exag').max = EXAG_MAX;
  $('exag').addEventListener('input', (e) => { state.exaggeration = parseFloat(e.target.value); applyExaggeration(); syncUrl(); });
  $('fecha').addEventListener('input', (e) => { if (e.target.value) { state.fecha = e.target.value; updateSun(); syncUrl(); } });
  $('hora').addEventListener('input', (e) => { state.minutes = parseInt(e.target.value, 10); updateSun(); syncUrl(); });
  document.querySelectorAll('[data-season]').forEach((b) => b.addEventListener('click', () => {
    const [mm, dd] = b.dataset.season.split('-');
    const y = state.fecha.slice(0, 4);
    state.fecha = `${y}-${mm}-${dd}`;
    updateSun(); syncUrl();
  }));
  document.querySelectorAll('[data-view]').forEach((b) => b.addEventListener('click', () => goView(b.dataset.view)));
  $('share-copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('share-url').value); $('share-copy').textContent = 'Enlace copiado'; }
    catch { $('share-url').select(); $('share-copy').textContent = 'Seleccionado: Ctrl+C'; }
    setTimeout(() => { $('share-copy').textContent = 'Copiar enlace'; }, 1800);
  });
  $('share-url').addEventListener('focus', (e) => e.target.select());

  controls.addEventListener('change', () => { updateCompass(); requestRender(); });
  controls.addEventListener('start', () => { tween = null; });
  window.addEventListener('resize', layout);

  function fillPanels() {
    $('h-sitio').textContent = manifest.sample_notice?.nombre || 'Terreno preliminar';
    if (manifest.sample_notice?.sintetico) { $('muestra').hidden = false; $('muestra').textContent = 'Muestra sintética · no es un sitio real'; }
    const area = manifest.inputs.polygon_area_m2;
    $('h-meta').textContent = `${nf0.format(area)} m² (${nf1.format(area / 10000)} ha) · ${nf1.format(Math.abs(lat)).replace(/(\d),(\d)/, '$1,$2')}° ${lat < 0 ? 'S' : 'N'}, ${nf1.format(Math.abs(lon))}° ${lon < 0 ? 'O' : 'E'}`.replace(/\s+/g, ' ');
    const src = extras.source || (baseDs.source === 'glo30' ? 'Copernicus GLO-30' : baseDs.name);
    $('f-fuente').textContent = `${src} (DSM)`;
    const px = manifest.grid.pixel_m;
    $('f-resolucion').textContent = `${nf0.format(px)} m · 1 píxel ≈ ${nf0.format(px * px)} m²`;
    $('n-pixeles').textContent = String(terr.n_pixels);
    $('f-datum').textContent = (manifest.grid.vertical_datum || extras.datum || '').replace(/ \(.*$/, '') + ' (m s. n. m.)';
    $('f-error').textContent = T.VERTICAL_TOLERANCE;
    $('f-fecha').textContent = manifest.volatile?.acquired?.[baseDs.source] || 'sin fecha';
    const lvl = conf.overall;
    $('nivel-letra').textContent = lvl;
    $('nivel-placa').dataset.level = lvl;
    $('nivel-texto').textContent = T.LEVEL_TEXT[lvl] || '';
    $('revision').hidden = !conf.human_review_required;
    const lim = (conf.limiting_factors || []).filter((f) => !['sin_dato', 'no_disponible'].includes(conf.by_factor?.[f])).map((f) => T.FACTOR_LABEL[f] || f);
    const sd = Object.entries(conf.by_factor || {}).filter(([, v]) => v === 'sin_dato').map(([k]) => T.FACTOR_LABEL[k] || k);
    const nd = Object.entries(conf.by_factor || {}).filter(([, v]) => v === 'no_disponible').map(([k]) => T.FACTOR_LABEL[k] || k);
    $('limitantes').innerHTML = [
      lim.length ? `<span>Limita el nivel:</span> ${lim.join(', ')}.` : '',
      sd.length ? `<span>Sin dato (el nivel no pasa de B):</span> ${sd.join(', ')}.` : '',
      nd.length ? `<span>No disponible (la lectura falló; el nivel no pasa de B y requiere revisión humana):</span> ${nd.join(', ')}.` : '',
    ].filter(Boolean).join('<br>');
    // resultados: la pendiente puntual se omite en nivel C o lote inconcluso (spec sec. 4)
    const noSlope = terr.inconclusive || conf.omit_point_slope;
    const rows = [];
    rows.push(['Elevación', `${nf0.format(terr.elev_min)}–${nf0.format(terr.elev_max)} m${terr.elevation_within_noise ? ' · desnivel dentro del ruido del dato' : ''}`]);
    rows.push(['Pendiente', noSlope ? 'No concluyente' : `${nf1.format(terr.slope_mean_pct)} % media (${nf1.format(terr.slope_mean_deg)}°)`]);
    if (!noSlope) rows.push(['Orientación', terr.aspect_dominant ? `Predominante ${terr.aspect_dominant}` : 'Sin orientación predominante']);
    $('resultados').innerHTML = rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('');
    $('t-disclaimer').textContent = T.DISCLAIMER;
    $('t-deskscreen').textContent = T.DESK_SCREEN;
    $('t-resolucion').textContent = T.RESOLUTION_NOTICE;
    $('t-atribucion').textContent = `${T.GLO30_ATTRIBUTION}. ${T.GLO30_LIABILITY}.`;
    $('t-aval').textContent = T.NO_ENDORSEMENT;
  }

  function wireLayerToggles() {
    const reason = {
      pendiente: () => (terr.inconclusive ? 'No concluyente: el predio tiene menos de 9 píxeles de 30 m.'
        : conf.omit_point_slope ? 'No concluyente en este nivel de confianza.' : 'No disponible en esta salida.'),
      orientacion: () => (terr.aspect_status === 'sin_orientacion_predominante' ? 'Sin orientación predominante: terreno casi plano.'
        : terr.inconclusive ? 'No concluyente: el predio tiene menos de 9 píxeles de 30 m.' : 'No disponible en esta salida.'),
      curvas: () => 'No disponible en esta salida.',
      sombreado: () => 'No disponible en esta salida.',
    };
    for (const n of LAYERS) {
      const cb = $(`capa-${n}`);
      const ok = layerState[n].status === 'ok';
      cb.disabled = !ok;
      cb.closest('label').classList.toggle('off', !ok);
      $(`capa-${n}-nota`).textContent = ok ? '' : reason[n]();
      cb.addEventListener('change', () => setLayer(n, cb.checked));
    }
  }

  // ---------------------------------------------------------------------------------------- arranque
  applyExaggeration();
  composeOverlay();
  layout();
  for (const n of params.capas) setLayer(n, true);
  goView(params.vista, { animate: false });
  updateCompass();
  syncUrl();

  const renderNow = () => { tween?.(performance.now()); updateCompass(); renderer.render(scene, camera); dirty = false; };
  (function loop() {
    if (tween) tween(performance.now());
    if (dirty) { renderer.render(scene, camera); dirty = false; }
    requestAnimationFrame(loop);
  })();

  // API para pruebas y para el recorrido del video
  window.__viewer = {
    state,
    renderNow,
    sunNow: () => ({ ...sunInfo }),
    info: () => ({
      vertices: geo.attributes.position.count,
      lotPoints: lotRing.length,
      meshScaleY: group.scale.y,
      baked: E0,
      extent: { dx, dy, R },
      lot: { center: lotCenter, span: lotSpan },
    }),
    api: {
      THREE, camera, controls, group, requestRender, renderNow,
      setExaggeration(v) { state.exaggeration = Math.min(EXAG_MAX, Math.max(EXAG_MIN, v)); applyExaggeration(); },
      setLayer, setSun(fecha, minutes) { if (fecha) state.fecha = fecha; if (minutes != null) state.minutes = Math.round(minutes); updateSun(); },
      setCameraSpherical, views: VIEWS, goView, layout, groundY,
      manifest, lotSpan, R,
    },
  };

  $('cargando').hidden = true;
  document.body.dataset.ready = 'true';
  if (params.tour) await import('./tour.js').then((m) => m.installTour(window.__viewer));
}

// ------------------------------------------------------------------------------------------ geometria auxiliar
function bboxOf(ring) {
  const xs = ring.map((p) => p[0]), ys = ring.map((p) => p[1]);
  return [Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)];
}

const HYPSO = [[0, '#7c9275'], [0.3, '#a9ad86'], [0.55, '#c8bb93'], [0.8, '#c3b6a5'], [1, '#e6e1d6']].map(([t, c]) => [t, new THREE.Color(c)]);
function paintHypsometric(geo, tOf) {
  const pos = geo.attributes.position;
  const col = new Float32Array(pos.count * 3);
  const c = new THREE.Color();
  for (let i = 0; i < pos.count; i++) {
    const t = Math.min(1, Math.max(0, tOf(pos.getY(i))));
    let j = 1;
    while (j < HYPSO.length - 1 && t > HYPSO[j][0]) j++;
    const [t0, c0] = HYPSO[j - 1], [t1, c1] = HYPSO[j];
    c.copy(c0).lerp(c1, (t - t0) / (t1 - t0));
    col.set([c.r, c.g, c.b], i * 3);
  }
  geo.setAttribute('color', new THREE.BufferAttribute(col, 3));
}

/** Faldon vertical bajo el borde de la malla (aristas usadas por una sola cara). */
function buildSkirt(geo, bottomY) {
  const idx = geo.index.array, pos = geo.attributes.position;
  const edges = new Map();
  for (let f = 0; f < idx.length; f += 3) {
    for (let k = 0; k < 3; k++) {
      const a = idx[f + k], b = idx[f + ((k + 1) % 3)];
      const key = a < b ? `${a}_${b}` : `${b}_${a}`;
      const e = edges.get(key);
      if (e) e.n++; else edges.set(key, { a, b, n: 1 });
    }
  }
  const v = [], c = [];
  const top = new THREE.Color('#a79f90'), bot = new THREE.Color('#8b8579');
  for (const { a, b, n } of edges.values()) {
    if (n !== 1) continue;
    const A = [pos.getX(a), pos.getY(a), pos.getZ(a)], B = [pos.getX(b), pos.getY(b), pos.getZ(b)];
    v.push(...A, ...B, B[0], bottomY, B[2], ...A, B[0], bottomY, B[2], A[0], bottomY, A[2]);
    c.push(top.r, top.g, top.b, top.r, top.g, top.b, bot.r, bot.g, bot.b, top.r, top.g, top.b, bot.r, bot.g, bot.b, bot.r, bot.g, bot.b);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(v, 3));
  g.setAttribute('color', new THREE.Float32BufferAttribute(c, 3));
  g.computeVertexNormals();
  const m = new THREE.Mesh(g, new THREE.MeshLambertMaterial({ vertexColors: true, side: THREE.DoubleSide }));
  m.castShadow = false;
  return m;
}

function buildLot(ring, heightAt, span, R) {
  // x este, y norte -> escena (x, z = -y); se densifica cada ~12 m para que la cinta siga el relieve
  const pts = [];
  const step = 12;
  for (let i = 0; i < ring.length; i++) {
    const [x0, y0] = ring[i], [x1, y1] = ring[(i + 1) % ring.length];
    const n = Math.max(1, Math.ceil(Math.hypot(x1 - x0, y1 - y0) / step));
    for (let k = 0; k < n; k++) pts.push([x0 + ((x1 - x0) * k) / n, -(y0 + ((y1 - y0) * k) / n)]);
  }
  const w = Math.max(3, span * 0.012, R * 0.0055);
  const N = pts.length;
  // orientacion del anillo (para saber cual lado es "interior")
  let area2 = 0;
  for (let i = 0; i < N; i++) { const [a, b] = [pts[i], pts[(i + 1) % N]]; area2 += a[0] * b[1] - b[0] * a[1]; }
  const sgn = area2 > 0 ? 1 : -1;
  const pos = [], idx = [];
  const curtainPos = [], curtainCol = [];
  const curtainH = Math.max(30, Math.min(span * 0.2, R * 0.12), R * 0.04);
  for (let i = 0; i < N; i++) {
    const p = pts[i], a = pts[(i + N - 1) % N], b = pts[(i + 1) % N];
    let tx = b[0] - a[0], tz = b[1] - a[1];
    const L = Math.hypot(tx, tz) || 1; tx /= L; tz /= L;
    const nx = -tz * sgn, nz = tx * sgn; // normal hacia afuera (aprox.)
    const o = [p[0] + nx * w * 0.5, p[1] + nz * w * 0.5], q = [p[0] - nx * w * 0.5, p[1] - nz * w * 0.5];
    const ho = heightAt(o[0], o[1]) ?? 0, hq = heightAt(q[0], q[1]) ?? 0, hp = heightAt(p[0], p[1]) ?? 0;
    pos.push(o[0], Math.max(ho, hp) + 1.6, o[1], q[0], Math.max(hq, hp) + 1.6, q[1]);
    curtainPos.push(p[0], hp + 0.5, p[1], p[0], hp + curtainH, p[1]);
    curtainCol.push(0.95, 0.31, 0.06, 0.5, 0.95, 0.31, 0.06, 0.0);
  }
  const cidx = [];
  for (let i = 0; i < N; i++) {
    const j = (i + 1) % N;
    idx.push(2 * i, 2 * i + 1, 2 * j, 2 * j, 2 * i + 1, 2 * j + 1);
    cidx.push(2 * i, 2 * i + 1, 2 * j, 2 * j, 2 * i + 1, 2 * j + 1);
  }
  const rg = new THREE.BufferGeometry();
  rg.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  rg.setIndex(idx);
  const ribbon = new THREE.Mesh(rg, new THREE.MeshBasicMaterial({ color: STAKE, side: THREE.DoubleSide }));
  const cg = new THREE.BufferGeometry();
  cg.setAttribute('position', new THREE.Float32BufferAttribute(curtainPos, 3));
  cg.setAttribute('color', new THREE.Float32BufferAttribute(curtainCol, 4));
  cg.setIndex(cidx);
  const curtain = new THREE.Mesh(cg, new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true, side: THREE.DoubleSide, depthWrite: false }));
  curtain.renderOrder = 2;
  const cx = ring.reduce((s, p) => s + p[0], 0) / ring.length, cy = ring.reduce((s, p) => s + p[1], 0) / ring.length;
  return { ribbon, curtain, center: [cx, -cy] };
}

function haloTexture() {
  const c = document.createElement('canvas'); c.width = c.height = 128;
  const g = c.getContext('2d');
  const grd = g.createRadialGradient(64, 64, 2, 64, 64, 64);
  grd.addColorStop(0, 'rgba(255,214,120,0.85)'); grd.addColorStop(0.35, 'rgba(255,200,90,0.25)'); grd.addColorStop(1, 'rgba(255,200,90,0)');
  g.fillStyle = grd; g.fillRect(0, 0, 128, 128);
  return new THREE.CanvasTexture(c);
}

function labelSprite(text) {
  const c = document.createElement('canvas'); c.width = 128; c.height = 64;
  const g = c.getContext('2d');
  g.font = '700 38px "Instrument Sans Variable", system-ui, sans-serif';
  g.textAlign = 'center'; g.textBaseline = 'middle';
  g.fillStyle = '#7a4f06';
  g.fillText(text, 64, 34);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false }));
}

main().catch((err) => {
  console.error(err);
  document.body.dataset.error = err instanceof ViewerError ? err.message : `Error inesperado: ${err.message}`;
  const box = $('error');
  box.hidden = false;
  box.textContent = document.body.dataset.error;
  $('cargando').hidden = true;
});
