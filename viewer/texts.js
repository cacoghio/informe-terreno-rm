// Textos FIJOS del visor (spec sec. 5). No los edita ni los genera el agente ni el manifiesto.
// Deben coincidir literalmente con terreno/texts_es.py (Task 10); tests/test_viewer_texts.py lo verifica si ese archivo existe.

export const DISCLAIMER =
  'Evaluación preliminar basada en datos públicos. Los resultados deben validarse con levantamientos detallados y ' +
  'estudios de ingeniería antes de tomar decisiones.';

export const DESK_SCREEN = 'Este informe es un filtro de escritorio (desk screen), no un levantamiento.';

export const RESOLUTION_NOTICE = 'Resolución real del dato: 30 m; un píxel (≈900 m²) es del orden de un lote urbano.';

export const NO_ENDORSEMENT = 'No se afirma aval oficial de Copernicus, ESA, JAXA ni de organismo chileno alguno.';

export const GLO30_ATTRIBUTION =
  'produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 ' +
  'provided under COPERNICUS by the European Union and ESA; all rights reserved';

export const GLO30_LIABILITY =
  'The organisations in charge of the Copernicus programme by law or by delegation do not incur any liability for any ' +
  'use of the Copernicus WorldDEM-30';

export const LEVEL_TEXT = {
  A: 'Dato apto para screening con la tolerancia declarada. No significa exacto.',
  B: 'Usar con cautela: hay factores que limitan la confianza.',
  C: 'Las cifras de pendiente puntuales se omiten del informe.',
};

export const FACTOR_LABEL = {
  pixels: 'píxeles dentro del predio',
  elev_range_m: 'rango de elevación',
  slope_mean_pct: 'pendiente media',
  disagreement_rms_m: 'desacuerdo GLO-30 / AW3D30',
  canopy_pct: 'dosel y construido',
  void_water_pct: 'vacíos, rellenos y agua',
  hem_p90_m: 'incertidumbre propia del dato (HEM)',
};

export const VERTICAL_TOLERANCE = '±4 m (LE90) como piso';
