// Copia las dependencias fijadas (node_modules) a viewer/vendor para que la pagina funcione desde un servidor estatico
// sin CDN ni bundler.   Uso: npm run vendor
import { cpSync, mkdirSync, rmSync, readFileSync, writeFileSync } from 'node:fs';

const NM = 'node_modules';
rmSync('vendor', { recursive: true, force: true });
mkdirSync('vendor/three/addons/loaders', { recursive: true });
mkdirSync('vendor/three/addons/controls', { recursive: true });
mkdirSync('vendor/three/addons/utils', { recursive: true });
mkdirSync('vendor/suncalc', { recursive: true });
mkdirSync('vendor/fonts', { recursive: true });

for (const f of ['three.module.min.js', 'three.core.min.js']) cpSync(`${NM}/three/build/${f}`, `vendor/three/${f}`);
for (const [d, f] of [['loaders', 'GLTFLoader.js'], ['controls', 'OrbitControls.js'],
  ['utils', 'BufferGeometryUtils.js'], ['utils', 'SkeletonUtils.js']]) {
  cpSync(`${NM}/three/examples/jsm/${d}/${f}`, `vendor/three/addons/${d}/${f}`);
}
cpSync(`${NM}/suncalc/index.js`, 'vendor/suncalc/index.js');
cpSync(`${NM}/suncalc/LICENSE`, 'vendor/suncalc/LICENSE');
cpSync(`${NM}/three/LICENSE`, 'vendor/three/LICENSE');
cpSync(`${NM}/@fontsource-variable/instrument-sans/files/instrument-sans-latin-wght-normal.woff2`,
  'vendor/fonts/instrument-sans-latin-wght-normal.woff2');
cpSync(`${NM}/@fontsource-variable/instrument-sans/LICENSE`, 'vendor/fonts/LICENSE-OFL.txt');

const v = (p) => JSON.parse(readFileSync(`${NM}/${p}/package.json`, 'utf8')).version;
writeFileSync('vendor/VERSIONS.json', JSON.stringify({
  three: v('three'), suncalc: v('suncalc'), '@fontsource-variable/instrument-sans': v('@fontsource-variable/instrument-sans'),
}, null, 2) + '\n');
console.log('vendor listo', v('three'), v('suncalc'));
