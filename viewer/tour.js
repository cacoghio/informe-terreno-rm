// Recorrido para el video (Task 12). Se activa con ?tour=1[&seconds=45]. Es DETERMINISTA: todo el estado es funcion de t,
// asi que el capturador (terreno/video.py -> tools/capture.mjs) pide window.__tour.seek(t) y toma una foto por fotograma.
// Guion (spec sec. 6): panorama -> acercamiento al poligono -> pasada con exageracion -> barrido de sombra -> tarjeta final.
import { sunPath } from './sun.js';
import * as T from './texts.js';

const clamp01 = (x) => Math.min(1, Math.max(0, x));
const ease = (k) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2);
const lerp = (a, b, k) => a + (b - a) * k;
const lerpAngle = (a, b, k) => a + ((((b - a) % 360) + 540) % 360 - 180) * k; // camino corto
const seg = (t, a, b) => clamp01((t - a) / (b - a));

export function installTour(viewer) {
  const { api, state } = viewer;
  const { THREE, manifest, R, lotSpan } = api;
  const q = new URLSearchParams(location.search);
  const D = Math.min(120, Math.max(5, parseFloat(q.get('seconds')) || 45));
  const baseExag = state.exaggeration;
  const raisedExag = Math.min(5, Math.max(baseExag + 2, 4));
  const [lon, lat] = manifest.inputs.centroid_lonlat;
  const fecha = state.fecha;
  const path = sunPath(fecha, lat, lon, 10).filter((p) => p.altitudeDeg >= 4);
  const sunFrom = path.length ? path[0].minutes : 8 * 60;
  const sunTo = path.length ? path[path.length - 1].minutes : 19 * 60;
  const endMinutes = 16 * 60 + 30;

  document.body.classList.add('tour');
  api.controls.enabled = false;

  const caption = document.createElement('div');
  caption.className = 'tour-caption';
  document.body.appendChild(caption);
  const card = document.createElement('div');
  card.className = 'tour-card';
  const conf = manifest.confidence;
  const terr = manifest.results.terrain;
  const px = manifest.grid.pixel_m;
  card.innerHTML = `
    <div class="plate" data-level="${conf.overall}"><span>${conf.overall}</span></div>
    <h3>Nivel de confianza ${conf.overall}</h3>
    <p class="src">Fuente: Copernicus GLO-30 · resolución real ${px} m · ${terr.n_pixels} píxeles en el predio</p>
    <p>${conf.human_review_required ? '<b>Revisión humana obligatoria.</b> ' : ''}${T.DISCLAIMER}</p>`;
  card.style.opacity = '0';
  document.body.appendChild(card);

  // estados de camara (target relativo al suelo del predio con la exageracion vigente)
  const pano = (bearing, elev, dist) => ({ tgt: 'pano', bearingDeg: bearing, elevationDeg: elev, dist });
  const lotv = (bearing, elev, dist) => ({ tgt: 'lot', bearingDeg: bearing, elevationDeg: elev, dist });
  const resolve = (s) => {
    const v = api.views[s.tgt === 'pano' ? 'panorama' : 'predio']();
    return { ...s, target: v.target };
  };
  const mix = (a, b, k) => {
    const A = resolve(a), B = resolve(b);
    return {
      target: A.target.clone().lerp(B.target, k),
      bearingDeg: lerpAngle(A.bearingDeg, B.bearingDeg, k),
      elevationDeg: lerp(A.elevationDeg, B.elevationDeg, k),
      dist: Math.exp(lerp(Math.log(A.dist), Math.log(B.dist), k)),
    };
  };

  const P1a = pano(205, 30, R * 4.7), P1b = pano(238, 34, R * 4.3);
  const L2 = lotv(200, 33, lotSpan * 4.6);
  const L3a = lotv(200, 33, lotSpan * 4.4), L3b = lotv(130, 27, lotSpan * 3.9);
  const L4a = lotv(130, 27, lotSpan * 3.9), L4b = lotv(165, 33, lotSpan * 6.0), L4c = lotv(215, 34, lotSpan * 6.8);
  const P5 = pano(205, 30, R * 4.7);

  function seek(t) {
    const u = clamp01(t / D);
    let cam, exag = baseExag, minutes = endMinutes, cap = '', cardOpacity = 0;
    if (u < 0.22) {
      cap = 'Panorama del sector';
      cam = mix(P1a, P1b, ease(seg(u, 0, 0.22)));
      minutes = 16 * 60 + 30;
    } else if (u < 0.44) {
      cap = 'Acercamiento al predio';
      cam = mix(P1b, L2, ease(seg(u, 0.22, 0.44)));
      minutes = 16 * 60 + 30;
    } else if (u < 0.64) {
      const k = seg(u, 0.44, 0.64);
      cap = 'Pasada con el relieve exagerado';
      cam = mix(L3a, L3b, ease(k));
      exag = lerp(baseExag, raisedExag, ease(seg(k, 0, 0.45)));
      minutes = 16 * 60 + 30;
    } else if (u < 0.88) {
      const k = seg(u, 0.64, 0.88);
      cap = 'Barrido de la sombra durante el día';
      cam = k < 0.5 ? mix(L4a, L4b, ease(k * 2)) : mix(L4b, L4c, ease((k - 0.5) * 2));
      exag = raisedExag;
      minutes = lerp(sunFrom, sunTo, k);
    } else {
      const k = seg(u, 0.88, 1);
      cap = '';
      cam = mix(L4c, P5, ease(seg(k, 0, 0.55)));
      exag = lerp(raisedExag, baseExag, ease(seg(k, 0, 0.35)));
      minutes = lerp(sunTo, endMinutes, ease(seg(k, 0, 0.55)));
      cardOpacity = ease(seg(k, 0.3, 0.55));
    }
    api.setLayer('curvas', u >= 0.5 && u < 0.88); // curvas indicativas durante la pasada y el barrido (si existen)
    api.setExaggeration(exag);
    api.setSun(null, minutes);
    const c = { ...cam, target: new THREE.Vector3(cam.target.x, api.groundY(), cam.target.z) };
    api.setCameraSpherical(c);
    caption.textContent = cap;
    caption.hidden = !cap;
    card.style.opacity = String(cardOpacity);
    card.style.visibility = cardOpacity > 0 ? 'visible' : 'hidden';
    api.renderNow();
    return { u, exag, minutes, cap };
  }

  window.__tour = { duration: D, seek, labels: ['panorama', 'acercamiento', 'pasada', 'sombra', 'tarjeta'] };
  seek(0);
}
