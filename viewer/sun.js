// Hora local de Chile -> posicion solar (SunCalc) en el sistema local del visor (x = este, y = arriba, z = -norte).
import * as SunCalc from 'suncalc';

export const TZ = 'America/Santiago';

function tzOffsetMin(utcMs, tz = TZ) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', second: 'numeric',
  }).formatToParts(new Date(utcMs));
  const o = Object.fromEntries(parts.map((p) => [p.type, p.value]));
  const asUtc = Date.UTC(+o.year, +o.month - 1, +o.day, +o.hour, +o.minute, +o.second);
  return (asUtc - Math.floor(utcMs / 1000) * 1000) / 60000;
}

/** Fecha 'YYYY-MM-DD' + minutos desde las 00:00 locales -> Date (UTC) respetando el horario de verano de Chile. */
export function localToDate(isoDate, minutes, tz = TZ) {
  const [y, m, d] = isoDate.split('-').map(Number);
  const guess = Date.UTC(y, m - 1, d, 0, minutes);
  let off = tzOffsetMin(guess, tz);
  let utc = guess - off * 60000;
  const off2 = tzOffsetMin(utc, tz);
  if (off2 !== off) utc = guess - off2 * 60000;
  return new Date(utc);
}

const COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SO', 'O', 'NO'];
export function compassName(bearingDeg) {
  return COMPASS[Math.round((((bearingDeg % 360) + 360) % 360) / 45) % 8];
}

/** Posicion del sol: altura, azimut (desde el norte, sentido horario) y vector unitario hacia el sol en coordenadas del visor. */
export function sunAt(date, lat, lon) {
  // SunCalc 2.x: altura y azimut en GRADOS; azimut medido desde el norte, en sentido horario.
  const p = SunCalc.getPosition(date, lat, lon);
  const bearing = ((p.azimuth % 360) + 360) % 360;
  const b = (bearing * Math.PI) / 180;
  const alt = (p.altitude * Math.PI) / 180;
  const ca = Math.cos(alt);
  return { altitudeDeg: p.altitude, azimuthDeg: bearing, dir: [ca * Math.sin(b), Math.sin(alt), -ca * Math.cos(b)] };
}

/** Puntos de la trayectoria del dia (solo con el sol sobre el horizonte). */
export function sunPath(isoDate, lat, lon, stepMin = 15) {
  const out = [];
  for (let m = 0; m < 1440; m += stepMin) {
    const s = sunAt(localToDate(isoDate, m), lat, lon);
    if (s.altitudeDeg > -0.5) out.push({ minutes: m, ...s });
  }
  return out;
}

export function fmtTime(minutes) {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
}

export function parseTime(hhmm) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm || '');
  if (!m) return null;
  const v = +m[1] * 60 + +m[2];
  return v >= 0 && v < 1440 ? v : null;
}
